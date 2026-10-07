"""Workflow 端点组的请求·响应模型（Phase 7 Task 7，spec §8 + docs/06 §2.6）。

约定同 `schemas/evaluation.py` / `schemas/agent_task.py`：响应不吐 ORM 对象，
只暴露声明字段，把 organization_id 等内部列挡在门外；请求模型是白名单，
`extra="forbid"` 让未声明字段当场 422 —— 触发入参没有资格携带执行接缝。
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictIn(BaseModel):
    """请求模型基类：白名单之外一律拒收（模式逐字复用 evaluation.py:63-66）。"""

    model_config = ConfigDict(extra="forbid")


# ---------------- 请求 ----------------


class TriggerIn(_StrictIn):
    """POST /workflows/{id}/trigger 的 body（docs/06 §2.6 的 `{inputs: {...}}` 形状）。

    `inputs` 的**必填键集 = 编目行的 input_spec**（如 doc_summary 要 document_id +
    question）：spec 是每行编目的数据、形状逐行不同，所以必填闸在路由层对着
    刚查出的 Workflow 行做（缺键 → 422），静态模型只锁外层键名。
    """

    inputs: dict = Field(default_factory=dict)


class DecisionIn(_StrictIn):
    """审批决策入参（brief 钦定签名，逐字）。

    `decision` 收窄成二值 Literal：放行/驳回是审批语义的全部真相，"maybe" 之类
    垃圾不许进服务（服务侧的「非 approved 一律按 rejected 落」是第二道兜底，
    不是把 HTTP 面放宽的理由）。`decided_by` 不在字段里 —— 决策人身份只能来自
    服务端身份（CurrentUser，8a 换源后），能从 body 冒充审批人 = 审批形同虚设。
    """

    decision: Literal["approved", "rejected"]
    comment: str | None = Field(None, max_length=2000)


# ---------------- 响应 ----------------


class WorkflowOut(BaseModel):
    """编目行（列表项与详情同形状）。

    `graph_key` 必须外显（brief 点名）：前端触发表单按它区分预置图的交互差异；
    `input_spec` 原样透传（字段名→类型声明），表单按它渲输入项 —— API 不 reshape。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str | None = None
    description: str | None = None
    graph_key: str
    input_spec: dict
    is_active: bool
    created_at: datetime


class TriggerOut(BaseModel):
    """触发即时响应（202）：两个 id 供轮询/审批寻址，status 是落库初值 pending。

    与 `agent_task.TaskSubmitOut` 同形 —— 前端同一套轮询代码复用（spec §8）。
    """

    task_id: uuid.UUID
    task_run_id: uuid.UUID
    status: str


class ApprovalOut(BaseModel):
    """一条审批记录（列表项与决策响应同形状）。

    decided_by/decided_at 在 pending 时为 null —— 「还没人决策」的真话，
    不填占位值；comment 是审批人留言（可空）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    graph_node: str
    status: str
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    created_at: datetime
