"""Tool loop（Phase 3）：模型 ↔ 工具的多轮循环。

一轮的三种产出：
    text       模型说给用户听的话 → 直接推前端
    usage      token 统计 → 落库
    tool_call  工具执行完的记录 → 推前端展示 + 落 tool_calls 表

为什么要有轮数上限：
    模型可能反复调同一个工具、或两个工具互相触发。
    没有上限就是一个不报错、不吃 CPU、只是永远不返回的请求 ——
    用户看到的是转圈，服务端看到的是正常的 await。

两层帧不要混淆（最容易搞错的地方）：
    StreamChunk(type="tool_calls")  llm_service 产出 —— 模型**说要**调工具
                                    只在 loop 内部消费，不往外传
    LoopChunk(type="tool_call")     本模块产出   —— 工具**已经跑完**了
                                    才是调用方要的东西（有结果、有耗时、能落库）

⚠️ 调用方注意：usage 是**每轮各报一次**的。
   一轮工具调用 = 两次模型调用 = 两次计费。累加时必须用 +=，
   用 = 赋值会把前面几轮的 token 全丢掉（表现是"用了工具的回答 token 特别少"）。
"""
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.ai.llm_service import llm_service
from app.ai.tools import registry
from app.ai.tools.base import (
    ToolCallRecord,
    ToolCallRequest,
    ToolContext,
    ToolResult,
    record_tool_call,
)


@dataclass
class LoopChunk:
    """循环产出的一块。type 与 StreamChunk 同构，外加 tool_call。"""

    type: str  # "text" | "usage" | "tool_call"
    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_call: ToolCallRecord | None = None


def _to_tool_message_text(result: ToolResult) -> str:
    """把 ToolResult 转成回填给模型的字符串。

    为什么必须是字符串：ToolMessage.content 的类型就是这么定的。
    直接把 dict 塞进 content，LangChain 在序列化时才会报错 ——
    错误点离现场很远，很难联想到是这里。

    default=str 兜底：sql_query 之类的工具会带出 Decimal / datetime
    （NUMERIC、TIMESTAMPTZ 列），它们不是 JSON 原生类型。
    此处是"模型输出"的边界，炸在这里会整轮请求失败，
    转成字符串让模型照样能读懂，比崩掉强。
    """
    if not result.ok:
        # 失败也照样回填：让模型有机会改参数重试，而不是这一轮直接断掉
        return f"工具执行失败：{result.error}"

    # 兜底 try：工具万一吐出超大整数（int→str 有 4300 位上限）等不可序列化结果，
    # json.dumps 会抛 ValueError 崩掉整轮、连错误帧都推不出。这里降级成占位文本，
    # 让这一轮照常收敛（计算器已在上游按位长度夹过，这是第二道保险）。
    try:
        body = json.dumps(result.data, ensure_ascii=False, default=str)
    except (ValueError, TypeError):
        body = "（结果过大或无法序列化，已省略）"
    if result.truncated:
        # 不告诉模型被截断了，它会拿"前 20 条"当成全部来下结论
        body += "\n（结果过长已截断，以上只是其中一部分）"
    return body


async def run(
    messages: list[BaseMessage],
    *,
    ctx: ToolContext,
    tool_names: list[str] | None = None,
    max_rounds: int = 6,
    role: str | None = None,
) -> AsyncIterator[LoopChunk]:
    """在 messages 上反复问模型，直到它不再要求调工具。messages 会被原地追加。

    role：给模型露出哪些工具按角色收口（F1 的第二道，schema 面）。
    None = 不过滤（保持旧行为）；非 None 时先按 registry.build_tools_for_role
    过滤，再与 tool_names 求交——模型根本看不见无权工具，比执行期拒更好。
    """
    if role is not None:
        tools = registry.build_tools_for_role(role)
    else:
        tools = registry.list_tools()
    if tool_names is not None:
        tools = [t for t in tools if t.name in tool_names]
    # schemas 无条件构造：它属于"拿哪些工具"，和上面的过滤是两件事。
    # 两条分支都必须产出 tools —— 默认调用（role=None）不能 NameError。
    schemas = [registry.to_openai_schema(t) for t in tools]

    for _ in range(max_rounds):
        round_text = ""
        pending: list[ToolCallRequest] | None = None

        async for chunk in llm_service.astream(messages, tools=schemas):
            if chunk.type == "text":
                round_text += chunk.text
                yield LoopChunk("text", text=chunk.text)
            elif chunk.type == "usage":
                yield LoopChunk(
                    "usage",
                    prompt_tokens=chunk.prompt_tokens,
                    completion_tokens=chunk.completion_tokens,
                )
            elif chunk.type == "tool_calls":
                # 先存下来，本轮不执行：流的末尾才拿到完整 tool_calls
                pending = chunk.tool_calls

        if not pending:
            # 模型没要求调工具 → 这轮就是最终回答。
            # 也要追加进 messages：调用方后面可能要拿整段历史继续用。
            messages.append(AIMessage(content=round_text))
            return

        # 回填前必须先把"模型说要调什么"记进历史。
        # 少了这条带 tool_calls 的 AIMessage，下一轮请求会因为没有
        # 对应的 tool_call 而拒收下面那些 ToolMessage（400）。
        messages.append(
            AIMessage(
                content=round_text,
                tool_calls=[
                    {"name": c.name, "args": c.args, "id": c.id} for c in pending
                ],
            )
        )

        for call in pending:
            # 逐个执行并**立刻**回填：一轮里模型可能同时要调三个工具，
            # 三个结果都要回到对话里，而且 tool_call_id 必须各自对上
            result = await registry.execute(call.name, call.args, ctx)
            messages.append(
                ToolMessage(
                    content=_to_tool_message_text(result),
                    tool_call_id=call.id,
                )
            )
            # 归属戳在产出点盖（见 record_tool_call）：这一条记录出自哪个 Agent span、
            # 重试的哪一次尝试，只有此刻还知道。
            yield LoopChunk("tool_call", tool_call=record_tool_call(ctx, call, result))

    # 只有"轮数被跑满"才会走到这里（正常收敛在上面 return 了）。
    # 必须补一段话给用户：不补的话前端只收到一串工具调用记录、没有答案，
    # 看起来就像回答被吞了
    notice = f"（已达工具调用轮数上限 {max_rounds}，先给出以上信息）"
    messages.append(AIMessage(content=notice))
    yield LoopChunk("text", text=notice)