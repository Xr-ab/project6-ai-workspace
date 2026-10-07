"""token → 钱（docs/08 §4 的 Cost 口径，全仓唯一计算处）。

纯函数 + 只读 settings：不 import sqlalchemy、不碰模型表。
这样执行侧（task_runner 的 span）、评测侧（Task 5 的 judge 用量）、
指标侧（Task 2 的 total_cost）共用一个换算，不会出现两处口径不一致。
"""
from __future__ import annotations

from app.core.config import settings

# cost 列是 Numeric(12,6)：留 6 位与列宽一致，多出来的位数会被库静默截掉
_COST_ROUND = 6


def price_configured() -> bool:
    """两把单价键**都**有值才算配置好。

    只配一侧就出数，那个数是"半价的成本"，比没有数更危险。
    0.0 是合法值（免费模型），所以判据用 `is None` 而不是真值判断。
    """
    return (settings.llm_input_price_per_1k is not None
            and settings.llm_output_price_per_1k is not None)


def compute_cost(prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    """返回 None = 这轮成本**不可得**（未配置单价），不是"成本为 0"。

    负数按 0：span 的 token 是 meta 差值算的（`node_guard._token_delta`），
    那里已经 max(diff, 0) 挡过一次，这里再兜一层 —— 负金额在报表里比 None 难解释得多。
    """
    if not price_configured():
        return None
    p = max(int(prompt_tokens or 0), 0)
    c = max(int(completion_tokens or 0), 0)
    cost = (p * settings.llm_input_price_per_1k
            + c * settings.llm_output_price_per_1k) / 1000.0
    return round(cost, _COST_ROUND)


def currency() -> str:
    """单价的货币单位，给前端把 ¥/$ 渲染对。口径：与单价同源，不单独判断。"""
    return settings.llm_price_currency
