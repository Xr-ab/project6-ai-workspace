"""任务态 Redis 缓存（Phase 8b T8）：纯加速面，**PG 恒真值源**。

键形 `task_state:{task_id}`（brief 定死），值 JSON：
    {"status": str, "heartbeat_at: str|None（datetime.isoformat，json 不认 datetime）,
     "summary": dict|None}
SETEX 3600（brief 定死 TTL）：即便接线点全部挂上，TTL 仍是 overlay 滞后的硬上限。

两条永不（全局约束逐字：「加速面拖死业务面是反模式」）：
- write_state **永不抛**：redis 断 / 序列化坏 / 任何异常只 logger.warning ——
  缓存写没写成，PG 那边状态都已 commit，业务面零感知。
- read_state **静默回落**：miss / redis 断 / 坏 JSON / 非 dict 值 → None，
  读侧拿到 None 就走纯 PG 路径，响应逐字段与无缓存时代一致。

诚实降级边界（留痕）：写失败或竞态窗里 cache 可能落后 PG，最长 TTL 时长；
读侧只拿它覆盖 task 摘要的 status 一位（run 明细恒取 PG），错的方向是
「轮询者早一拍看到旧状态」，不是「数据错」。终态写点（completed/failed/rejected/
sweeper 判死）全部接线，就是这个窗口在正常路径上的实际上界≈零。
"""
import json
import logging
import uuid
from datetime import datetime

from app.core.security import get_redis

logger = logging.getLogger(__name__)

# brief 定死值（逐字）
TASK_STATE_TTL_SECONDS = 3600
KEY_PREFIX = "task_state:"


def _key(task_id) -> str:
    # str(UUID) 即键尾；同时接受 UUID 与 str（调用面两种都有）
    return f"{KEY_PREFIX}{task_id}"


async def write_state(task_id, *, status: str, heartbeat_at, summary: dict | None) -> None:
    """SETEX `task_state:{task_id}` 3600s。**永不抛**：见模块 docstring 的永不①。

    heartbeat_at 接受 datetime（转 isoformat 字符串——json 不认 datetime）或 None/str。
    刻意不做「先读后写」的比较优化：SETEX 一条命令就是全部，多余往返反而在
    redis 抖动时多一个失败面。
    """
    try:
        value = json.dumps({
            "status": status,
            "heartbeat_at": (
                heartbeat_at.isoformat() if isinstance(heartbeat_at, datetime)
                else heartbeat_at
            ),
            "summary": summary,
        }, ensure_ascii=False, default=str)
        await get_redis().setex(_key(task_id), TASK_STATE_TTL_SECONDS, value)
    except Exception:  # noqa: BLE001 —— 加速面永不拖业务面，见模块 docstring
        logger.warning("task_state 缓存写入失败 task=%s（PG 已提交，不影响业务面）",
                       task_id, exc_info=True)


async def read_state(task_id) -> dict | None:
    """GET + json.loads；**任何异常/miss/坏 JSON/非 dict → None**（永不②，静默回落）。"""
    try:
        raw = await get_redis().get(_key(task_id))
    except Exception:  # noqa: BLE001 —— redis 断不是读面的错
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
