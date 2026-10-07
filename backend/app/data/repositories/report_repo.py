"""报告仓储（Phase 9b）：reports 表的读写，只做数据访问不含业务（docs/02 §4）。

隔离口径与全仓一致（docs/05 §1.4）：**每个查询都显式带 `organization_id` 谓词**，
不引 PG RLS、也不写自动过滤中间件 —— 理由与 Phase 8a 的裁定同源（隐式魔法一旦有人
绕过 session 拿连接就静默失效，且不好测）。跨 org 取一条 → 调用方按"不存在"处理。

可见性分档沿用 documents 的 `_doc_scope` 先例（8b T10 R-T5b）：
    member → 只看自己的（列表带 user_id、按 id 取或删他人 → 同形 404）
    admin  → 本组织全量（`user_id=None`）
分层理由与审计页/文档页同一套：admin 是运维面，member 是数据面。
"""
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import Report


async def list_reports(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    report_type: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Report]:
    """本组织报告列表，最近创建在前（`ix_reports_org_created` 正是这个形态）。

    `user_id=None` 表示不过滤到人（admin 档）；member 档传自己的 id。
    与审计面 `audit_repo.list_for_org` 的差别在这里：那边只有 org 一档，
    本表的可见性有"我的 / 全组织"两档，所以 user_id 是可选谓词而不是硬编码。

    `since` 是时间窗下界（含），由 service 从 `range` 算出来（口径见
    `stats_repo.range_start`，本层不认识 `today`/`week` 这些词）。索引
    `ix_reports_org_created` 就是 `(organization_id, created_at DESC)`，
    带 `since` 的查询正好走它。
    """
    stmt = select(Report).where(Report.organization_id == organization_id)
    if user_id is not None:
        stmt = stmt.where(Report.user_id == user_id)
    if report_type:
        stmt = stmt.where(Report.report_type == report_type)
    if since is not None:
        stmt = stmt.where(Report.created_at >= since)
    stmt = stmt.order_by(Report.created_at.desc(), Report.id.desc()).limit(limit).offset(offset)
    return list(await session.scalars(stmt))


async def count_reports(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    report_type: str | None = None,
    since: datetime | None = None,
) -> int:
    """列表同口径的计数（前端「共 N 份」用；与 list_reports 的谓词必须同源）。

    为什么单独一个函数而不是让调用方自己算 len(rows)：列表是分页的，
    拿当页长度当总数是错的 —— 这是分页接口最常见的谎。
    反过来说，加了 `since` 却忘了在这里也加，是同一类谎的另一种写法：
    数字看着对，翻到第二页才发现"共 3 份"里有 2 份不在窗内。
    两条签名的一致性由 `tests/unit/test_report_layer.py` 的签名对账针钉住。
    """
    stmt = select(func.count()).select_from(Report).where(Report.organization_id == organization_id)
    if user_id is not None:
        stmt = stmt.where(Report.user_id == user_id)
    if report_type:
        stmt = stmt.where(Report.report_type == report_type)
    if since is not None:
        stmt = stmt.where(Report.created_at >= since)
    return await session.scalar(stmt) or 0


async def get_report(
    session: AsyncSession,
    *,
    report_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> Report | None:
    """按 id 取一条（带 org 谓词；user_id 给了就一并收窄）。取不到返回 None。

    返回 None 而不是抛 `ReportNotFoundError`：仓储层不认 HTTP 语义（docs/02 §4 分层），
    "取不到算不算错误"是业务判断，归 service。
    """
    stmt = select(Report).where(
        Report.id == report_id, Report.organization_id == organization_id
    )
    if user_id is not None:
        stmt = stmt.where(Report.user_id == user_id)
    return await session.scalar(stmt)


async def get_by_task_run(
    session: AsyncSession, *, task_run_id: uuid.UUID, organization_id: uuid.UUID
) -> Report | None:
    """按来源执行取报告（Task 详情页「看报告」入口）。

    一条 run 最多一份报告（写入方在终态落库时建一次），所以 scalar 取一条即可；
    真出现重复行也不是这里要修的 —— 那说明写侧被调了两次，用 `created_at desc` 取最新一份
    而不是抛错，让页面仍可用（写侧的唯一性由调用点保证，不在这里加约束：
    重建报告是将来可能有的能力，硬唯一约束会把它堵死）。
    """
    stmt = (
        select(Report)
        .where(Report.task_run_id == task_run_id, Report.organization_id == organization_id)
        .order_by(Report.created_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def delete_report(session: AsyncSession, report: Report) -> None:
    """删一条（调用方已按 org/user 档取到行才调这里）。

    只删报告行：来源 task / task_run 与 Trace 一行不动 —— 报告是可再生成的投影，
    执行痕迹是不可再生成的事实（本表 docstring 那条分工）。`tasks.report_id` 由
    外键的 ON DELETE SET NULL 自动放开，不用手工清。
    """
    await session.delete(report)
