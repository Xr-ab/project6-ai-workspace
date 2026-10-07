"""只读统计面（Phase 9a，契约见 docs/06 §2.8）。

两个 GET，无副作用、无写库 —— 所以本文件只有「取身份 → 调 service → 装响应模型」。
限流：spec §3.1 裁定 stats 不加额外逻辑；既有全局中间件与 429 面不动（聚合是 SQL 活，
打满的是库而不是 Redis，靠 stats 限流挡不住真问题）。
"""
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.application import stats_service
from app.data.db import get_db
from app.schemas.stats import RangeKey, StatsOverviewOut, StatsUsageOut

router = APIRouter(prefix="/stats", tags=["stats"])

# range 的合法值**不在这里再抄一遍**：用 `schemas/stats.py` 的 `RangeKey`。
# reports 列表口现在也接同一个参数，两处各写一份 Literal 的话，改一处忘一处
# 不会报错，只会让"近 7 天"在两个页面上变成两个意思。
GroupByParam = Literal["task_type", "model", "day"]


@router.get("/overview", response_model=StatsOverviewOut)
async def get_overview(
    user: CurrentUser,
    range: RangeKey = Query("today", description="时间窗（滚动窗，today=本地日 00:00 起）"),
    session: AsyncSession = Depends(get_db),
) -> StatsOverviewOut:
    return await stats_service.overview(session, user=user, range_key=range)


@router.get("/usage", response_model=StatsUsageOut)
async def get_usage(
    user: CurrentUser,
    range: RangeKey = Query("today"),
    group_by: GroupByParam = Query("task_type"),
    session: AsyncSession = Depends(get_db),
) -> StatsUsageOut:
    return await stats_service.usage(session, user=user, range_key=range, group_by=group_by)
