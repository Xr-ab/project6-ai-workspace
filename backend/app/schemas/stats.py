"""Dashboard 统计响应模型（Phase 9a，契约见 docs/06 §2.8）。

**这里的字段清单就是「能出网的数据」清单**：spec §6 钉死单价与模型只读、密钥永不外泄，
所以 PricingOut 是手写的白名单，而不是 settings 的投影 —— 加字段必须过一遍这个类。
"""
from typing import Literal

from pydantic import BaseModel

from app.schemas.agent_task import TaskOut
from app.schemas.conversation import ConversationOut

# 时间窗取值：**全仓唯一一份声明**（请求参数与响应字段都用它）。
# 语义（滚动窗、today=本地日 00:00）住在 `stats_repo.range_start`，这里只管词表 ——
# stats 两端点与 reports 列表口三个消费者共用，加一档要同时改词表和 range_start，
# 只改一处会在 range_start 里抛 ValueError（那条路有针，不会静默）。
RangeKey = Literal["today", "week", "month", "all"]
GroupByKey = Literal["task_type", "model", "day"]
ScopeKey = Literal["org", "personal"]


class SuccessBasis(BaseModel):
    """成功率的两个分母数字。带上它们，75% 才不是一个无法核对的孤数。"""

    completed: int
    failed: int


class OverviewCards(BaseModel):
    task_total: int
    success_rate: float | None      # None = 分母为 0（这区间没有终态行），不是 0%
    success_basis: SuccessBasis
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    total_cost: float
    currency: str
    pricing_configured: bool        # false 时 total_cost 恒 0，前端要显示「未配置单价」


class StatsOverviewOut(BaseModel):
    range: RangeKey
    scope: ScopeKey
    cards: OverviewCards
    recent_tasks: list[TaskOut]
    recent_conversations: list[ConversationOut]


class UsageGroupOut(BaseModel):
    key: str
    tasks: int
    total_tokens: int
    total_cost: float


class PricingOut(BaseModel):
    """只读单价摘要（Settings 页与 Dashboard 共用）。**不含任何密钥字段。**"""

    llm_model: str
    embedding_model: str
    embedding_dim: int
    currency: str
    pricing_configured: bool
    input_price_per_1k: float | None
    output_price_per_1k: float | None


class StatsUsageOut(BaseModel):
    range: RangeKey
    scope: ScopeKey
    group_by: GroupByKey
    groups: list[UsageGroupOut]
    total: UsageGroupOut
    pricing: PricingOut
