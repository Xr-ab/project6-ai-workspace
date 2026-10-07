"""request_id 上下文 + 审计落库件（Phase 8a）。

write_audit 自己开一条 AsyncSessionLocal、自己 commit：
    业务 session 往往正卡在事务中间（登录要写 users、审批要条件 UPDATE），
    复用它 = 审计写入替业务把未完成的活儿提前 commit（Phase 5 事务污染同款坑）。
    审计与业务各记各的账，业务回滚也不该把"发生过这件事"抹掉。

write_audit 永不抛错：审计是留痕面不是交易面，它挂了不能把登录/审批带崩。
丢行降级为一条 error 日志（Phase 8b 结构化制式：JSON 行、携本请求 request_id，
与日志面按 request_id 互对账）。
"""
import contextvars
import logging
import uuid

from app.data.db import AsyncSessionLocal
from app.data.models import AuditLog

logger = logging.getLogger(__name__)

# 每请求一个 id（06 §1.3）。ContextVar 而不是把 request 一路传参：
# 埋点散在 service/registry 深处，穿参数会污染所有中间层签名。
REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def get_request_id() -> str | None:
    return REQUEST_ID.get() or None


async def write_audit(
    *,
    organization_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
) -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                AuditLog(
                    organization_id=organization_id,
                    user_id=user_id,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    detail=detail,
                    request_id=get_request_id(),
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 刻意兜一切：见模块 docstring
        # brief 原文此行为 logging.getLogger(__name__).error(...)：模块头已备
        # logger = logging.getLogger(__name__)，两处同物，收进模块级引用免重复构造。
        logger.error("audit 写入失败（业务不受影响）: %s: %s", type(exc).__name__, exc)
