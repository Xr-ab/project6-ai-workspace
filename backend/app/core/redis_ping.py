"""healthz 用的 redis 探活：只回答「通不通」，永不抛。

503 语义属于 refresh 面（RefreshUnavailableError），不属于 healthz——
探活件把挂了报成 False 就是如实降级，抛出反而会把整个 /healthz 打成 500。
"""
from app.core.security import get_redis

# arq 默认队列键（arq/constants.py: default_queue_name = 'arq:queue'）。
# 键名以本机实测为准，见 ping_queue docstring 的留痕。
ARQ_QUEUE_KEY = "arq:queue"


async def ping_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:  # noqa: BLE001 —— 探活不许抛，见模块 docstring
        return False


async def ping_queue() -> bool:
    """arq 队列探针（8b spec §3.3）：只回答「Redis 当 broker 通不通」。

    与 ping_redis 同一条连接、同一套纪律：队列空也是 True，够不着才是 False。
    不证明 worker 活着（那要读 arq 自己的 `arq:queue:health-check` 哨兵，
    空跑 worker 也会写；单独一档属 Task 12 出口的事）。

    实测留痕（arq 0.28.0——requirements 下限写 >=0.26，实装取到 0.28）：
    - 起了但零入队的 worker：只写 `arq:queue:health-check`（TTL 哨兵），
      队列键本身尚未诞生；
    - 真入队一条后：`arq:queue` + `arq:job:{job_id}`，且 **`arq:queue` 实测是
      zset 不是 list**（connections.py:175 `pipe.zadd(queue, {job_id: score})`，
      LLEN 直接 WRONGTYPE）——计划文档写错了类型，实测为准，
      对质真实键形后按实测修正。
    故探针按 TYPE 分派计数命令（zset→ZCARD / list→LLEN / none→队列空）：
    键类型随 arq 版本漂移不关探针的事，它只回答可达性。
    """
    try:
        r = get_redis()
        key_type = await r.type(ARQ_QUEUE_KEY)
        if key_type == "zset":  # get_redis() decode_responses=True，比较 str
            await r.zcard(ARQ_QUEUE_KEY)
        elif key_type == "list":  # 计划文档把类型写错了（见上留痕，实测为准），
            await r.llen(ARQ_QUEUE_KEY)  # 留兜底防版本漂移，实装 0.28 走不到这枝
        # key_type == "none"：键还没诞生 = 队列空，可达性已由 TYPE 命令证明
        return True
    except Exception:  # noqa: BLE001 —— 探活不许抛，同 ping_redis
        return False
