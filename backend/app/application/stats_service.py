"""统计业务层（Phase 9a）。口径的五条硬约束见 stats_repo 模块头；这里只加两条业务判断：
1. **角色 → scope**：admin 聚合本组织、member 只聚合自己。判据与 `GET /agents/tasks`
   （api/agents.py:175 起同时传 organization_id 和 user_id）保持一致 —— 同一个产品里
   两处数字必须能互相对上，否则用户第一次打开 Dashboard 就会看到自相矛盾的数。
2. **成功率分母**：completed / (completed + failed)。分母 0 时返回 None 而不是 0.0 ——
   「这区间没跑完过任何任务」和「全失败了」是两回事，压成同一个 0% 是撒谎。

R17 留痕：`usage_groups(group_by="day")` 的键由数据层按**应用本地日历日**切好
（stats_repo._local_day_expr），本层与 API 层一律原样透传，**不做第二次时区换算**。
"""
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.pricing import currency, price_configured
from app.core.config import settings
from app.data.models import User
from app.data.repositories import conversation_repo, stats_repo, task_repo
from app.schemas.agent_task import TaskOut
from app.schemas.conversation import ConversationOut
from app.schemas.stats import (
    OverviewCards,
    PricingOut,
    ScopeKey,
    StatsOverviewOut,
    StatsUsageOut,
    SuccessBasis,
    UsageGroupOut,
)

RECENT_LIMIT = 5


def _scope_for(user: User) -> tuple[ScopeKey, uuid.UUID | None]:
    """返回 (scope 标记, 传给 repo 的 user_id)。member 传自己的 id，admin 传 None。"""
    if user.role == "admin":
        return "org", None
    return "personal", user.id


def _pricing_block() -> PricingOut:
    """单价摘要：只搬模型名与单价数字，币种与 pricing.py 同源（不单独判断）。"""
    return PricingOut(
        llm_model=settings.llm_model,
        embedding_model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
        currency=currency(),
        pricing_configured=price_configured(),
        input_price_per_1k=settings.llm_input_price_per_1k,
        output_price_per_1k=settings.llm_output_price_per_1k,
    )


async def overview(session: AsyncSession, *, user: User, range_key: str) -> StatsOverviewOut:
    scope, user_id = _scope_for(user)
    since = stats_repo.range_start(range_key, now=datetime.now().astimezone())
    counts = await stats_repo.run_status_counts(
        session, organization_id=user.organization_id, user_id=user_id, since=since
    )
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    denom = completed + failed
    totals = await stats_repo.token_cost_totals(
        session, organization_id=user.organization_id, user_id=user_id, since=since
    )
    tasks = await task_repo.list_tasks(
        session,
        organization_id=user.organization_id,
        user_id=user.id,
        limit=RECENT_LIMIT,
    )
    conversations = await conversation_repo.list_conversations(
        session,
        organization_id=user.organization_id,
        user_id=user.id,
        limit=RECENT_LIMIT,
    )
    return StatsOverviewOut(
        range=range_key,
        scope=scope,
        cards=OverviewCards(
            task_total=await stats_repo.count_product_tasks(
                session, organization_id=user.organization_id, user_id=user_id, since=since
            ),
            success_rate=(completed / denom) if denom else None,
            success_basis=SuccessBasis(completed=completed, failed=failed),
            total_tokens=int(totals["total_tokens"]),
            prompt_tokens=int(totals["prompt_tokens"]),
            completion_tokens=int(totals["completion_tokens"]),
            total_cost=float(totals["total_cost"]),
            currency=currency(),
            pricing_configured=price_configured(),
        ),
        recent_tasks=[TaskOut.model_validate(t) for t in tasks],
        recent_conversations=[ConversationOut.model_validate(c) for c in conversations],
    )


async def usage(
    session: AsyncSession, *, user: User, range_key: str, group_by: str
) -> StatsUsageOut:
    scope, user_id = _scope_for(user)
    since = stats_repo.range_start(range_key, now=datetime.now().astimezone())
    groups = await stats_repo.usage_groups(
        session,
        organization_id=user.organization_id,
        user_id=user_id,
        since=since,
        group_by=group_by,
    )
    totals = await stats_repo.token_cost_totals(
        session, organization_id=user.organization_id, user_id=user_id, since=since
    )
    return StatsUsageOut(
        range=range_key,
        scope=scope,
        group_by=group_by,
        groups=[UsageGroupOut(**g) for g in groups],
        # total.tasks 是「各分组内去重任务数之和」：组内 DISTINCT，跨组会重复计数
        # （同一个 task 的 span 落进两个 day 就被数两次）——已知口径，前端别把它
        # 当「任务总数」用（任务总数在 overview 卡片的 task_total，那边才是 DISTINCT 全量）。
        total=UsageGroupOut(
            key="合计",
            tasks=sum(g["tasks"] for g in groups),
            total_tokens=int(totals["total_tokens"]),
            total_cost=float(totals["total_cost"]),
        ),
        pricing=_pricing_block(),
    )
