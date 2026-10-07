"""审计日志读写（Phase 8a）。列表只按组织过滤：跨组织探测防护沿用全局 404 同形口径。"""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import AuditLog


async def list_for_org(
    session: AsyncSession, *, organization_id: uuid.UUID, page: int, page_size: int
) -> tuple[list[AuditLog], int]:
    total = (
        await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.organization_id == organization_id)
        )
        or 0
    )
    rows = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.organization_id == organization_id)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
    )
    return rows, total
