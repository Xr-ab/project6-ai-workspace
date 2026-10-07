"""Dashboard 统计聚合（Phase 9a，docs/06 新增的 stats 口径唯一 SQL 处）。

五条口径写死在这里，service 层只做角色判断，API 层只做装配：
1. **scope**：每条聚合都过 `tasks` JOIN 拿 organization_id（`agent_runs` 没有 scope 列，
   裸聚合 = 跨组织泄漏，spec §1.5）。
2. **评测排除**：run 级聚合直接 `task_runs.run_type != 'evaluation'`；task 级计数复用
   `task_repo.product_task_filter()`。两者是同一口径的两个视角（评测 task 的定义就是
   「名下有评测 run 的 task」），必须同源，否则列表与数字分叉。
3. **时间窗落点**：任务数落 `tasks.created_at`，终态计数落 `task_runs.finished_at`
   （NULL 不计 —— 没跑完的行不进成功率分母），token/cost 落 `agent_runs.started_at`。
4. **成功率分母**：只算 completed / failed 两类终态行（其余状态进不了分母）。
5. **day 分组按本地日切**（R17）：`group_by="day"` 的键必须是**应用进程本地日历日**，与
   `range_start("today")` 同刻度——库的会话时区是 UTC，直接 `to_char` 会让「按天」分组和
   「今日」卡片差 8 小时。平移写法见 `_local_day_expr`。
"""
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, literal, select
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import AgentRun, Task, TaskRun
from app.data.repositories.task_repo import product_task_filter

RANGE_KEYS = ("today", "week", "month", "all")
GROUP_BY_KEYS = ("task_type", "model", "day")

# group_by 的表达式表：静态维度住在这里；day 不在表内（平移量要每次现算，见 _local_day_expr）。
_GROUP_EXPRS = {
    "task_type": Task.task_type,
    "model": func.coalesce(AgentRun.model, "（未知模型）"),
}


def _local_day_expr() -> ColumnElement[str]:
    """`started_at` 按**应用进程本地日**渲染成 YYYY-MM-DD（R17）。

    为什么不能直接 `to_char(started_at, 'YYYY-MM-DD')`：p6-postgres 的会话时区实测
    `show timezone` = **Etc/UTC**，而 `range_start('today')` 用的是应用进程本地日 00:00
    （本机 UTC+8）。直接渲染会让「按天」分组的日历日刻度比「今日」卡片晚 8 小时——
    本地 00:00–07:59 之间跑出来的分组会把今天的数据标成昨天，用户在 Settings 点
    「按天」就会看到和上方卡片互相矛盾的数字。先把瞬时按本地偏移平移、再按会话时区
    渲染，得到的就是本地日历日，与 range_start 同源。
    **实测禁止走 `AT TIME ZONE`**：`now() AT TIME ZONE '+08'` 在本库把偏移**取反**
    （05:44Z 渲染成前一天；判别常量 2026-09-26T20:00Z 也渲染成 09-26 而不是本地日 09-27），
    是个会静默算错一位的坑。平移量取 `datetime.now().astimezone().utcoffset()`，
    与 `range_start` 用的「本地」是同一个来源；部署区无夏令时，故不做逐行的历史偏移换算。
    """
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return func.to_char(AgentRun.started_at + literal(offset, INTERVAL), "YYYY-MM-DD")


def _group_expr(group_by: str) -> ColumnElement[str]:
    """维度 → SQL 表达式的唯一选择口（service/API 只认 GROUP_BY_KEYS 里的字符串）。"""
    if group_by == "day":
        return _local_day_expr()
    return _GROUP_EXPRS[group_by]


def range_start(range_key: str, *, now: datetime) -> datetime | None:
    """range → 时间窗下界（含）。`all` → None（不加时间条件）。

    裁定写死**滚动窗**（spec §3.1）：week/month = 滚动 7/30 天，不是自然周/月 ——
    「最近 7 天」才是运营想看的，自然周会让周一的数字看起来像系统坏了。
    today = 传入 now 所在**本地日**的 00:00；调用方必须传带 tz 的 now
    （列是 timestamptz，naive datetime 会被 Postgres 按会话时区猜，边界能差一天）。
    """
    if range_key == "all":
        return None
    if range_key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "week":
        return now - timedelta(days=7)
    if range_key == "month":
        return now - timedelta(days=30)
    raise ValueError(f"未知 range：{range_key}（合法值 {RANGE_KEYS}）")


def _scope_where(organization_id: uuid.UUID, user_id: uuid.UUID | None) -> list[Any]:
    """角色分口径的唯一构造处：admin 传 user_id=None（本组织全量），member 传自己的 id。"""
    conds: list[Any] = [Task.organization_id == organization_id]
    if user_id is not None:
        conds.append(Task.user_id == user_id)
    return conds


async def count_product_tasks(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    since: datetime | None = None,
) -> int:
    """产品任务条数（评测排除走共享谓词，时间窗落 created_at）。"""
    stmt = select(func.count()).select_from(Task).where(
        *_scope_where(organization_id, user_id), product_task_filter()
    )
    if since is not None:
        stmt = stmt.where(Task.created_at >= since)
    return int(await session.scalar(stmt) or 0)


async def run_status_counts(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    since: datetime | None = None,
) -> dict[str, int]:
    """task_runs 各状态计数（成功率分母的来源）。

    只数**已落终态且有 finished_at** 的行：`finished_at IS NULL` 的行（queued/running）
    进不了任何分母 —— 一次还没跑完的任务不该拉低成功率。
    run 级过滤用 `run_type` 列本身，不绕子查询：这里已经在 task_runs 上，
    再套 NOT IN 是白加一次半连接，且口径与 product_task_filter 完全一致。
    """
    stmt = (
        select(TaskRun.status, func.count())
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            *_scope_where(organization_id, user_id),
            TaskRun.run_type != "evaluation",
            TaskRun.finished_at.is_not(None),
        )
        .group_by(TaskRun.status)
    )
    if since is not None:
        stmt = stmt.where(TaskRun.finished_at >= since)
    rows = (await session.execute(stmt)).all()
    return {status: int(total) for status, total in rows}


async def token_cost_totals(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    since: datetime | None = None,
) -> dict[str, int | float]:
    """Token 与 Cost 汇总 —— **必须** agent_runs JOIN task_runs JOIN tasks。

    为什么不能裸聚合 agent_runs：该表没有 organization_id / user_id 列（spec §1.5，
    models.py:294-332 全览），裸 sum = 把别的组织的 token 算进本组织的卡片。
    cost 列是 Numeric(12,6)，SUM 出来是 Decimal，这里一律转 float 交给响应模型。
    """
    stmt = (
        select(
            func.coalesce(func.sum(AgentRun.prompt_tokens), 0),
            func.coalesce(func.sum(AgentRun.completion_tokens), 0),
            func.coalesce(func.sum(AgentRun.total_tokens), 0),
            func.coalesce(func.sum(AgentRun.cost), 0),
        )
        .select_from(AgentRun)
        .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            *_scope_where(organization_id, user_id),
            TaskRun.run_type != "evaluation",
        )
    )
    if since is not None:
        stmt = stmt.where(AgentRun.started_at >= since)
    prompt, completion, total, cost = (await session.execute(stmt)).one()
    return {
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(total),
        "total_cost": float(cost),
    }


async def usage_groups(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    group_by: str = "task_type",
) -> list[dict[str, Any]]:
    """按维度分组的用量。排序在 Python 侧做（token 多的在前），不把 SQL 表达式塞进 order_by。"""
    if group_by not in GROUP_BY_KEYS:
        raise ValueError(f"未知 group_by：{group_by}（合法值 {GROUP_BY_KEYS}）")
    key = _group_expr(group_by).label("key")
    stmt = (
        select(
            key,
            func.count(func.distinct(Task.id)).label("tasks"),
            func.coalesce(func.sum(AgentRun.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(AgentRun.cost), 0).label("total_cost"),
        )
        .select_from(AgentRun)
        .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            *_scope_where(organization_id, user_id),
            TaskRun.run_type != "evaluation",
        )
        .group_by(key)
    )
    if since is not None:
        stmt = stmt.where(AgentRun.started_at >= since)
    rows = (await session.execute(stmt)).mappings().all()
    groups = [
        {
            "key": str(row["key"]),
            "tasks": int(row["tasks"]),
            "total_tokens": int(row["total_tokens"]),
            "total_cost": float(row["total_cost"]),
        }
        for row in rows
    ]
    groups.sort(key=lambda g: g["total_tokens"], reverse=True)
    return groups
