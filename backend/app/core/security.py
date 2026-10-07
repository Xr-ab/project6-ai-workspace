"""密码哈希 + JWT + refresh 白名单（Phase 8a，docs/06 §1.4/§6.1 的字面兑现）。

全模块纯函数/薄封装：不 import FastAPI、不碰 DB——
security 出的每一个决定（口令对不对、token 有效无效）都必须可在纯脚本里复现，
这也是 scratch 套件能用同一套函数直接铸测试 token 的原因。
"""
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt as pyjwt
from redis import asyncio as aioredis

from app.core.config import settings
from app.core.exceptions import (
    InvalidTokenError,
    RefreshTokenRevokedError,
    RefreshUnavailableError,
)

_ALG = "HS256"


def _require_secret() -> str:
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET 未配置：请在 backend/.env 设置（生成：python -c \"import secrets;print(secrets.token_urlsafe(48))\"）")
    return settings.jwt_secret


# ---- 口令 ----

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    # 空 hash 直接 False：dev 时代用户的 password_hash 就是 ''，
    # 把 '' 喂给 bcrypt.checkpw 会抛 ValueError——那不是"口令错"，是没设口令
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ---- Access Token（无状态 JWT：每请求只验签，不查 Redis——spec §2.2）----

def create_access_token(user_id: uuid.UUID, organization_id: uuid.UUID, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "org": str(organization_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return pyjwt.encode(payload, _require_secret(), algorithm=_ALG)


def decode_access_token(token: str) -> dict:
    try:
        return pyjwt.decode(token, _require_secret(), algorithms=[_ALG])
    except RuntimeError:
        raise  # 配置错误是人话 RuntimeError，不许伪装成"登录过期"
    except pyjwt.PyJWTError as exc:
        raise InvalidTokenError() from exc


# ---- Refresh Token（不透明随机串 + Redis 白名单：可轮转、可吊销）----

def new_refresh_token() -> str:
    # 32 字节 ≈ 256bit 熵；不透明即可——身份放 value 里，key 只按 token 查
    return secrets.token_urlsafe(32)


class RefreshStore:
    """refresh 白名单。键形 `refresh:{token}` → user_id（Global 裁定 2：
    token 不透明，无法从 key 反解用户，所以以 token 为键；吊销/轮转都是单键操作）。

    客户端由构造传入：生产用 get_redis()，测试注 fakeredis.FakeRedis——
    轮转/复用语义必须能在零容器的纯脚本里演。
    """

    def __init__(self, redis_client) -> None:
        self._r = redis_client

    async def issue(self, user_id: uuid.UUID) -> str:
        token = new_refresh_token()
        try:
            await self._r.setex(f"refresh:{token}", settings.refresh_token_ttl_days * 86400, str(user_id))
        except Exception as exc:
            raise RefreshUnavailableError() from exc
        return token

    async def consume(self, token: str) -> uuid.UUID:
        """一次性取出并删除（GETDEL = "验在 + 轮转删旧" 的原子合体）。

        GETDEL 而非 GET+DEL：并发双刷同一 token 时，原子语义保证恰一个赢家，
        输家拿 None → 401002 —— 和"旧 token 复用"同一条路，不用另加锁。
        """
        try:
            val = await self._r.getdel(f"refresh:{token}")
        except Exception as exc:
            raise RefreshUnavailableError() from exc
        if val is None:
            raise RefreshTokenRevokedError()
        try:
            return uuid.UUID(val)
        except (ValueError, TypeError) as exc:
            raise RefreshTokenRevokedError() from exc

    async def revoke(self, token: str) -> None:
        """登出用。不存在也静默成功（幂等）：登出要的是"它现在无效"，不是"它刚才存在"。"""
        try:
            await self._r.delete(f"refresh:{token}")
        except Exception as exc:
            raise RefreshUnavailableError() from exc


_redis_client = None


def get_redis():
    """进程级 redis 客户端（decode_responses=True：白名单值就是纯字符串）。"""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client
