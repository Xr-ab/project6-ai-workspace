"""Auth API（Phase 8a）：06 §2.1 的六端点，路由只做「取参 → 调 service → 选响应模型」。

公开面（06 §1.4）：register / login / refresh；logout 需带 access token
（吊销动作要记到登出者名下——06 §1.4 实现注）；me / audit-log 走鉴权依赖。

get_refresh_store 是本文件唯一的机制接缝：生产给真 Redis，测试用
`app.dependency_overrides[get_refresh_store]` 换 fakeredis 实例——
service 收 store 进参（Task 2 同款），路由层不 import 任何 mock 开关。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_role
from app.application import auth_service
from app.core.config import settings
from app.core.rate_limit import rate_limit_dep, rate_limit_login_dep
from app.core.security import RefreshStore, get_redis
from app.data.db import get_db
from app.data.models import User
from app.data.repositories import audit_repo
from app.schemas.auth import (
    AuditLogPage,
    AuthOut,
    ChangePasswordIn,
    LoginIn,
    LogoutIn,
    LogoutOut,
    ProfileUpdateIn,
    RefreshIn,
    RegisterIn,
    TokenPairOut,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_db)]


async def get_refresh_store() -> RefreshStore:
    return RefreshStore(get_redis())


StoreDep = Annotated[RefreshStore, Depends(get_refresh_store)]

# 8b T9 登录面限流（spec §6 表第一行：爆破/枚举——401001 同码防了探测防不了速刷）。
# IP+email 双键；上限进 Settings，lambda 是测试接缝（套件垫高它，生产默认 10/min 不动）。
_rl_auth = rate_limit_login_dep("auth", lambda: settings.rate_limit_auth_per_min)

# 身份写口（Phase 9a）单独一个桶（rl:pwd:{uid}）：**同档不同桶**（spec §3.3）——
# 与 login 同上限（settings.rate_limit_auth_per_min），但键空间不同，
# 免得"在 Settings 改三次口令"就把登录面刷成 429（那是自伤，不是防护）。
# 裁定：PATCH /me 也挂此桶——两个写口是同一个"改自己账号"操作面，
# 分两个桶等于给了双份额度，而 §3.3 的意图是给写口加闸，不是加额度。
_rl_pwd = rate_limit_dep("pwd", lambda: settings.rate_limit_auth_per_min)


@router.post("/register", response_model=AuthOut, dependencies=[Depends(_rl_auth)])
async def register(body: RegisterIn, session: SessionDep, store: StoreDep):
    return await auth_service.register(session, store, body=body)


@router.post("/login", response_model=AuthOut, dependencies=[Depends(_rl_auth)])
async def login(body: LoginIn, session: SessionDep, store: StoreDep):
    return await auth_service.login(session, store, body=body)


@router.post("/refresh", response_model=TokenPairOut)
async def refresh(body: RefreshIn, session: SessionDep, store: StoreDep):
    return await auth_service.refresh(session, store, refresh_token=body.refresh_token)


@router.post("/logout", response_model=LogoutOut)
async def logout(body: LogoutIn, store: StoreDep, user: CurrentUser):
    await auth_service.logout(store, user=user, refresh_token=body.refresh_token)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser, session: SessionDep):
    return await auth_service.get_profile(session, user=user)


@router.patch("/me", response_model=UserOut, dependencies=[Depends(_rl_pwd)])
async def patch_me(
    user: CurrentUser,
    payload: ProfileUpdateIn,
    session: SessionDep,
) -> UserOut:
    updated = await auth_service.update_profile(session, user=user, full_name=payload.full_name)
    # R27：返回体走 get_profile，与 GET /me 同形状（organization_name 必带）。
    # 用 UserOut.model_validate(updated) 会让同名响应模型在两个动词下给出不同字段值，
    # Task 9 的 Settings 改完名把结果 applyUser 进 auth store 时组织名会白屏一下。
    return await auth_service.get_profile(session, user=updated)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_rl_pwd)])
async def change_password(
    user: CurrentUser,
    payload: ChangePasswordIn,
    session: SessionDep,
) -> None:
    await auth_service.change_password(
        session, user=user, old_password=payload.old_password, new_password=payload.new_password
    )


@router.get("/audit-log", response_model=AuditLogPage)
async def audit_log(
    session: SessionDep,
    user: Annotated[User, Depends(require_role("admin"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    rows, total = await audit_repo.list_for_org(
        session, organization_id=user.organization_id, page=page, page_size=page_size
    )
    return {"items": rows, "total": total, "page": page, "page_size": page_size}
