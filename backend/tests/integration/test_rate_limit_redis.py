"""滑窗闸的集成面（8b T9，实读 rate_limit.py:49）。

fakeredis 演不了这一层，因为它给不了真 redis 的两件事：
① ZSET 跨会话过期（expire 兜底那条），② 真 socket 断开 → OSError → 503 这条染色路径。
键形 rl:{bucket}:{identity} 逐字沿用；bucket 一律 p6test- 前缀（spec §5.1），
所以这些键即便留在库里也只属于集成层。
"""
import asyncio
import uuid

import pytest
from redis import asyncio as aioredis

from app.core import rate_limit
from app.core.exceptions import RateLimitedError, RateLimitUnavailableError
from app.core.security import get_redis

pytestmark = pytest.mark.redis


async def _zcard(identity: str, bucket: str) -> int:
    return int(await get_redis().zcard(f"rl:{bucket}:{identity}"))


def _identity() -> str:
    return f"p6test-{uuid.uuid4().hex}"


async def test_in_window_counter_tracks_calls():
    ident = _identity()
    for _ in range(3):
        await rate_limit.enforce(identity=ident, bucket="p6test-task", limit=5)
    assert await _zcard(ident, "p6test-task") == 3


async def test_over_limit_raises_429_with_a_usable_retry_after():
    ident = _identity()
    for _ in range(3):
        await rate_limit.enforce(identity=ident, bucket="p6test-task", limit=3)
    with pytest.raises(RateLimitedError) as excinfo:
        await rate_limit.enforce(identity=ident, bucket="p6test-task", limit=3)
    assert excinfo.value.status_code == 429
    assert excinfo.value.code == "COMMON_429001"
    # retry_after 是给 Retry-After 头的秒数：必须落在 [1, window] 内，
    # 0 会让前端显示"约 0 秒后可再试"，>window 在滑窗语义下不可能
    assert 1 <= excinfo.value.retry_after <= rate_limit.DEFAULT_WINDOW_S


async def test_rejected_request_stays_counted():
    """**拒绝不撤销**（模块 docstring 的刻意不对称）：被拒那一次的 zadd 留在窗内，
    爆破重试只会自然变慢。若哪天有人"好心"回滚，这条会红。"""
    ident = _identity()
    await rate_limit.enforce(identity=ident, bucket="p6test-auth", limit=1)
    with pytest.raises(RateLimitedError):
        await rate_limit.enforce(identity=ident, bucket="p6test-auth", limit=1)
    assert await _zcard(ident, "p6test-auth") == 2


async def test_buckets_and_identities_are_separate_ledgers():
    """2 桶语义：换桶不续命（task 耗尽不影响 upload），换身份不续命（同桶各自记账）。"""
    ident, other = _identity(), _identity()
    await rate_limit.enforce(identity=ident, bucket="p6test-upload", limit=1)
    with pytest.raises(RateLimitedError):
        await rate_limit.enforce(identity=ident, bucket="p6test-upload", limit=1)
    # 同 identity 换桶：额度重新算（upload 耗尽挡不住 auth）
    await rate_limit.enforce(identity=ident, bucket="p6test-auth", limit=1)
    # 同桶换 identity：不受影响
    await rate_limit.enforce(identity=other, bucket="p6test-upload", limit=1)
    assert await _zcard(other, "p6test-upload") == 1


async def test_sliding_window_lets_the_oldest_entry_out():
    """滑窗的「滑」由 zremrangebyscore 兑现：window_s=1 ⇒ 等 1.4 秒后额度自然回来。
    这条针刻意用极短 window_s 而不是 sleep 60s：判据是"窗口会滑"，与窗口多长无关。"""
    ident = _identity()
    await rate_limit.enforce(identity=ident, bucket="p6test-task", limit=1, window_s=1)
    with pytest.raises(RateLimitedError):
        await rate_limit.enforce(identity=ident, bucket="p6test-task", limit=1, window_s=1)
    await asyncio.sleep(1.4)
    await rate_limit.enforce(identity=ident, bucket="p6test-task", limit=1, window_s=1)


async def test_redis_down_is_503_not_silently_allowed(monkeypatch):
    """宕机口径（spec §6 字面）：受闸面一律 503 COMMON_503001，**不做挂了就放行**。
    宕机窗口就是爆破窗口，静默放行是反向假安全。
    端口 6399 刻意不是 6379：那台机器上没人监听，connect 立刻 refused，
    而它也不构成"守卫要挡的默认端口"（守卫只管 settings.redis_url）。"""
    dead = aioredis.from_url("redis://localhost:6399/0", socket_connect_timeout=0.3)
    # 打在**消费者**名字上：rate_limit.py:38 是 from ... import get_redis，
    # 改 app.core.security.get_redis 对它无效（Task 2 假 embedder 同一条教训）
    monkeypatch.setattr(rate_limit, "get_redis", lambda: dead)
    with pytest.raises(RateLimitUnavailableError) as excinfo:
        await rate_limit.enforce(identity=_identity(), bucket="p6test-task", limit=5)
    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "COMMON_503001"
    await dead.aclose()


async def test_programming_error_is_not_dyed_as_503(monkeypatch):
    """异常族窄化（T9 评审轮）：只有 RedisError/OSError 染 503。
    拿一个连 .pipeline 都没有的对象进去 ⇒  AttributeError 必须外抛，
    不许被伪装成"依赖不可用"—— 编程错误得响亮。"""
    monkeypatch.setattr(rate_limit, "get_redis", lambda: object())
    with pytest.raises(AttributeError):
        await rate_limit.enforce(identity=_identity(), bucket="p6test-task", limit=5)
