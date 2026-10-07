"""Chat API：SSE 流式对话 + 会话上下文 + 消息落库（Phase 1）。

一次请求的完整链路：
    POST /chat/stream {conversation_id, message}
      → 路由：取身份 → Service 校验会话 + 取历史 → 落 user 消息 → 组上下文
      → 返回 StreamingResponse，开始推流：LLM 逐块出文本 → 包成 SSE 帧
      → 流结束：整条回复落库 → 推 [DONE]

为什么用 SSE 而不是普通 HTTP 响应：
    LLM 是逐 token 生成的。普通响应要等全部生成完才返回，用户对着白屏干等十几秒；
    SSE 让服务器保持连接、边生成边推，前端就能做打字机效果。

SSE 协议要点（HTTP 层只有两条规则）：
    1. 响应头 Content-Type: text/event-stream
    2. 响应体每条消息格式：data: <内容>\\n\\n   （结尾必须两个换行）

    注意：内容用 JSON 编码（json.dumps）而不是直接拼字符串 —— 模型输出里常带换行
    （比如 Markdown 列表），裸拼会把 SSE 的"一行一条 data"格式冲烂。

三种 SSE 帧（按出现顺序）：
    {"citations": [...]}   仅 use_knowledge=true 时的第一帧，引用来源
    {"text": "..."}        文本增量（打字机效果）
    {"tool_call": {...}}   Phase 3：一次工具调用**已完成**的记录（有耗时、有结果）
    {"error": "..."}       出错帧，出现即代表本轮结束
    data: [DONE]           正常结束帧
"""
import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from langchain_core.messages import BaseMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import tool_loop
from app.ai.llm_service import llm_service
from app.ai.rag.citation import build_context
from app.ai.rag.retriever import retrieve
from app.ai.tools import registry
from app.ai.tools.base import ToolCallRecord, ToolContext
from app.data.db import get_db
from app.data.models import Conversation
from app.schemas.chat import ChatRequest
from app.schemas.conversation import ToolCallOut
from app.application import chat_service
from app.api.deps import CurrentUser

router = APIRouter(prefix="/chat", tags=["chat"])


def _tool_call_payload(record: ToolCallRecord) -> ToolCallOut:
    """把一条工具调用记录压成推给前端的帧内容。

    用和会话详情里 tool_calls 同一个模型（ToolCallOut）：前端只需要一个类型，
    以后加字段也是两边一起加，不会出现"实时有、刷新后没有"的字段。

    只给"能展示的元信息"，不给 result.data —— 完整结果已经回填给模型、也落了
    tool_calls 表，再推一遍前端只是把同一份数据在网络上搬第三次
    （sql_query 一次能返回 200 行）。
    """
    result = record.result
    tool = registry.get_tool(record.call.name)
    return ToolCallOut(
        # message_id 留空：推帧时助手消息还没落库（它要等流结束才写），
        # 前端按"当前正在流式的这条消息"挂上去即可
        name=record.call.name,
        # tool_type 和落库那边一样在注册表查：它是"工具定义"的属性，
        # 不是"这次调用"的属性，让产出记录的地方填必然会有漏填的
        tool_type=tool.tool_type if tool is not None else "unknown",
        # args 要给：它是"模型这次到底查了什么"的信息，前端展开能看到具体 SQL / 查询词
        args=record.call.args,
        ok=result.ok,
        error=result.error,
        rows=result.rows,
        duration_ms=result.duration_ms,
        truncated=result.truncated,
    )


async def _sse_generator(
    session: AsyncSession,
    conversation: Conversation,
    messages: list[BaseMessage],
    citations: list[dict],
    ctx: ToolContext,
    use_tools: bool,
) -> AsyncIterator[str]:
    """把 LLM 的文本增量包装成 SSE 帧；流结束后把助手回复落库。

    本函数是异步生成器，实现体里必须出现 yield。
    """
    # 攒下所有增量：回复要"整条"入库，不能边流边写
    pieces: list[str] = []
    # usage 可能一个都没有（模型没开统计 / 中途报错），所以先给默认值兜底，
    # 否则落库时会因为变量未定义直接崩
    prompt_tokens = 0
    completion_tokens = 0
    # 本轮所有工具调用记录：流结束后落 tool_calls 表
    tool_records: list[ToolCallRecord] = []

    # 引用帧必须在文本帧**之前**推：前端要先拿到来源，才能在收字的过程中
    # 把回答里的 [1] 渲染成可点标签。放最后推的话用户看完答案才看到来源，
    # 引用就只是个装饰，起不到"答案从哪来"的作用。
    if citations:
        yield f"data: {json.dumps({'citations': citations}, ensure_ascii=False)}\n\n"

    # try/except 包住整段：SSE 一旦开始推送，HTTP 状态码就改不了了，
    # 模型中途报错只能把错误写进流里，让前端显示提示
    try:
        # 两个流都是"逐块吐、块类型同构"的异步生成器，所以能共用下面这一个循环：
        #   关掉工具 → llm_service.astream 直接问模型
        #   打开工具 → tool_loop.run 在它外面套一层"模型↔工具"的循环
        stream = (
            tool_loop.run(messages, ctx=ctx, role=ctx.role)
            if use_tools
            else llm_service.astream(messages)
        )
        async for chunk in stream:
            if chunk.type == "text":
                pieces.append(chunk.text)
                # 每块包成一个 SSE 帧。json.dumps 把内容里的换行转义掉，
                # 避免破坏 SSE"一行一条 data"的格式
                yield f"data: {json.dumps({'text': chunk.text}, ensure_ascii=False)}\n\n"
            elif chunk.type == "usage":
                # usage 不推给前端：它只服务落库和成本统计，
                # 混进文本流会污染前端的渲染
                #
                # 必须是 += 不能是 =：一轮工具调用会触发**多次**模型调用，
                # 每轮各报一次 usage。赋值会把前面几轮的 token 全丢掉
                # （表现是"用了工具的回答 token 反而特别少"，很难联想到是这里）
                prompt_tokens += chunk.prompt_tokens
                completion_tokens += chunk.completion_tokens
            elif chunk.type == "tool_call":
                # 只有 tool_loop 会产出这种块（工具已执行完，有结果有耗时）
                record = chunk.tool_call
                tool_records.append(record)
                # 立刻推：用户要能一边等一边看到"它在查什么"，
                # 攒到流结束再一起推就退化成了 loading 转圈
                payload = _tool_call_payload(record).model_dump(mode="json")
                yield f"data: {json.dumps({'tool_call': payload}, ensure_ascii=False)}\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        # 客户端点「停止」/断连：Starlette 抛的是这两个（继承自 BaseException，
        # 下面 except Exception 接不住，助手回复就永不落库、留一条「有问无答」的孤儿
        # user 消息脏了下一轮上下文）。尽力把已生成的部分落库后原样再抛——绝不吞取消信号。
        # best-effort：连接已断时 await 未必还有调度时间，但绝大多数情况能落上。
        try:
            message = await chat_service.save_message(
                session,
                conversation=conversation,
                role="assistant",
                content="".join(pieces),
                error_message="客户端中断",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                citations=citations or None,
            )
            await chat_service.save_tool_calls(
                session, conversation=conversation, message=message, records=tool_records
            )
        except Exception:  # noqa: BLE001 —— 落库失败也不能掩盖原始取消
            pass
        raise
    except Exception as e:
        # 异常可能源自 DB 本身（会话已被判 abort）：先回滚恢复会话，否则下面的留痕落库会二次失败。
        # rollback 会把 conversation 标过期，async 下同步访问过期属性会炸——refresh 恢复。
        await session.rollback()
        await session.refresh(conversation)
        # 已经生成的部分照常落库（带 error_message），事后能查这次为什么断
        message = await chat_service.save_message(
            session,
            conversation=conversation,
            role="assistant",
            content="".join(pieces),
            error_message=str(e),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            citations=citations or None,
        )
        # 报错也要落工具记录：恰恰是"调了工具之后才炸"的场景最需要这份留痕
        await chat_service.save_tool_calls(
            session, conversation=conversation, message=message, records=tool_records
        )
        yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        return

    # 为什么攒完整再落一次库，而不是边流边写：
    #   边流边写会产生几十次 UPDATE；而且中途断开时留下的半截消息，
    #   和"正常生成完的短回复"在库里长得一模一样，历史上下文就脏了。
    message = await chat_service.save_message(
        session,
        conversation=conversation,
        role="assistant",
        content="".join(pieces),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        # 引用随消息一起落库：前端刷新页面走 GET /conversations/{id} 恢复对话时，
        # 引用来源要能跟着回来，否则刷新一次引用就没了
        citations=citations or None,
    )
    # 顺序不能反：tool_calls.message_id 指向上面这条助手消息，它得先存在
    await chat_service.save_tool_calls(
        session, conversation=conversation, message=message, records=tool_records
    )
    # 结束帧：前端收到它就关闭连接、停止 loading
    yield "data: [DONE]\n\n"


@router.post("/stream")
async def chat_stream(
    user: CurrentUser,
    req: ChatRequest,
    session: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """流式对话：POST 一条消息，以 SSE 逐块返回模型输出。

    两个开关互不依赖，可以同时开：
        use_knowledge=true  走 RAG：先检索知识库，把命中资料拼进 system 提示词，
                            并把引用来源作为**第一帧**推给前端（开关式检索）
        use_tools=true      走 Tool Calling：给模型一张工具清单，由它自己决定
                            要不要查、查什么（自主式调用）。
                            两个都开时检索会做两次（一次预检索 + 模型可能再自己查一次），
                            这是有意允许的：预检索保底，模型自行补查是加分项。
    """
    organization_id, user_id = user.organization_id, user.id

    # 校验 + 落库都必须在 return StreamingResponse 之前：
    # 一旦开始推流，HTTP 状态码就固定成 200 了，会话不存在只能塞进流里，
    # 前端拿不到 404，也没法复用统一的异常处理。
    conversation, history = await chat_service.prepare_turn(
        session,
        conversation_id=req.conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    # user 消息先落库：模型调用失败时，用户问过什么仍然留痕
    await chat_service.save_message(
        session, conversation=conversation, role="user", content=req.message
    )

    # 检索也放在推流之前，理由同上：它要查库 + 跑 embedding（秒级），
    # 出错时应该返回正常的 HTTP 错误，而不是往 SSE 流里塞一条错误帧。
    context: str | None = None
    citations: list[dict] = []
    if req.use_knowledge:
        hits = await retrieve(
            session, organization_id=organization_id, query=req.message
        )
        context, citation_objs = build_context(hits)
        citations = [c.to_payload() for c in citation_objs]
        if not context:
            # 开了知识库但一条都没命中：给个占位文本，而不是留空字符串。
            # 留空会让"用了知识库"和"没用知识库"在下游长得一样，
            # 白白丢掉"这次没查到"这个信息（前端要提示用户，模型也该说没查到）。
            context = "（没有检索到相关资料）"

    # 历史（本轮之前）+ 本轮输入 → 完整上下文。模型无状态，每轮都要重新拼。
    messages = chat_service.build_messages(
        history, req.message, context=context, use_tools=req.use_tools
    )
    # 请求态依赖由这里显式注入，不经过模型输入 —— 模型的参数里永远没有 org / session
    # role 来自认证后的用户行（8a 权限闸输入），不来自请求体。
    ctx = ToolContext(
        session=session, organization_id=organization_id, user_id=user_id,
        role=user.role,
    )

    # StreamingResponse 接收生成器，FastAPI 会不断从里面取值往连接里写，
    # 直到生成器耗尽（对应上面 yield 完 [DONE] 之后）。
    # session 在推流期间仍然可用：FastAPI 0.118+ 的依赖(yield) 在响应发完之后才退出。
    return StreamingResponse(
        _sse_generator(session, conversation, messages, citations, ctx, req.use_tools),
        media_type="text/event-stream",
    )
