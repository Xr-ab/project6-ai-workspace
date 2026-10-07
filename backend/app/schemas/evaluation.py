"""评测接口的请求·响应模型（Phase 6 Evaluation，docs/06 §2.7）。

约定同 `schemas/agent_task.py`：响应不直接吐 ORM 对象，只暴露这里声明的字段，
把 organization_id / user_id 等内部列挡在外面（测试第 12 条钉住）。

🔴 测试接缝在本文件里**一个都不许出现**（计划硬约束，不是风格问题）：
`evaluation_service.execute_evaluation` 的 `executor` / `judge_enabled` / `default_score`
是给**单元测**留的注入点。一旦能从 HTTP 传进去，一次
`{"judge_enabled": false, "default_score": 5}` 就能产出一条 `passed` 来自硬编码分数、
指标全绿、状态 completed 的 run —— 读的人以为量到了质量，其实量的是自己设的默认值。
所以请求模型是白名单，并且用 `extra="forbid"`：**未声明字段直接 422，不静默丢弃**。
ignore 与 forbid 的差别在这里是可证的：ignore 下调用方以为自己关掉了判分、实际跑了
真 judge（还烧了钱），forbid 下它当场知道自己写错了字段名。

`type` ↔ `category` 的用词差（docs/06 用 `type`、表用 `category`）只出现在入参模型上，
语义不变；映射在 app/api/evaluations.py 一处完成（裁定 D11 同一类处理）。
"""
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# docs/08 §2 的五类评测对象。入参按这个集合校验：写错的类别不会报错但会让
# code 前缀（见 service 的 _case_code_prefix）和指标口径一起漂移，
# 而 category_scope 又是 retrieval_quality「不适用」判据的唯一来源。
# （fixround2 I1：这句"按这个集合校验"自本行起对两个入参都成立 ——
# CaseIn.type 与 DatasetCreate.category_scope 的元素都过同一个 pattern，
# 之前 category_scope 只限个数不限取值，注释说的是假话。）
CASE_CATEGORIES = ("chat", "rag", "tool_calling", "multi_agent", "workflow")
# docs/06 §2.7 :140 的 target 取值
RUN_TARGETS = ("agent", "prompt", "workflow")

# GET /compare 的 diff 项里 `dir` 的**取值域**（契约声明侧，W2）。
# 生产侧同一份在 evaluation_metrics.DIFF_DIRS —— 这里是"接口承诺给什么"，那边是
# "实现实际产什么"，两份都会自己漂，所以 test_evaluations_api 有一条真闸断言：
# 拿两次真 Run 的 compare 响应，逐项 dir 必须落在本元组内（新取值不加声明即红）。
# 为什么这里不收窄成 pydantic 校验：CompareOut 是 extra="allow" 的原样透传壳，
# 键名由 compute_metrics 决定、逐字段建模会把没建模的键静默丢掉（见 _PassThroughOut
# 的裁定）；取值域登记成常量 + 断言钉住，是透传形状下唯一不丢东西的写法。
COMPARE_DIRS = ("up", "down", "same", "missing_base", "missing_head",
                "unmeasured", "changed")

# compare 响应里**不是指标 diff** 的两个顶层键（W3）。
# 为什么要在契约侧登记：CompareOut 是 extra="allow" 的原样透传壳，前端拿它当
# {指标名: diff} 的表来渲；case_flips 与 case_coverage 混在同一层，
# 不点名就会被当成一格指标渲出来（渲成 [object Object]）。
# test_evaluations_api 有一条断言钉：响应顶层键集 = 指标键 ∪ 这两个，
# 后端再加第三种结构性键而这里没登记 → 红。
COMPARE_STRUCTURAL_KEYS = ("case_flips", "case_coverage")

# case_coverage 的字段域（diff_metrics 产出、service 读取边界把 case_no 归一成 int）
CASE_COVERAGE_KEYS = ("base_total", "head_total", "comparable_total",
                      "only_in_base", "only_in_head", "flips_total")

# 枚举 pattern 只写这一份：type 与 category_scope 收的是同一个值域（裁定见 D11 注释），
# 两处各拼一遍字面量 = 加类别时必然有一处漏改。
_CATEGORY_PATTERN = f"^({'|'.join(CASE_CATEGORIES)})$"

_CATEGORY_FIELD = Field(min_length=1, max_length=30, pattern=_CATEGORY_PATTERN)


class _StrictIn(BaseModel):
    """请求模型基类：白名单之外一律拒收（见模块头的接缝说明）。"""

    model_config = ConfigDict(extra="forbid")


# ---------------- 请求 ----------------


class CaseIn(_StrictIn):
    """一条用例的入参，形状逐字按 docs/06 :138 的 `{input, expected, type}`。

    `type` 落库为 `evaluation_cases.category`；`code` 缺省时由服务端按
    `{类别首字母}-{两位序号}` 生成（MA-01 / TC-01），显式给了就用给的
    （给了同数据集里已有的 code = upsert 覆盖内容列，见 evaluation_repo.upsert_cases）。
    """

    input: str = Field(min_length=1, max_length=5000)
    type: str = _CATEGORY_FIELD
    expected: dict | None = None
    code: str | None = Field(None, min_length=1, max_length=60)
    references: dict | None = None
    judgement: dict | None = None
    tags: list[str] | None = None


class PromoteCaseIn(_StrictIn):
    """回放沉淀入参（D9 / docs/08 §3.3 主渠道）：把一条生产 task_run 加成评测用例。

    只收 task_run_id —— 目标数据集走路径参数（归属闸与 D13 同一套理由），
    category / input / references 全部由服务端从任务行反推，客户端不自报任何字段。
    """

    task_run_id: uuid.UUID


class DatasetCreate(_StrictIn):
    """创建用例集。`cases` 可省（先建集再加用例），给了就在同一次请求里幂等写入。

    `category_scope` 的**元素**按 CASE_CATEGORIES 逐值校验（fixround2 I1）：
    光靠 max_length 只挡个数，["multi_agentt"] 能一路落库，之后
    compute_metrics 拿它判 retrieval_quality/not_applicable —— 静默产出一份
    "检索质量未覆盖"看着像真结论的测量。拼错当场 422，不进来再猜。
    """

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    category_scope: list[
        Annotated[str, StringConstraints(pattern=_CATEGORY_PATTERN)]
    ] | None = Field(None, max_length=len(CASE_CATEGORIES))
    cases: list[CaseIn] | None = None


class RunCreate(_StrictIn):
    """发起评测。

    **没有 `target_version` 这个字段是有意的**：版本锚是回归对比的对齐依据
    （docs/08 §6），只能由服务端从 git 现取（evaluation_service._default_target_version）；
    让客户端自报 = 任何人都能把两次不同的执行写成同一个锚。
    也**没有 judge/executor 相关的任何字段**（见模块头）。
    """

    dataset_id: uuid.UUID
    target: str = Field(pattern=f"^({'|'.join(RUN_TARGETS)})$")
    note: str | None = Field(None, max_length=2000)


# ---------------- 响应：数据集 / 用例 ----------------


class CaseOut(BaseModel):
    """一条用例的全字段（docs/06 §2.7「用例集详情含 cases 明细」）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    category: str
    input: str
    expected: dict
    references: dict
    judgement: dict
    tags: list


class DatasetOut(BaseModel):
    """数据集摘要（列表项与创建响应同形状）。

    `case_count` 不是 evaluation_datasets 的列：由 evaluation_repo 的计数函数
    查出来再填进来（列表页一次 GROUP BY，见 evaluation_service.case_counts），
    响应模型自己不现算全表。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    category_scope: list = []
    created_at: datetime
    case_count: int = 0


class DatasetDetailOut(DatasetOut):
    """数据集详情 = 摘要 + 用例明细（code 正序，与 Run 内 case_no 同向）。"""

    cases: list[CaseOut] = []


# ---------------- 响应：Run 与结果 ----------------


class RunSubmitOut(BaseModel):
    """POST /runs 的即时响应：只给 id 与初始状态，执行在后台。

    `status` 是 docs/06 §2.7 的 `{evaluation_id}` 之外多带的一个（落地补记已写进文档）：
    前端拿到就能立刻判断起跑是否成功，不必再补一次 GET。
    """

    evaluation_id: uuid.UUID
    status: str


class RunOut(BaseModel):
    """评测状态 / 进度（docs/08 §7 轮询的就是这条）。

    只出 pending / running 需要的进度字段，**不内嵌 metrics / results**：那两块各有
    专门端点（未完成时 metrics 是 409 而不是 null，见 evaluation_service.get_metrics）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dataset_id: uuid.UUID | None = None
    target: str
    target_version: str
    note: str | None = None
    status: str
    progress: int
    case_total: int
    case_done: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


class RunSummaryOut(RunOut):
    """GET /runs 的列表项 = 状态字段 + created_at + metrics。

    带 metrics 是给趋势图当数据源（docs/08 §6「各指标趋势」）：不带的话前端要为列表里
    每条 run 各发一次 /metrics。repo 侧 list_runs 只出终态，所以这里的 metrics 必然有值
    （completed）或为写失败前的 null（failed），不做任何裁剪 —— 键的形状与
    evaluation_metrics.compute_metrics 逐字一致。
    """

    created_at: datetime
    metrics: dict | None = None


class _PassThroughOut(BaseModel):
    """原样透传的响应壳：不声明字段、`extra="allow"` 全收。

    为什么用这种形状而不是逐字段建模（brief 对 RunMetricsOut 的裁定）：
    指标键由 `evaluation_metrics.compute_metrics` / `diff_metrics` 决定，
    在 API 侧再建一遍模型 = 两处各持一份键名，加指标时必然漂移；
    更糟的是**建模会静默丢掉没建模的键** —— 于是"这轮少了个指标"看起来像"值是 0"。
    透传是这套形状里唯一不会静默丢东西的写法。
    """

    model_config = ConfigDict(extra="allow")


class RunMetricsOut(_PassThroughOut):
    """评测指标（docs/06 §2.7 的响应，Task 2 的 metrics 原样）。

    docs/06 §2.7 :142 点名的六个键（契约，逐字不改）：
        task_success_rate / tool_success_rate / latency_avg_ms /
        total_tokens / total_cost / failure_category_dist
    docs/08 §4 的其余口径加在旁边（latency_p50_ms / latency_p95_ms /
    total_prompt_tokens / total_completion_tokens /
    agent_completion_rate / reviewer_error_detection_rate / reviewer_miss_rate /
    final_answer_quality_avg / retrieval_quality / cases_total / cases_passed /
    cases_error / reviewed_cases / cost_available / quality_pass_score /
    category_scope / not_applicable / case_results）。
    `total_cost: null` = 未配单价（cost_available=false，D5），不是"这轮免费"。
    """


class CompareOut(_PassThroughOut):
    """两次 Run 的回归对比 = evaluation_metrics.diff_metrics 的返回原样。

    形状（docs/08 §6）：`{指标名: {base, head, delta, dir, lower_is_better,
    direction_registered}}` + `case_flips: [{case_no, from, to}]`（case_no 是 int，
    与 /result 同型 —— JSONB 键的字符串强转在 service.compare_runs 读取边界归一）。
    `dir` 的取值域 = 本文件的 COMPARE_DIRS（逐字 = evaluation_metrics.DIFF_DIRS）：
        up / down           两侧都是有限数值、delta 量出来非 0
        same                delta 恰为 0，或两侧都是非数值且值相等（真持平）
        missing_base        基线侧没这个键（新增）
        missing_head        当前侧没这个键（消失）
        unmeasured          两侧键都在、但任一侧为 null —— **没量出来，不是持平**（W2）
        changed             两侧都在、都不是 null、但不是两个可减数值且值不相等
                            （旗标翻了 / 集合换构成）—— 变了，但方向无意义
    裁定 D14：单边缺席不许渲成"持平"。W2 把同一条纪律延伸到"键在而没量出数值"。
    `direction_registered=false` = 该键的方向语义两张表（LOWER/HIGHER_IS_BETTER）都没登记，
    此时 `lower_is_better` 的 false 含义是"没登记"而不是"越大越好"，
    消费端必须渲中性"方向未登记"，不许给绿/红（方向表只有一份，在前端再养一张即两个
    真相源，W2 裁定不采纳那个建议）。

    顶层还有两个**结构性键**（= COMPARE_STRUCTURAL_KEYS，不是指标、没有 dir）：
        case_flips    两侧都跑过、pass/fail 变了的那些条
        case_coverage W3 补的覆盖账：`{base_total, head_total, comparable_total,
                      only_in_base: [case_no], only_in_head: [case_no], flips_total}`
        —— case_flips 只统计**两侧都跑过**的用例，所以 base 10 条 / head 5 条时
        flips 可以是空的而真相是"后 5 条根本没进过场"；没有这个字段，页面的
        "没有翻转"就是一句对覆盖收缩的谎（与 D14/W2 同族，判据 W3.3）。
    """


class EvalResultCaseOut(BaseModel):
    """一条用例的结果（docs/06 §2.7 :143 的 case 形状 + case_no/status/latency/cost）。

    `cost: float | None` —— **None 的含义是"未配单价、这轮成本不可得"，不是 0**（D5）：
    前端的"未配置"提示要显式渲染，把 None 当 0 显示就是假账。
    `task_run_id` 是失败下钻的入口（docs/08 §8：评测不复制执行痕迹，只引用生产 Trace）。
    """

    model_config = ConfigDict(from_attributes=True)

    case_id: uuid.UUID | None = None
    case_no: int
    status: str
    passed: bool
    score: int | None = None
    trace_id: uuid.UUID | None = None
    task_run_id: uuid.UUID | None = None
    note: str | None = None
    reasons: list | None = None
    latency_ms: int | None = None
    cost: float | None = None


class RunResultOut(BaseModel):
    """逐用例结果（docs/06 §2.7 :143：外层键就叫 `cases`）。"""

    cases: list[EvalResultCaseOut] = []
