"""工具调用记录的数据访问（Phase 3）。

Repository 层约定（见 docs/02-architecture.md §4）：
    只负责读写数据，不做业务判断、不抛业务异常。

为什么参数是 rows: list[dict] 而不是 list[ToolCallRecord]：
    本层不认识 AI 层的类型（和 document_repo.add_chunks 保持同一约定）。
    从 ToolCallRecord 到列值的映射放 service 层，本层只管"怎么落库"。
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import ToolCall


async def add_tool_calls(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    rows: list[dict],
) -> int:
    """批量落一轮问答里的工具调用记录，返回写入条数。

    rows 每项：tool_name / tool_type / input_summary / input_json /
              output_summary / output_json / rows_returned / truncated /
              status / error_message / duration_ms / started_at / finished_at

    为什么攒到最后一次性写、而不是每调一次工具写一条：
        和 messages 的落库时机对齐 —— 助手消息也是流结束后才写。
        提前写会带来两个问题：① message_id 还不知道（消息还没建），
        要么留空要么后面再 UPDATE 一次；② 流中途断掉时会留下一批
        挂在"不存在的消息"上的记录。攒起来一次写完，message_id 天然就有。
    """
    if not rows:
        return 0

    session.add_all(
        [
            ToolCall(
                conversation_id=conversation_id,
                message_id=message_id,
                organization_id=organization_id,
                user_id=user_id,
                tool_name=row["tool_name"],
                tool_type=row["tool_type"],
                input_summary=row.get("input_summary"),
                input_json=row.get("input_json"),
                output_summary=row.get("output_summary"),
                output_json=row.get("output_json"),
                rows_returned=row.get("rows_returned"),
                truncated=row.get("truncated", False),
                status=row["status"],
                error_message=row.get("error_message"),
                duration_ms=row.get("duration_ms"),
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
            )
            for row in rows
        ]
    )
    await session.commit()
    return len(rows)


async def add_task_tool_calls(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    task_run_id: uuid.UUID,
    trace_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    rows: list[dict],
) -> int:
    """Agent 任务侧的工具调用落库（Phase 6 审查补）：数据源同 add_tool_calls，
    只是归属从 conversation/message 换成 task/task_run/trace，供 Trace 树下钻与
    Tool Calling Success Rate 统计。

    不在此 commit：由执行层 run_task 在终态一并提交（与 agent_runs 同一时机）；
    任务失败走 _fail 时这批未提交的行随 rollback 丢弃，符合「失败的整个 run 不留半条工具记录」。
    """
    if not rows:
        return 0
    session.add_all(
        [
            ToolCall(
                trace_id=trace_id,
                task_id=task_id,
                task_run_id=task_run_id,
                # 父指针：指向产出这条记录的 Agent span（agent_runs.span_id）。
                # 值由 node_guard 在节点进入时发号、产出点盖章，本层只负责搬进列。
                parent_span_id=row.get("parent_span_id"),
                organization_id=organization_id,
                user_id=user_id,
                tool_name=row["tool_name"],
                tool_type=row["tool_type"],
                input_summary=row.get("input_summary"),
                input_json=row.get("input_json"),
                output_summary=row.get("output_summary"),
                output_json=row.get("output_json"),
                rows_returned=row.get("rows_returned"),
                truncated=row.get("truncated", False),
                status=row["status"],
                error_message=row.get("error_message"),
                duration_ms=row.get("duration_ms"),
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
            )
            for row in rows
        ]
    )
    return len(rows)


async def list_tool_calls(
    session: AsyncSession, *, conversation_id: uuid.UUID
) -> list[ToolCall]:
    """取某会话的全部工具调用，按时间正序（前端刷新后重放调用过程用）。

    为什么要用 started_at 而不是只按 created_at：
        一个请求里的所有工具调用是**攒到最后一次性写**的（见 add_tool_calls），
        所以整批 created_at 完全相同（Postgres 的 now() 取的是事务开始时间）。
        只按 created_at 排序时，同一批里"先查 A 再查 B"的顺序是随机的，
        前端刷新后重放会把调用过程显示反 —— 而顺序恰恰是链路推理的证据。
        started_at 由 duration_ms 反推得出，对**顺序执行**的工具天然是递增的，
        正好当这个批内次序用；最后再拿 tool_name 兜底保证顺序稳定。
    """
    stmt = (
        select(ToolCall)
        .where(ToolCall.conversation_id == conversation_id)
        .order_by(ToolCall.created_at, ToolCall.started_at, ToolCall.tool_name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_task_run_tool_calls(
    session: AsyncSession, *, task_run_id: uuid.UUID
) -> list[ToolCall]:
    """某次执行下的全部工具调用，按 started_at 正序 —— Trace 的 tool 腿（docs/06 §4）。

    为什么排序键用 started_at 而不是 created_at：任务侧的调用是执行过程中逐次写的，
    started_at 才是真实发生顺序；created_at 在同一事务批量写时全等（Postgres 的 now()
    取事务开始时间），按它排会把调用次序打乱 —— Trace 树的父子兄弟序就是这条链路的证据。

    这里**不按 conversation_id 过滤**：任务型调用的会话列恒 NULL，用它查会得到空集
    （既有的 list_tool_calls 是 Chat 侧专用，两者不是同一操作面）。
    """
    stmt = (
        select(ToolCall)
        .where(ToolCall.task_run_id == task_run_id)
        .order_by(ToolCall.started_at, ToolCall.tool_name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
