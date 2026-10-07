"""鉴权依赖（06 §1.4 点名的三件）：get_current_user / get_current_org / require_role。"""
import uuid
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidTokenError, RolePermissionError
from app.core.security import decode_access_token
from app.data.db import get_db
from app.data.models import User
from app.data.repositories import user_repo

_bearer = HTTPBearer(auto_error=False)
# auto_error=False：FastAPI 默认对缺失头抛 HTTPException(403)——那是框架体，
# 没有业务 code，前端按 code 分支就漏判。这里自己收，统一走 AUTH_401001 信封。


async def get_current_user(
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if cred is None or not cred.credentials:
        raise InvalidTokenError("未登录")
    payload = decode_access_token(cred.credentials)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError() from exc
    user = await user_repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        raise InvalidTokenError()
    if str(user.organization_id) != payload.get("org"):
        # token 声称的组织与库内归属对不上：只可能是伪造或数据异常
        raise InvalidTokenError()
    return user


def get_current_org(user: Annotated[User, Depends(get_current_user)]) -> uuid.UUID:
    return user.organization_id


def require_role(role: str):
    """角色闸。v1 真实使用点 = GET /auth/audit-log（admin-only）——机制 + 一针，不造管理面。"""
    async def _dep(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role != role:
            raise RolePermissionError()
        return user
    return _dep


CurrentUser = Annotated[User, Depends(get_current_user)]
