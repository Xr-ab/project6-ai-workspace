"""Workflow 编目 / 审批的数据访问（Phase 7 Task 6 起步，Task 7 读侧端点续住这里）。

为什么单独建文件而不塞进 task_repo：task_repo 的模块头把地盘写死为
「任务 / 执行 / agent span」，workflow_approvals 是 Phase 7 新表、审批语义独立
（pending→approved/rejected 的条件更新闸门），且 Task 7 的审批列表也要住这里 ——
另起一屋比在旧屋里越界搭台好。

隔离规矩与 task_repo 同源：workflow_approvals 自己没有 org/user 列（挂在 task 下），
所有查询 JOIN / IN 回 tasks 做 org+user 双过滤（Task 1/2 读侧口径），不赌调用方干净。
"""
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import Task, Workflow, WorkflowApproval


# ---------------- 编目读（Task 7 端点组的归属闸） ----------------


async def list_workflows(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Sequence[Workflow]:
    """本 org 的可用编目（is_active 才外显）；创建序在前、graph_key 稳序兜底。

    workflows 表没有 user 列（Task 3 建表即 org 级共享目录：同 org 全员见同一组
    预置图），所以这里的归属过滤只有 org 一维 —— 「org+user 双过滤」是审批面
    （workflow_approvals 经 tasks）的口径，不适用于编目，签名不假装有 user。
    """
    stmt = (
        select(Workflow)
        .where(Workflow.organization_id == organization_id, Workflow.is_active.is_(True))
        .order_by(Workflow.created_at, Workflow.graph_key)
    )
    return list(await session.scalars(stmt))


async def get_workflow(
    session: AsyncSession, *, workflow_id: uuid.UUID, organization_id: uuid.UUID
) -> Workflow | None:
    """按 id 取编目行（org 归属过滤；停用行按不存在处理）。

    trigger 路由的归属闸（服务侧不校验 workflow.organization_id，见其 docstring）：
    「不存在」「别的 org」「已停用」三种输入在此同返 None → 同一个 404，
    不给存在性探测留缝。返回行绑调用方会话，可直接传给 workflow_service.trigger。
    """
    stmt = select(Workflow).where(
        Workflow.id == workflow_id,
        Workflow.organization_id == organization_id,
        Workflow.is_active.is_(True),
    )
    return await session.scalar(stmt)


async def get_approval(
    session: AsyncSession,
    *,
    approval_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> WorkflowApproval | None:
    """按 id 取一条审批（org+user 归属过滤）。查不到 → None。

    「不存在」与「属于别人」在这里返回值完全同形（都是 None），由 service 层
    翻成同一个 404 —— 不给靠状态码差异探测审批 id 存在性留缝（同 CONV/DOC 口径）。
    """
    stmt = (
        select(WorkflowApproval)
        .join(Task, Task.id == WorkflowApproval.task_id)
        .where(
            WorkflowApproval.id == approval_id,
            Task.organization_id == organization_id,
            Task.user_id == user_id,
        )
    )
    return await session.scalar(stmt)


async def decide_pending_approval(
    session: AsyncSession,
    *,
    approval_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    decision: str,
    decided_by: uuid.UUID,
    comment: str | None = None,
) -> WorkflowApproval | None:
    """竞态守卫的原子决策：条件 UPDATE ... WHERE status='pending' RETURNING。

    不许读后写裸改（Task 7 修复波经验）：两个并发 decide_approval 各自过完读检查再写，
    就是双 resume、双续跑段。本语句让 Postgres 来裁：
      - 行锁串行化两个 UPDATE；
      - READ COMMITTED 下输者拿锁后**重新评估** WHERE（status 已是 approved/rejected）
        → 命中 0 行 → 返回 None → service 抛 ApprovalConflictError(409)。
    decision 即 approval.status 的新值（approved/rejected），调用方已归一化；
    归属过滤（org+user）内联在 WHERE 里 —— 这是第三道防线，与 get_approval 同源口径。
    不 commit：事务边界留给 service（决策 + 任务状态推进要同批生效）。
    """
    stmt = (
        update(WorkflowApproval)
        .where(
            WorkflowApproval.id == approval_id,
            WorkflowApproval.status == "pending",
            WorkflowApproval.task_id.in_(
                select(Task.id).where(
                    Task.organization_id == organization_id,
                    Task.user_id == user_id,
                )
            ),
        )
        .values(
            status=decision,
            decided_by=decided_by,
            decided_at=datetime.now(timezone.utc),
            comment=comment,
        )
        .returning(WorkflowApproval)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_approvals(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Sequence[WorkflowApproval]:
    """某任务的审批列表：pending 在前，同组内创建序在后（待办顶到队首）。

    审批人=当前 org+user（Task 2 口径，多用户审批角色不在 Phase 7 范围）：
    approvals 无归属列，经 tasks JOIN 拿双过滤 —— 与 get_approval 同一隔离规矩。
    """
    stmt = (
        select(WorkflowApproval)
        .join(Task, Task.id == WorkflowApproval.task_id)
        .where(
            WorkflowApproval.task_id == task_id,
            Task.organization_id == organization_id,
            Task.user_id == user_id,
        )
        .order_by(
            case((WorkflowApproval.status == "pending", 0), else_=1),
            WorkflowApproval.created_at,
            WorkflowApproval.id,
        )
    )
    return list(await session.scalars(stmt))
