"""users 表读写（Phase 8a）。邮箱查询大小写不敏感：注册和登录必须用同一把归一化尺子。"""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import Organization, User


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )


async def create_org_with_admin(
    session: AsyncSession, *, organization_name: str, email: str, password_hash: str, full_name: str
) -> tuple[Organization, User]:
    org = Organization(name=organization_name)
    session.add(org)
    await session.flush()  # 拿 org.id 给用户行做外键
    user = User(
        organization_id=org.id, email=email.strip().lower(),
        password_hash=password_hash, full_name=full_name,
        role="admin", is_active=True,
    )
    session.add(user)
    await session.commit()
    return org, user
