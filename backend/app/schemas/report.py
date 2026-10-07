"""报告接口的请求 / 响应模型（Phase 9b，docs/06 §2.5）。

约定同 schemas/conversation.py：响应不直接回 ORM 对象，只暴露这里声明的字段，
把 organization_id / user_id 等内部列挡在外面。

列表与详情分两个模型（而不是一个宽模型）：
    列表页只需要"是哪份报告、什么时候、从哪来"，正文（`content` 可能是几十 KB 的
    JSONB、`markdown` 是全文）跟着列表一起回等于每翻一页都拖着 N 份正文走。
    这是分页接口最容易犯的胖响应。
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ReportOut(BaseModel):
    """报告摘要（列表用）：不含正文。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    report_type: str
    status: str
    reviewer_verdict: str | None = None
    total_tokens: int
    cost: float
    # 来源（可空：SET NULL 之后仍能列出这份报告，只是失去了回跳目标）
    task_id: uuid.UUID | None = None
    task_run_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class ReportDetailOut(ReportOut):
    """报告详情：摘要 + 正文两形。

    `content` 与 `markdown` **两个都给**，不是冗余：
      - `content` 是结构化真值（前端按字段分段渲染、将来做导出/对比都靠它）
      - `markdown` 是后端渲染好的全文（详情页直接 MarkdownView 就是它）
    只给一个的话，另一个的渲染口径就会散到前端去（本仓 Phase 9b 明确不这么做，
    理由见 report_service 模块 docstring）。`markdown` 为 null 表示结构化渲染失败
    或形状认不出 —— 前端此时退回按 `content` 渲染，不是"没有报告"。
    """

    content: dict
    markdown: str | None = None


class ReportListOut(BaseModel):
    """列表响应：`{items, total}`。

    **这是一处对 docs/06 §7.1「列表口一律裸数组」的有意破例**，且有代价：
    全仓列表口的消费者都按裸数组写，本口不是 —— 谁按老习惯 `[...]` 遍历本响应谁就炸。
    换来的东西：报告列表页要显示「共 N 份」并据此翻页，而裸数组拿不到总数，
    把当页长度当总数就是撒谎（`count_reports` 的 docstring 说的同一件事）。

    形状**只有两个键**，刻意不长成 `/auth/audit-log` 那个
    `{items,total,page,page_size}` 四键信封：v1 的分页参数是 `limit`/`offset`，
    回 `page`/`page_size` 会让前端为了翻页把 offset 再反推回去。
    破例已登记在 docs/06 §7.1 与 docs/10，不是漏登记的私货。
    """

    items: list[ReportOut]
    total: int
