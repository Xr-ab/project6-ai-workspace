"""任务 / 执行 / Trace 接口的请求·响应模型（Phase 5：Agents API）。

约定同 schemas/conversation.py：响应不直接吐 ORM 对象，只暴露这里声明的字段，
把 organization_id / user_id 等内部列挡在外面。
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    """提交分析任务请求。mode 决定走哪张图（agent_analysis = Multi-Agent 全链路）。"""

    question: str = Field(min_length=1, max_length=2000)
    task_type: str = Field("agent_analysis", pattern="^(agent_analysis|workflow)$")


class TaskFollowUp(BaseModel):
    """追问请求：只带新问题。记忆由后端按 task 自己的历史快照装配，不由前端传
    —— 前端能传 memory_context 就等于把白名单旁开了（docs/11 §4.2 物理载体）。"""

    question: str = Field(min_length=1, max_length=2000)


class TaskSubmitOut(BaseModel):
    """提交/重跑的即时响应：给前端两个 id 去轮询状态 / 跳 Trace。"""

    task_id: uuid.UUID
    task_run_id: uuid.UUID
    status: str


class TaskRunOut(BaseModel):
    """一次执行（task_run）的状态与进度（docs/06 §2.4 执行状态结构）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_no: int
    status: str
    progress: int
    failure_category: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # meta 里含 report / reviewer_verdict / retry_count / latency_ms —— 详情/result 直接读它，
    # 不再单开端点（Phase 5 范围）。见 task_runner._run_meta。
    meta: dict | None = None


class TaskOut(BaseModel):
    """任务摘要（列表用）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    question: str
    task_type: str
    status: str
    created_at: datetime
    # Phase 9b：该任务**最新一份**报告的 id（没有报告时为 None）。
    # 列表页用它直接给出「看报告」入口，不必再按 task_run_id 反查一次 ——
    # 这一列 Phase 4 就建了，Phase 9b 才第一次有写入方（见 models.Task.report_id 注释）。
    report_id: uuid.UUID | None = None


class TaskDetailOut(TaskOut):
    """任务详情：摘要 + 最近一次执行（含报告 meta）。"""

    latest_run: TaskRunOut | None = None
    run_count: int = 0


class TraceNodeOut(BaseModel):
    """Trace 的一个节点：agent 执行与工具调用**统一形状**（docs/06 §4 的 nodes）。

    为什么合并成一种而不是 spans + tools 两个列表：归属关系（谁在谁下面）在库里就是
    `parent_span_id`，两个列表等于让每个消费者自己做一次合并 —— WorkflowPage 的 step bar
    和评测的下钻已经各抄过一遍了。合并后前端只做组树与渲染（spec §2.2）。
    """

    kind: str                 # "agent" | "tool"
    span_id: uuid.UUID
    parent_span_id: uuid.UUID | None   # NULL = 根
    name: str                 # agent_name 或 tool_name
    status: str               # ok / error
    duration_ms: int | None
    total_tokens: int         # 工具节点恒 0（tool_calls 无 token 列，不编数）
    cost: float
    model: str | None         # 工具节点为 None
    summary: str | None       # output_summary / 工具结果摘要
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class TraceOut(BaseModel):
    """一次执行的 Trace：run 概要 + 统一节点序列（按 started_at 升序，前端不再重排）。"""

    trace_id: uuid.UUID | None
    task_run_id: uuid.UUID
    status: str
    nodes: list[TraceNodeOut]


class MemoryRoundOut(BaseModel):
    """一轮执行在记忆里的样子（不含原始快照，那是 state 列的事）。"""

    run_no: int
    status: str
    has_snapshot: bool
    question: str | None = None
    conclusion: str | None = None
    facts: int = 0
    reused_by_next: bool = False   # 是否落在下一轮实际继承的窗口内


class TaskMemoryOut(BaseModel):
    """记忆视图：历轮摘要 + 下一轮会真正继承到的 memory_context（含 truncated 标记）。"""

    task_id: uuid.UUID
    rounds: list[MemoryRoundOut]
    next_memory_context: dict | None = None
