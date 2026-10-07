"""发消息的业务逻辑（Phase 1）：会话校验 → 上下文组装 → 消息落库。

分层（见 docs/02-architecture.md §4）：
    路由   = 决定"校验/落库放在返回 StreamingResponse 之前还是之后"，返回什么协议
    Service= 本模块，发消息这条业务链
    Repository = 只读写数据，查不到返回 None

为什么上下文组装放在 Service 而不是路由：
    它需要"读历史 + 拼消息 + 控制条数"三件事，且 Phase 6 会换成按 token 预算裁剪。
    放路由里，将来改裁剪策略要动路由。
"""
import json
import uuid
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import pricing
from app.ai.prompts import (
    RAG_CONTEXT_FOOTER,
    RAG_CONTEXT_HEADER,
    RAG_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TOOL_RULES,
)
from app.ai.tools import registry
from app.ai.tools.base import ToolCallRecord
from app.core.exceptions import ConversationNotFoundError
from app.data.models import Conversation, Message
from app.data.repositories import conversation_repo, tool_call_repo

# 喂给模型的历史消息条数上限（不含本轮输入）。
# 为什么不喂全部：对话越长越贵，超过模型窗口会直接报错。
# Phase 1 用固定条数；Phase 6 换成按 token 预算裁剪。
HISTORY_LIMIT = 20

# tool_calls 表里 input_summary / output_summary 的长度上限。
# 为什么不限：完整结果已经落在 output_json 里了，摘要只是给列表页 /
# 人工排查看的可读版本。不截断的话，两列各存一份完整结果，
# 表体积和查询开销都翻倍。
SUMMARY_LIMIT = 500


async def prepare_turn(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[Conversation, list[Message]]:
    """校验会话并取本轮之前的历史消息。

    返回的 history 是"本轮输入之前"的消息 —— 本轮用户输入还没落库，
    由调用方负责落库，再由 build_messages 把本轮输入拼上去。
    """
    conversation = await conversation_repo.get_conversation(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    if conversation is None:
        raise ConversationNotFoundError()

    history = await conversation_repo.list_recent_messages(
        session,
        conversation_id=conversation_id,
        organization_id=organization_id,
        user_id=user_id,
        limit=HISTORY_LIMIT,
    )
    return conversation, history


def build_messages(
    history: list[Message],
    user_input: str,
    context: str | None = None,
    use_tools: bool = False,
) -> list[BaseMessage]:
    """把数据库里的历史消息 + 本轮输入，组装成模型要的消息列表。

    多轮对话靠的就是这一步：每次请求都重新拼一遍完整上下文发给模型
    （模型本身无状态，不会记得上一轮）。

    context 是 Phase 2 RAG 检索到的资料块（见 app/ai/rag/citation.py）。
    判据是 `is not None` 而不是"非空"：空字符串代表"开了知识库但一条都没命中"，
    这种情况必须仍然走 RAG 提示词（让模型说"资料中没有"），
    如果按"非空"判断就会静默降级成普通对话 —— 用户以为在查知识库，
    实际模型在自由发挥，而且从输出上看不出来。这是 RAG 最危险的失败模式。

    use_tools 为真时在 system 末尾追加工具规则（见 app/ai/prompts.TOOL_RULES）。
    追加而非替换：工具不改变回答立场，只是多一条获取事实的途径，
    和通用助手 / RAG 两套提示词都不冲突。
    """
    if context is not None:
        # 资料块拼在 system 里而不是作为一条 user 消息：
        # system 是"规则"，user 是"用户说的话"。资料是给模型的约束材料，
        # 混成 user 消息会被后续多轮历史淹没，也容易让模型以为用户在贴文档。
        system_content = "\n\n".join(
            [RAG_SYSTEM_PROMPT, RAG_CONTEXT_HEADER, context, RAG_CONTEXT_FOOTER]
        )
    else:
        system_content = SYSTEM_PROMPT
    if use_tools:
        system_content = f"{system_content}\n\n{TOOL_RULES}"

    messages: list[BaseMessage] = [SystemMessage(content=system_content)]

    for m in history:
        if m.role == "user":
            messages.append(HumanMessage(content=m.content))
        elif m.role == "assistant" and m.content:
            messages.append(AIMessage(content=m.content))
        # 空 content 的 assistant 消息跳过：模型直接报错、一个字都没生成时会落这么一条，
        # 空 content 发给部分厂商会被直接拒掉（400），留着只会让后面每轮都失败。
        # role == "system" 的历史消息也跳过：系统提示词统一由上面的 SYSTEM_PROMPT 给，
        # 否则历史里的 system 会覆盖当前提示词，改提示词就不生效了。

    messages.append(HumanMessage(content=user_input))
    return messages


async def save_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    role: str,
    content: str,
    error_message: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    citations: list[dict] | None = None,
) -> Message:
    """落一条消息，并把会话的活跃时间往前推。

    身份取自 conversation 而不是另取一份：
    会话能取到就说明身份已经校验过了，消息的身份必须和它一致。

    cost 由本条 token 经 pricing 现算（2026-10-03 接线，口径见 docs/05 落地补记）：
    未配置单价写 0、历史行不回溯。
    """
    seq = await conversation_repo.next_seq(session, conversation_id=conversation.id)
    # prompt/completion 是**整轮累计**（tool loop 每轮 usage 已在 api/chat.py 累加），
    # 所以这一条 cost 覆盖本轮全部模型调用（含工具轮的决策补全），与 agent span
    # 共用 pricing 唯一换算；未配置单价时 compute_cost 返 None → 写 0（列 NOT NULL）。
    cost = pricing.compute_cost(prompt_tokens, completion_tokens) or 0.0
    message = await conversation_repo.add_message(
        session,
        conversation_id=conversation.id,
        organization_id=conversation.organization_id,
        user_id=conversation.user_id,
        seq=seq,
        role=role,
        content=content,
        model=conversation.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        error_message=error_message,
        citations=citations,
    )
    await conversation_repo.touch_conversation(session, conversation_id=conversation.id)
    return message


def to_tool_call_row(record: ToolCallRecord) -> dict:
    """把一条工具调用记录映射成 tool_calls 表的行值。

    为什么 tool_type 要去注册表查、而不是随记录一起传下来：
        tool_type 是"工具定义"的属性，不是"这次调用"的属性。
        跟着记录传会变成每个产出 ToolCallRecord 的地方都得记得填，
        漏一个就是 NULL。在落库这一处查一次，只有一个地方要对。
    """
    result = record.result
    # 模型可能调一个不存在的工具（幻觉工具名），这时查不到定义。
    # 兜个 unknown 而不是崩掉：这条记录恰恰是排查"模型为什么乱调工具"的关键证据，
    # 落不进去反而丢线索。
    tool = registry.get_tool(record.call.name)

    # 起止时刻由 registry.execute 在调用执行器前后实测填入（见 ToolResult 的注释），
    # 这里直接用。兜底是给"没走 registry.execute 的 ToolResult"留的：
    # 那种情况下宁可给个同一时刻，也不让两列一起变成 NULL。
    finished_at = result.finished_at or datetime.now(timezone.utc)
    started_at = result.started_at or finished_at

    input_summary = json.dumps(record.call.args, ensure_ascii=False)
    if result.ok:
        output_summary = json.dumps(result.data, ensure_ascii=False, default=str)
    else:
        output_summary = f"失败：{result.error}"

    return {
        "tool_name": record.call.name,
        "tool_type": tool.tool_type if tool is not None else "unknown",
        "input_summary": input_summary[:SUMMARY_LIMIT],
        "input_json": record.call.args,
        "output_summary": output_summary[:SUMMARY_LIMIT],
        "output_json": result.data,
        "rows_returned": result.rows,
        "truncated": result.truncated,
        "status": "ok" if result.ok else "error",
        "error_message": result.error,
        "duration_ms": result.duration_ms,
        "started_at": started_at,
        "finished_at": finished_at,
        # Trace 树的父指针：Chat 侧不经过 node_guard，恒为 None（它的归属是 message）；
        # 任务侧由 node_guard 发号、产出点盖章（见 base.record_tool_call）。
        # 这一列必须由本函数统一带出，add_task_tool_calls 才认它。
        "parent_span_id": record.parent_span_id,
    }


async def save_tool_calls(
    session: AsyncSession,
    *,
    conversation: Conversation,
    message: Message,
    records: list[ToolCallRecord],
) -> int:
    """把本轮用到的工具调用记录落库。

    必须在助手消息落库**之后**调用：tool_calls.message_id 要指向它，
    而消息是流结束后才建出来的（见 tool_call_repo.add_tool_calls 的注释）。
    """
    if not records:
        return 0
    return await tool_call_repo.add_tool_calls(
        session,
        conversation_id=conversation.id,
        message_id=message.id,
        organization_id=conversation.organization_id,
        user_id=conversation.user_id,
        rows=[to_tool_call_row(r) for r in records],
    )
