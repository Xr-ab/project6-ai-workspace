"""Auth 业务层（Phase 8a）：register / login / refresh / logout。

分层约定同其余 service：HTTP 细节不进来，session 与 store 从外面进（测试注
fakeredis 版 RefreshStore 就能全链路演，不起 Redis 也不 mock 内部）。

审计口径（Task 3 裁定兑现）：
- write_audit 永远自己开会话、永不抛——这里逐事件 fire-and-forget await 即可，
  不需要 try，也不该共享业务 session（业务回滚不能抹掉「发生过这件事」）。
- login 的错密码与未知邮箱返回**同一个** InvalidTokenError（AUTH_401001/401）：
  能区分「邮箱没注册」和「密码错」= 免费送出一个账号枚举 oracle。

日志口径（8b T2）：logger.info 只贴已有 write_audit 埋点的事件面，不新增业务事件（8a 七个 +
9a 的 profile_update / password_change = 九个）；
身份（org/user）进 msg 结构字段（key=value），request_id 由 JSON handler 的 Filter
从中间件 contextvar 注入——日志行与 audit 行按它互对账（spec §7）。
"""
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    InvalidTokenError,
    OldPasswordIncorrectError,
    RefreshTokenRevokedError,
)
from app.core.security import RefreshStore, create_access_token, hash_password, verify_password
from app.data.models import Organization, User
from app.data.repositories import user_repo
from app.schemas.auth import LoginIn, RegisterIn, UserOut

logger = logging.getLogger(__name__)


async def _issue_pair(store: RefreshStore, user: User) -> dict:
    return {
        "access_token": create_access_token(user.id, user.organization_id, user.role),
        "refresh_token": await store.issue(user.id),
    }


async def register(session, store, *, body: RegisterIn) -> dict:
    if await user_repo.get_by_email(session, body.email) is not None:
        raise EmailAlreadyRegisteredError()
    try:
        org, user = await user_repo.create_org_with_admin(
            session, organization_name=body.organization_name, email=body.email,
            password_hash=hash_password(body.password), full_name=body.full_name)
    except IntegrityError:
        # F2（8b T10）：预检是「先查后插」，两步之间并发到达的同邮箱请求会把唯一
        # 约束留到最后 commit 才引爆——那才是真撞车。裸抛 = 500；语义应是 409。
        # rollback 让同事务里 flush 过的 org 行一并回收，不留孤儿组织。
        # （单测针是串行等价，真并发窗归 exit 压测可选——Task 10 brief 裁定原话。）
        await session.rollback()
        raise EmailAlreadyRegisteredError() from None
    await write_audit(organization_id=org.id, user_id=user.id, action="register",
                      target_type="organization", target_id=str(org.id))
    logger.info("auth event=register org=%s user=%s", org.id, user.id)
    pair = await _issue_pair(store, user)
    return {**pair, "user": UserOut.model_validate(user)}


async def login(session, store, *, body: LoginIn) -> dict:
    user = await user_repo.get_by_email(session, body.email)
    if user is None or not verify_password(body.password, user.password_hash):
        # 归因两分（8b T10 R-T4b 收口）：查得到用户 → org 归因照落（错密码爆破
        # 自此上 admin 审计页，可被组织管理员看见）；查不到用户 → org 无从而来，
        # 诚实 NULL（05 表设计里的可空语义），不往任何组织名下塞假事件。
        await write_audit(organization_id=user.organization_id if user else None,
                          user_id=user.id if user else None, action="login_failed")
        logger.info("auth event=login_failed user=%s", user.id if user else None)
        raise InvalidTokenError("邮箱或密码不正确")
    if not user.is_active:
        await write_audit(organization_id=user.organization_id, user_id=user.id,
                          action="login_failed", detail={"reason": "inactive"})
        logger.info("auth event=login_failed reason=inactive org=%s user=%s",
                    user.organization_id, user.id)
        raise InvalidTokenError("邮箱或密码不正确")  # 禁用账号不给独立错误码：探测防护同 404 口径
    await write_audit(organization_id=user.organization_id, user_id=user.id, action="login")
    logger.info("auth event=login org=%s user=%s", user.organization_id, user.id)
    pair = await _issue_pair(store, user)
    return {**pair, "user": UserOut.model_validate(user)}


async def refresh(session, store, *, refresh_token: str) -> dict:
    try:
        user_id = await store.consume(refresh_token)
    except RefreshTokenRevokedError:
        await write_audit(action="refresh_reuse", target_type="refresh_token",
                          target_id=refresh_token[:8],
                          detail={"why": "白名单缺键：已轮转/已登出/从未签发"})
        logger.info("auth event=refresh_reuse target=refresh_token prefix=%s",
                    refresh_token[:8])
        raise
    user = await user_repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        raise InvalidTokenError()
    await write_audit(organization_id=user.organization_id, user_id=user.id, action="refresh")
    logger.info("auth event=refresh org=%s user=%s", user.organization_id, user.id)
    return await _issue_pair(store, user)


async def logout(store, *, user: User, refresh_token: str) -> None:
    await store.revoke(refresh_token)
    await write_audit(organization_id=user.organization_id, user_id=user.id, action="logout")
    logger.info("auth event=logout org=%s user=%s", user.organization_id, user.id)


async def update_profile(session: AsyncSession, *, user: User, full_name: str) -> User:
    """只改 full_name。白名单在签名上就已经收紧（参数不是 dict）。"""
    user.full_name = full_name
    await session.commit()
    await write_audit(
        organization_id=user.organization_id,
        user_id=user.id,
        action="user.profile_update",
        target_type="user",
        target_id=str(user.id),
        detail={"full_name_length": len(full_name)},
    )
    logger.info("auth event=profile_update org=%s user=%s", user.organization_id, user.id)
    return user


async def change_password(
    session: AsyncSession, *, user: User, old_password: str, new_password: str
) -> None:
    """验旧口令 → 换新 hash。

    旧会话**不强杀**，这是实况不是缺陷：access 是无状态 JWT（验签即通过），
    refresh 存的是 `refresh:{随机token} → user_id`（core/security.py:92），
    没有按用户枚举会话的反向索引 —— 想做也做不了，spec §9 已登记为待改版项。
    口令不写日志（只记长度/事件），失败计数靠限流闸，不在这层造。
    """
    if not verify_password(old_password, user.password_hash):
        raise OldPasswordIncorrectError()
    user.password_hash = hash_password(new_password)
    await session.commit()
    await write_audit(
        organization_id=user.organization_id,
        user_id=user.id,
        action="user.password_change",
        target_type="user",
        target_id=str(user.id),
    )
    logger.info("auth event=password_change org=%s user=%s", user.organization_id, user.id)


async def get_profile(session: AsyncSession, *, user: User) -> UserOut:
    """me 的响应：一次 join 带出组织名（不为它单开端点，R6）。"""
    org_name = await session.scalar(
        select(Organization.name).where(Organization.id == user.organization_id)
    )
    out = UserOut.model_validate(user)
    out.organization_name = org_name
    return out
