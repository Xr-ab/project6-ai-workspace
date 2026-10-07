"""评测 Runner（Phase 6 Evaluation，docs/08 §5 / §7）：一批用例跑完并记账。

分层职责：judge 只管"给一条输出打分"（app/ai/evaluation_judge.py），
本模块只管"把一批用例跑完、逐条落结果、汇总指标"。
Task 6 起本模块还多管一块「数据集与用例的写读」（建集 / 加用例 / 列表计数）：
自动 code 要按库里现有编号续号，读库是 repo 的事、业务判断是 service 的事，
两头都不沾的那一层就只能落在这里（路由侧仍然是「取身份 → 调 service → 选响应模型」）。

🔴 执行侧的会话纪律（本模块最容易炸的地方，docs/08 §7 的异步形状由此决定）：
    8b T6 起 execute_evaluation 由 worker 侧领 job 后直调（app/workers/jobs.py
    的 run_evaluation），不再挂在请求进程的后台 —— 请求带来的 session 在响应
    返回时就由 get_db 关掉了，执行函数绝不能继续用它，也不能接外部 session 参数
    （测试第 6 段用 inspect.signature 钉住这一点）。幂等门（pending→running
    条件 UPDATE）收口在 jobs 侧，本函数的终态检查退居第二道闸。
    并且**每条用例一条新会话**：一条 multi_agent 用例跑 60s，中途任一失败会让会话
    进入 aborted 状态；沿用同一条会话 = 第一条失败拖垮后面 9 条
    （Phase 6 记忆侧 _fail 先 rollback 再留痕，是同一类问题的解法）。

指标口径的两条红线（都是前面 Task 复核钉下来的，不是本地发明）：
    1. cost 一律由 token 经 pricing.compute_cost 现算（cost_of_row），**绝不读
       agent_runs.cost / evaluation_results.cost 列** —— 列 NOT NULL DEFAULT 0，
       未配单价与真实 0 元分不开，读列会让 compute_metrics 的门槛判出
       cost_available=true + total_cost=0.0 的假测量（裁定 D5）。
    2. judge 自己的 token/成本**绝不并入 metrics.total_tokens / total_cost**：
       判分是评测的开销，不是被测产品的开销 —— 混进去后趋势图里"这轮比上轮贵了"
       可能只是 judge 换了模型，读图的人会以为是 Agent 退步了。
       judge 用量只写进 EvaluationResult.case_snapshot["judge"]，Task 8 要报
       "评测本身花了多少"时从那里 sum。

status 字面量的两套世界（裁定 D12）：
    agent_runs / tool_calls 的 span 状态只有 'ok' / 'error'（docs/08 §8 写 'completed'
    是文档错）—— 这个 'ok' 只允许出现在 _flatten_for_metrics 的比较式里，
    写错的方向（!= 'completed'）不会报错，只会让 agent_error_nodes 恒 0、
    agent_completion_rate 恒 1.0，把"全绿批次"伪装成满分。
    EvaluationResult.status 是另一套三态（ok / failed / error，见模型注释），
    与 span 状态无关。

passed 的唯一出处是 evaluation_metrics.score_case（裁定 D15，阈值 QUALITY_PASS_SCORE
单源在指标模块，本文件不重复声明）；本文件所有计算都委托 Task 2 的纯函数。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import evaluation_judge, pricing
from app.application.evaluation_metrics import compute_metrics, diff_metrics, score_case
from app.core.exceptions import (
    EvaluationConflictError,
    EvaluationNotFoundError,
    EvaluationParamError,
)
from app.data.db import AsyncSessionLocal
from app.data.models import (
    AgentRun,
    EvaluationCase,
    EvaluationDataset,
    EvaluationResult,
    EvaluationRun,
    TaskRun,
    ToolCall,
)
from app.data.repositories import evaluation_repo, task_repo

logger = logging.getLogger(__name__)

# EvaluationRun 的终态：入口见到就直接拒绝重复执行（幂等，测试第 7 段）
_TERMINAL_STATUSES = ("completed", "failed")

# 本模块对 evaluation_results / metrics 行内数值统一走 JSONB：
# Numeric 列读回是 Decimal，sum() 混 float 会 TypeError、Decimal 也进不了 JSONB ——
# 一律在**读到的那一刻**归一成 float（验收判据 3）。
_JUDGE_TOKENS_KEY = "judge"  # case_snapshot 里 judge 用量的键名


@dataclass
class CaseOutcome:
    """一条用例跑完后的完整结果（写库前在内存里过一道手，字段与结果列同名）。"""

    case_no: int
    status: str                      # ok / failed / error（评测结果三态，非 span 态）
    passed: bool
    score: int | None
    reasons: list[str]
    task_run_id: uuid.UUID | None
    trace_id: uuid.UUID | None
    latency_ms: int | None
    prompt_tokens: int
    completion_tokens: int
    cost: float | None               # None = 未配单价（D5），写列时按 NOT NULL 记 0
    failure_category: str | None
    note: str | None


def _as_float_or_none(value: Any) -> float | None:
    """Numeric/Decimal → float，None 原样（含义是"不可得"，不是 0）。"""
    return None if value is None else float(value)


def _as_int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def cost_of_row(row: dict) -> float | None:
    """一行展平结果的 cost：从两个 token 数经 pricing 现算，不读任何 cost 列。

    未配置单价返回 None（不是 0），指标层据此判 cost_available（D5）。
    """
    return _as_float_or_none(
        pricing.compute_cost(row.get("prompt_tokens"), row.get("completion_tokens"))
    )


def _default_target_version() -> str:
    """target_version 缺省时的自动兜底：当前 git commit 短号（docs/08 §6 的回归锚）。

    这条兜底只在此处实现一份（Task 5/6 说的是同一件事，不各写一遍）：
    让调用方手填 = 一定有人填空，而回归对比全靠这个字段对齐。
    取不到 git（非仓库环境 / git 不在 PATH）时退成 "unknown"+UTC 时间戳 ——
    宁可锚是难看的，也不能为空（为空则两次 Run 无法区分是谁测的）。
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        sha = (out.stdout or "").strip()
        if sha:
            return sha[:100]
    except Exception:  # noqa: BLE001 —— 兜底路径本身不许把起跑请求带炸
        pass
    return "unknown-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


# ---------------- 写侧：起跑 ----------------


async def start_run(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    target: str,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    target_version: str | None = None,
    note: str | None = None,
) -> EvaluationRun:
    """建 run（pending）并冻结 case_total，**不执行**；8b T6 起执行由路由入队、
    worker 领 job 后直调 execute_evaluation（见 api/evaluations.py 与 workers/jobs.py）。

    用例数为 0 直接抛 EvaluationParamError（EVAL_400001）且**不建 run** ——
    docs/06 §5 登记该码为「用例集为空/参数非法」；一个空批次的 completed run
    会伪装成"测过了"。这与 Task 2 不冲突：纯函数侧对空 rows 出全 None 是纵深防御，
    不是入口放行的理由。
    题面快照的冻结在 execute_evaluation 逐用例写 case_snapshot 时完成
    （"跑当时测的是什么"只有开跑那一刻知道，start_run 先只锚定条数）。
    """
    dataset = await evaluation_repo.get_dataset(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    if dataset is None:
        raise EvaluationNotFoundError("评测数据集不存在")
    cases = await evaluation_repo.list_cases(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    if not cases:
        raise EvaluationParamError("用例集为空，拒绝起跑（空批次的 completed 会伪装成测过了）")
    # W11 叠跑闸门：同一数据集已有未完成的批次就不再叠一条 —— 两批并发会交错写
    # task_runs/进度，趋势图上"这两批"根本不可解释。裁定：不加 cancel/retry 端点。
    if await evaluation_repo.has_active_run_on_dataset(
        session, dataset_id=dataset_id, organization_id=organization_id
    ):
        raise EvaluationConflictError("该数据集已有未完成的评测批次，先等它跑完（不允许同数据集叠跑）")
    run = await evaluation_repo.create_run(
        session,
        dataset_id=dataset_id,
        organization_id=organization_id,
        user_id=user_id,
        target=target,
        target_version=target_version or _default_target_version(),
        note=note,
        case_total=len(cases),
    )
    await session.commit()  # 路由层在响应前就需要 run 行可见（POST /runs 只回 id）
    return run


# ---------------- 写侧：数据集与用例（docs/06 §2.7 的建集 / 加用例两个端点） ----------------
#
# 为什么这一小块写在 service 而不是路由里：本仓的分层是「路由只取身份 → 调 service →
# 选响应模型」（api/agents.py 的模块头），自动 code 要读库里的现有编号才能续号，
# 读库就是 repo 的事 → 中间这一层只能在 service。
# 插入路径仍然只有 Task 4 的 upsert_cases 一条（brief 硬约束）：本块不 add 任何
# EvaluationCase，只做「补齐 code + 调 upsert + 回读」。

_CASE_CODE_RE = re.compile(r"^([A-Z]{1,2})-(\d+)$")


def _case_code_prefix(category: str) -> str:
    """自动 code 的前缀 = 类别各词首字母大写（multi_agent→MA、tool_calling→TC）。

    与 docs/08 §3.4 那 10 条基线用例的编号同一套规则 —— 前缀规则一旦不同，
    seed 建的 MA-01 和接口建的 M-01 就能在同一条目集里共存，(dataset_id, code)
    唯一键挡不住这种分叉。
    """
    words = [w for w in (category or "").strip().split("_") if w]
    return ("".join(w[0] for w in words)[:2] or "C").upper()


def _with_generated_codes(cases: list[dict], existing: set[str]) -> list[dict]:
    """给没带 code 的用例补 `{前缀}-{两位序号}`，序号从本数据集该前缀的现有最大值续。

    纯函数（不查库）：existing 是**库里已有的 code 集合**，同批次里显式给过的 code 也
    先进 used，否则"第 1 条显式 MA-02 + 第 2 条自动生成"会让第 2 条也叫 MA-02，
    然后在 upsert_cases 里把第 1 条覆盖掉 —— 一次请求少一条用例，还看不出来。
    """
    used = set(existing) | {c["code"] for c in cases if c.get("code")}
    seq: dict[str, int] = {}
    for code in existing:
        m = _CASE_CODE_RE.match(code)
        if m:
            seq[m.group(1)] = max(seq.get(m.group(1), 0), int(m.group(2)))
    out: list[dict] = []
    for case in cases:
        row = dict(case)
        code = row.get("code")
        if not code:
            prefix = _case_code_prefix(row.get("category") or "")
            n = seq.get(prefix, 0) + 1
            while f"{prefix}-{n:02d}" in used:
                n += 1
            code = f"{prefix}-{n:02d}"
            row["code"] = code
            used.add(code)
            seq[prefix] = n
        out.append(row)
    return out


async def _apply_cases(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    cases: list[dict],
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[list[EvaluationCase], int]:
    """一次事务内的用例写入：续 code → upsert_cases → 回读涉及的行。

    返回 `(涉及的用例行, 其中新登记的条数)` —— 第二条是给 POST /datasets/{id}/cases
    区分 201（真新建）与 200（幂等覆盖）用的：upsert 语义下这两个都必须能分辨，
    否则调用方无法知道自己到底是加了用例还是改了用例。
    """
    if not cases:
        return [], 0
    before = await evaluation_repo.list_cases(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    existing = {row.code for row in before}
    prepared = _with_generated_codes(cases, existing)
    created = len([c for c in prepared if c["code"] not in existing])
    await evaluation_repo.upsert_cases(
        session,
        dataset_id=dataset_id,
        organization_id=organization_id,
        user_id=user_id,
        cases=prepared,
    )
    after = {
        row.code: row
        for row in await evaluation_repo.list_cases(
            session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
        )
    }
    touched = [after[c["code"]] for c in prepared if c["code"] in after]
    return touched, created


async def create_dataset(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    description: str | None = None,
    category_scope: list[str] | None = None,
    cases: list[dict] | None = None,
) -> EvaluationDataset:
    """建数据集，cases（docs/06 :138 的入参形状）在同一次请求里写完，一个事务收尾。

    分两次 commit 会留一个中间态：数据集建了、用例没建（请求在第二步失败）——
    那正是「空数据集」的样子，而空数据集是 POST /runs 要 400 拒绝的东西。
    """
    dataset = await evaluation_repo.create_dataset(
        session,
        organization_id=organization_id,
        user_id=user_id,
        name=name,
        description=description,
        category_scope=list(category_scope or []),
    )
    await _apply_cases(
        session,
        dataset_id=dataset.id,
        cases=cases or [],
        organization_id=organization_id,
        user_id=user_id,
    )
    await session.commit()
    return dataset


async def upsert_dataset_cases(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    cases: list[dict],
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[list[EvaluationCase], int]:
    """往既有数据集加/改用例（单条也是走这里，传单元素列表）。

    dataset_id 是路径参数，但**归属仍然要在这里过一遍**：先 get_dataset(带 org + user) 拿 404，
    再谈写不写。这就是 docs/08 §7 的平铺 /cases 落地成嵌套路径的全部理由（裁定 D13）——
    平铺形状下 dataset_id 来自请求体，等于让客户端自报"这数据集是我的"。
    """
    await get_dataset(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    touched, created = await _apply_cases(
        session,
        dataset_id=dataset_id,
        cases=cases,
        organization_id=organization_id,
        user_id=user_id,
    )
    await session.commit()
    return touched, created


# ---------------- 写侧：从生产任务回放沉淀用例（D9 收口；docs/08 §3.3 的"主渠道"）----------------

# task_type → 评测类别。表里只放**本仓真实存在的生产类型**（models.Task.task_type 注释同源）：
#   agent_analysis = supervisor 多智能体图（/agents 提交面只认它，见 api/agents.py:47）；
#   workflow       = 三条声明式工作流。
# 映射不出类别 = 400 拒绝，不猜（chat / rag 两类至今没有"生产任务"入口，不预埋）。
_REPLAY_CATEGORY_BY_TASK_TYPE = {"agent_analysis": "multi_agent", "workflow": "workflow"}

# 可回放的运行终态：跑完才有"当时的产出"可看；waiting_approval 是半途、running 还没数，
# 都不许提前沉淀（failed 可以——"任务失败过"正是 §3.3 点名的沉淀条件之一）。
_REPLAYABLE_RUN_STATUSES = ("completed", "failed", "rejected")


async def promote_task_run_to_case(
    session: AsyncSession,
    *,
    task_run_id: uuid.UUID,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[EvaluationCase, bool]:
    """生产 task_run → 评测用例（v1「一键回放沉淀」，docs/08 §3.3；裁定 D9 收口）。

    反推规则（服务端唯一决定，客户端只交 run id 与 dataset id，不自报任何字段）：
        input       = task.question（"当时的输入"）；
        category    = _REPLAY_CATEGORY_BY_TASK_TYPE[task.task_type]；
        references  = {task_id, task_run_id, task_type}（追溯回 Trace 的锚）；
        expected    = {"key_points": []} —— §3.3 原话"后续人工补 expected"，
                      空要点是**明确的待标注态**，不是"默认通过"（tag 里也写明）；
        tags        = ["replay", "待补expected"]。

    幂等：同数据集内已有指向这条 run 的用例 → 原样返回它（created=False），重复点
    "加入评测集"不会长出第二条。数据集与 run 的归属闸都是 org + user 双过滤（W12 同口径），
    走不到的行按 404 处理（不是 403，与全仓探测防护同款）。
    """
    await get_dataset(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    run = await task_repo.get_task_run(
        session, run_id=task_run_id, organization_id=organization_id, user_id=user_id
    )
    if run is None:
        raise EvaluationNotFoundError("执行记录不存在")
    if run.run_type != "product":
        raise EvaluationParamError("评测执行不能回放沉淀（请选生产任务）")
    if run.status not in _REPLAYABLE_RUN_STATUSES:
        raise EvaluationParamError(f"任务尚未跑完（status={run.status}），跑完再沉淀")
    task = await task_repo.get_task(
        session, task_id=run.task_id, organization_id=organization_id, user_id=user_id
    )
    if task is None:
        raise EvaluationNotFoundError("任务不存在")
    category = _REPLAY_CATEGORY_BY_TASK_TYPE.get(task.task_type)
    if category is None:
        raise EvaluationParamError(f"task_type={task.task_type!r} 暂无对应评测类别，不支持回放")
    question = (task.question or task.title or "").strip()
    if not question:
        raise EvaluationParamError("任务没有可回放的输入文本（question 为空）")

    existing = await evaluation_repo.list_cases(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    for row in existing:
        if (row.references or {}).get("task_run_id") == str(run.id):
            return row, False

    touched, created = await _apply_cases(
        session,
        dataset_id=dataset_id,
        cases=[{
            "category": category,
            "input": question,
            "expected": {"key_points": []},
            "references": {
                "task_id": str(task.id),
                "task_run_id": str(run.id),
                "task_type": task.task_type,
            },
            "judgement": {},
            "tags": ["replay", "待补expected"],
        }],
        organization_id=organization_id,
        user_id=user_id,
    )
    await session.commit()
    # `created > 0` 归一成 bool：幂等分支返回的是字面 False，两条路径的类型要一致
    # （针发现过的：int 1 经 `is True` 判不过，且注解写的就是 bool）
    return touched[0], created > 0


# ---------------- 读侧：数据集与用例 ----------------
#
# W12：以下读函数一律带必填 user_id 并原样传给 repo（org + user 双过滤）——
# 同 org 的同事之间也不互见。


async def get_dataset(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> EvaluationDataset:
    dataset = await evaluation_repo.get_dataset(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    if dataset is None:
        raise EvaluationNotFoundError("评测数据集不存在")
    return dataset


async def list_datasets(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[EvaluationDataset]:
    return await evaluation_repo.list_datasets(
        session, organization_id=organization_id, user_id=user_id,
        limit=limit, offset=offset,
    )


async def list_cases(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[EvaluationCase]:
    await get_dataset(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    return await evaluation_repo.list_cases(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )


async def case_counts(
    session: AsyncSession,
    *,
    dataset_ids: list[uuid.UUID],
    organization_id: uuid.UUID,
) -> dict[uuid.UUID, int]:
    """数据集 → 用例条数（一次 GROUP BY）。缺键 = 该集 0 条，调用方 .get(id, 0)。"""
    return await evaluation_repo.count_cases_by_datasets(
        session, dataset_ids=dataset_ids, organization_id=organization_id
    )


# ---------------- 取数：展平（纯取数，不做任何计算） ----------------

async def _flatten_for_metrics(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    results: list[EvaluationResult],
) -> list[dict]:
    """Task 2 的入参展平处：每行都是标量（{case_no, category, status, passed, score,
    latency_ms, prompt_tokens, completion_tokens, cost, failure_category,
    tool_total, tool_ok, agent_error_nodes, reviewer_verdict, retried}）。

    计算全在 compute_metrics（纯函数）里，本函数只做取数，两条纪律：
    1. span 聚合**只在结果行的 task_run_id 范围内**数 —— 按 trace_id / task_id 数
       会把同一 task 上一次重跑的 span 混进这条用例的指标里。
    2. span 的 'ok' 字面量只许出现在这里（D12）：agent_error_nodes 的判据是
       status != 'ok'。写成 != 'completed' 不报错，但每个正常 span 都被数成错误
       节点，agent_completion_rate 恒 0；反向写错方向则 error span 数不到、
       指标永远满分 —— 两种都静默。
    3. cost 不读列（文件头红线 1）。
    结果行没有 task_run_id（executor 直接炸掉，没建出 run）时退化成读结果行自身
    已有的标量列，span 计数全 0。
    """
    rows: list[dict] = []
    for res in results:
        prompt_tokens = _as_int_or_none(res.prompt_tokens) or 0
        completion_tokens = _as_int_or_none(res.completion_tokens) or 0
        latency_ms = _as_int_or_none(res.latency_ms)
        failure_category = res.failure_category
        tool_total = tool_ok = agent_error_nodes = 0
        reviewer_verdict: Any = None
        retried = False
        if res.task_run_id is not None:
            ar = (await session.execute(
                select(
                    func.coalesce(func.sum(AgentRun.prompt_tokens), 0),
                    func.coalesce(func.sum(AgentRun.completion_tokens), 0),
                    func.coalesce(func.sum(case((AgentRun.status != "ok", 1), else_=0)), 0),
                    func.coalesce(func.sum(case((AgentRun.agent_name == "reviewer", 1), else_=0)), 0),
                ).where(AgentRun.task_run_id == res.task_run_id)
            )).one()
            prompt_tokens = int(ar[0])
            completion_tokens = int(ar[1])
            agent_error_nodes = int(ar[2])
            reviewer_spans = int(ar[3])
            tc = (await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(case((ToolCall.status == "ok", 1), else_=0)), 0),
                ).where(ToolCall.task_run_id == res.task_run_id)
            )).one()
            tool_total = int(tc[0])
            tool_ok = int(tc[1])
            tr = (await session.execute(
                select(TaskRun.failure_category, TaskRun.meta)
                .where(TaskRun.id == res.task_run_id)
            )).first()
            if tr is not None:
                # W1（后端 C-1）：链路侧的 failure_category **只在未通过时采纳**。
                # 这一列的名字就叫"失败类别"，通过的行带它等于自相矛盾 —— 修前是无条件
                # 覆盖，于是 case 6（status=ok / passed=t / score=3）因为链路上有过一次
                # 评审超时被打回，就被写进"失败分布"，把 3 个真失败量成 4 个。
                # 不丢信息：链路事实仍留在 task_runs.failure_category 与本行的
                # case_snapshot["spans"] 里，要查"这条用例的链路发生过什么"照样查得到。
                failure_category = tr.failure_category if not res.passed else None
                meta = tr.meta if isinstance(tr.meta, dict) else {}
                latency_ms = _as_int_or_none(meta.get("latency_ms"))
                reviewer_verdict = meta.get("reviewer_verdict")
                # reviewer span 数 > 1 = 这次执行被打回回炉过（retried 的口径）
                retried = reviewer_spans > 1
        row = {
            "case_no": res.case_no,
            "category": (res.case_snapshot or {}).get("category"),
            "status": res.status,
            "passed": bool(res.passed),
            "score": res.score,
            "latency_ms": latency_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "failure_category": failure_category,
            "tool_total": tool_total,
            "tool_ok": tool_ok,
            "agent_error_nodes": agent_error_nodes,
            "reviewer_verdict": reviewer_verdict,
            "retried": retried,
        }
        row["cost"] = cost_of_row(row)
        rows.append(row)
    return rows


# ---------------- 写侧：后台批次入口 ----------------


async def _execute_case(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    case: Any,
    case_no: int,
    executor: Callable[..., Coroutine[Any, Any, Any]],
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    judge_enabled: bool,
    default_score: int | None,
) -> CaseOutcome:
    """跑一条用例并把结果行落进**这条**会话（调用方 commit 后才关）。

    单条用例内部的一切异常都收进结果行，不外抛 —— 15 分钟的批次不能因第 2 条白等；
    只有会话级故障（写库都写不动）才穿透到整批兜底。
    """
    snapshot = {
        "code": case.code,
        "category": case.category,
        "input": case.input,
        "expected": case.expected,
        "references": case.references,
        "judgement": case.judgement,
        "tags": case.tags,
    }

    async def _persist(outcome: CaseOutcome, agg: dict | None, judge_usage: dict | None) -> None:
        snap = dict(snapshot)
        if agg is not None:
            # span 计数随结果行留一份：Trace 下钻之外，人工复核"这个数从哪来"不用重查
            snap["spans"] = {
                k: agg[k] for k in
                ("tool_total", "tool_ok", "agent_error_nodes", "reviewer_verdict", "retried")
            }
        if judge_usage is not None:
            # 文件头红线 2：judge 用量只进这里，不进指标层
            snap[_JUDGE_TOKENS_KEY] = judge_usage
        await evaluation_repo.add_result(
            session,
            run_id=run_id,
            organization_id=organization_id,
            user_id=user_id,
            case_id=case.id,
            case_no=outcome.case_no,
            case_snapshot=snap,
            task_run_id=outcome.task_run_id,
            trace_id=outcome.trace_id,
            status=outcome.status,
            passed=outcome.passed,
            score=outcome.score,
            reasons=outcome.reasons,
            latency_ms=outcome.latency_ms,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            # 列 NOT NULL：未配单价时写 0 而不是 None（与 task_runner 的 span 同口径，
            # "不可得"记在指标层的 cost_available 上，别在源头造假）
            cost=outcome.cost if outcome.cost is not None else 0,
            failure_category=outcome.failure_category,
            note=outcome.note,
        )

    # 1) 执行（executor 默认 = 生产链路 submit_task；评测复用生产 Trace，不另建监控）
    try:
        run = await executor(
            session, question=case.input,
            organization_id=organization_id, user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 链路炸了记 error 结果行，不中断整批
        # 注意：不给这条补任何假标记 —— executor 没建出 task_run，没有可标的 run。
        logger.exception("评测 %s 用例 %s 执行链路异常（不中断整批）", run_id, case_no)
        # executor 若死在一条失败的 flush 上，会话已进 aborted：先 rollback 恢复，
        # 否则连 error 结果行都写不进（与 task_runner._fail 开头的处理是同一招）。
        # 会话生命周期课的全文已随 8b T6 迁到 app/workers/jobs.py run_evaluation 上方
        # （锚点换了、招数留在这里）：执行体永远不许复用关掉/炸掉的会话。
        await session.rollback()
        outcome = CaseOutcome(
            case_no=case_no, status="error", passed=False, score=None, reasons=[],
            task_run_id=None, trace_id=None, latency_ms=None,
            prompt_tokens=0, completion_tokens=0, cost=None,
            failure_category=None,
            note=f"执行链路异常：{type(exc).__name__}: {exc}"[:500],
        )
        await _persist(outcome, None, None)
        return outcome

    # 2) 评测标记（docs/08 §5.2 的唯一写入点）。add_result 不校验 run 归属，
    #    这条链闭合全靠 org 过滤贯穿：0 行 = run 不存在或租户没命中，是错误不是成功。
    marked = await evaluation_repo.mark_task_run_as_evaluation(
        session, task_run_id=run.id, evaluation_run_id=run_id,
        organization_id=organization_id,
    )
    if marked == 0:
        logger.error(
            "评测 %s 用例 %s 的标记未命中（mark 更新 0 行），按 error 记", run_id, case_no)
        outcome = CaseOutcome(
            case_no=case_no, status="error", passed=False, score=None, reasons=[],
            task_run_id=run.id, trace_id=getattr(run, "trace_id", None),
            latency_ms=None, prompt_tokens=0, completion_tokens=0, cost=None,
            failure_category=None,
            note="评测标记未命中：mark_task_run_as_evaluation 更新 0 行"
                 "（run 不存在或 organization_id 不匹配）"[:500],
        )
        await _persist(outcome, None, None)
        return outcome

    # 3) 展平取数（'ok' 字面量与 cost 现算都在 _flatten_for_metrics 里，这里不重复）：
    #    用一个未落库的草稿行喂给它 —— 聚合只依赖 task_run_id + 结果行的状态字段。
    pipeline_ok = getattr(run, "status", None) == "completed"  # TaskRun 态，另一套字面量
    draft = EvaluationResult(
        run_id=run_id, organization_id=organization_id, user_id=user_id,
        case_id=case.id, case_no=case_no, task_run_id=run.id,
        status="ok" if pipeline_ok else "error",
    )
    agg = (await _flatten_for_metrics(session, run_id=run_id, results=[draft]))[0]

    # 4) 判分。judge_enabled=False + default_score 是编排单测的确定性开关
    #    （真 judge 烧 token 且分数不可复现，生产路径永不走 False 分支）。
    meta = getattr(run, "meta", None)
    report = meta.get("report") if isinstance(meta, dict) else None
    if report is None:
        state = getattr(run, "state", None)
        if isinstance(state, dict):
            report = state.get("report")
    if report is not None and not isinstance(report, str):
        # report 节点真产出是结构化 dict（Report.model_dump，Task 8 冒烟实测：
        # build_judge_messages 拿 dict 直接拼字符串当场 TypeError，judge 全批哑火；
        # 单测假 executor 只给过 str，这条形状差绿桩测不到）。judge 读文本判要点，
        # JSON 化保字段结构、不丢内容（ensure_ascii=False 保住中文可读）。
        report = json.dumps(report, ensure_ascii=False)
    reasons: list[str] = []
    note: str | None = None
    judge_usage: dict | None = None
    if not pipeline_ok:
        # 链路炸了（TaskRun failed）：三态里的 error，不进判分 —— 给炸掉的执行打质量分
        # 等于把故障算成能力（score_case 同口径）。failure_category/latency 已在 agg。
        status, passed, score = "error", False, None
    elif not judge_enabled:
        score = default_score
        passed = score_case(status="ok", score=score)
        status = "ok" if passed else "failed"
        note = None if score is not None else "judge_enabled=False 且未给 default_score"
    elif not report:
        status, passed, score, note = "ok", False, None, "无报告正文，judge 未判分"
    else:
        try:
            verdict, jp, jc = await evaluation_judge.judge_case(
                question=case.input,
                key_points=list((case.expected or {}).get("key_points") or []),
                actual_output=report,
                must_include=list((case.judgement or {}).get("must_include") or []),
                must_not_include=list((case.judgement or {}).get("must_not_include") or []),
            )
        except Exception:  # noqa: BLE001 —— judge 的传输层失败也不许炸批次（None = 未出分）
            logger.exception("评测 %s 用例 %s 的 judge 调用异常", run_id, case_no)
            verdict, jp, jc = None, 0, 0
        judge_usage = {
            "prompt_tokens": int(jp), "completion_tokens": int(jc),
            "cost": _as_float_or_none(pricing.compute_cost(jp, jc)),
        }
        if verdict is None:
            # 绝不默认给过：宁可低估不虚报（score_case 对 score=None 同口径判 False）
            status, passed, score, note = "ok", False, None, "judge 未出分"
        else:
            score = int(verdict.score)
            passed = score_case(status="ok", score=score)  # D15：passed 唯一出处在产出侧
            status = "ok" if passed else "failed"
            reasons = list(verdict.reasons)

    outcome = CaseOutcome(
        case_no=case_no, status=status, passed=passed, score=score, reasons=reasons,
        task_run_id=run.id, trace_id=getattr(run, "trace_id", None),
        latency_ms=agg["latency_ms"],
        prompt_tokens=agg["prompt_tokens"],
        completion_tokens=agg["completion_tokens"],
        cost=agg["cost"],
        # W1 写侧同闸：agg 是在判分**之前**展平的（那时还不知道过没过），这里按最终的
        # passed 再收一次 —— 否则"通过的不许带失败类别"只守住了读侧，落库的列照样会
        # 出现 passed=t 且 failure_category=review_timeout 的自相矛盾行（基线就是这么来的）。
        failure_category=(None if passed else agg["failure_category"]),
        note=note,
    )
    await _persist(outcome, agg, judge_usage)
    return outcome


async def execute_evaluation(
    run_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    executor: Callable[..., Coroutine[Any, Any, Any]] | None = None,
    judge_enabled: bool = True,
    default_score: int | None = None,
) -> None:
    """worker job 本体（8b T6 起）：把 run_id 名下的数据集逐用例跑完并汇总指标。

    签名里**没有 session 参数是有意的**（请求会话在响应后就关了，见模块头）。
    幂等门（pending→running 条件 UPDATE）收口在 workers/jobs.py 的 run_evaluation，
    本函数开头的终态检查是第二道闸（直调方仍不会被 completed/failed 行追加结果）。
    executor 是 worker 侧显式传入的生产链路实名 agent_task_service.submit_task
    （R-8b-5：spec 概念执行器名的实际载体；也是编排单测留的注入点，
    judge_enabled=False + default_score 同理（真 judge 烧 token 且不可复现）。

    会话编排：run 级读 1 条 → 每条用例 1 条（结果/标记在其中 commit 完才关）→
    每条用例后再开 1 条写进度 → 收尾 1 条算指标 → 兜底 1 条置 failed。
    """
    if executor is None:
        # 惰性导入：默认走生产链路，但 import 本模块（读侧端点也用）不该把整张图拖进来
        from app.application import agent_task_service
        executor = agent_task_service.submit_task
    try:
        # ---- run 级会话：读 run + 用例列表，置 running 后即关 ----
        async with AsyncSessionLocal() as s:
            run = await evaluation_repo.get_run(
                s, run_id=run_id, organization_id=organization_id, user_id=user_id
            )
            if run is None:
                raise EvaluationNotFoundError("评测 Run 不存在")
            if run.status in _TERMINAL_STATUSES:
                # 幂等入口拒绝：completed/failed 的 run 再跑一次不许追加结果
                logger.info("评测 Run %s 已是终态 %s，跳过重复执行", run_id, run.status)
                return
            if run.dataset_id is None:
                raise EvaluationParamError("评测数据集已被删除，无法执行")
            cases = await evaluation_repo.list_cases(
                s, dataset_id=run.dataset_id, organization_id=organization_id, user_id=user_id
            )
            if not cases:
                raise EvaluationParamError("用例集为空，拒绝执行")
            dataset_id = run.dataset_id
            await evaluation_repo.update_run_progress(
                s, run_id=run_id, status="running", started=True,
                organization_id=organization_id,
            )
            await s.commit()

        # ---- 用例级：每条一开一关。单条失败只落 error 结果行，不外抛 ----
        for case_no, case_row in enumerate(cases, 1):
            async with AsyncSessionLocal() as s:
                await _execute_case(
                    s, run_id=run_id, case=case_row, case_no=case_no,
                    executor=executor, organization_id=organization_id, user_id=user_id,
                    judge_enabled=judge_enabled, default_score=default_score,
                )
                await s.commit()  # 结果行、标记都在这条会话里 commit 完再关，不留惰性读到关后
            # ---- 进度独立会话：跑完一条立刻可见（前端 §7 轮询的就是这个数）----
            async with AsyncSessionLocal() as s:
                await evaluation_repo.update_run_progress(
                    s, run_id=run_id, case_done=case_no,
                    organization_id=organization_id,
                )
                await s.commit()
            # 8b T6 Step 5 逐用例留痕：worker 的结构化日志里每用例至少一行（可对账）
            logger.info("评测批次 %s 用例 %d/%d 已跑完并记进度", run_id, case_no, len(cases))

        # ---- 收尾会话：取全部结果 → 展平 → 纯函数算指标 ----
        async with AsyncSessionLocal() as s:
            results = await evaluation_repo.list_results(
                s, run_id=run_id, organization_id=organization_id, user_id=user_id
            )
            rows = await _flatten_for_metrics(s, run_id=run_id, results=results)
            dataset = await evaluation_repo.get_dataset(
                s, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
            )
            metrics = compute_metrics(
                rows, category_scope=(dataset.category_scope if dataset else None) or []
            )
            await evaluation_repo.update_run_progress(
                s, run_id=run_id, status="completed", metrics=metrics,
                finished=True, organization_id=organization_id,
            )
            await s.commit()
    except Exception as exc:  # noqa: BLE001 —— Runner 自身出错也必须收尾
        # 整批兜底：把 run 置 failed + error_message，**绝不留在 running**
        # （docs/11 §7 记过同款僵尸：running 挂库里没人收尾，前端轮询转到天荒地老）。
        logger.exception("评测批次 %s 自身出错，置 failed", run_id)
        try:
            async with AsyncSessionLocal() as s:
                await evaluation_repo.update_run_progress(
                    s, run_id=run_id, status="failed",
                    error_message=f"评测批次执行失败：{type(exc).__name__}: {exc}"[:500],
                    finished=True, organization_id=organization_id,
                )
                await s.commit()
        except Exception:  # noqa: BLE001 —— 连收尾都写不进时只能记日志，别让异常逃出一个后台任务
            logger.exception("评测批次 %s 的 failed 收尾写入也失败", run_id)


# ---------------- 读侧（Task 6 端点直接调；命名与 repo 一侧对齐） ----------------
#
# W12：这一组同样一律带必填 user_id —— repo 收口后调用链上没有一环只按 org 读。


async def get_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> EvaluationRun:
    run = await evaluation_repo.get_run(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    if run is None:
        raise EvaluationNotFoundError("评测 Run 不存在")
    return run


async def list_runs(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    dataset_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[EvaluationRun]:
    return await evaluation_repo.list_runs(
        session, organization_id=organization_id, user_id=user_id,
        dataset_id=dataset_id, limit=limit, offset=offset,
    )


async def get_metrics(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict:
    """未完成就 409（EVAL_409001）而不是回一坨 null —— null 会被读成"指标就是 0"。"""
    run = await get_run(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    if run.status != "completed":
        raise EvaluationConflictError("评测未完成，暂无指标")
    return run.metrics or {}


async def list_results(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[EvaluationResult]:
    await get_run(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )  # 404 先于空列表
    return await evaluation_repo.list_results(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )


async def compare_runs(
    session: AsyncSession,
    *,
    base_id: uuid.UUID,
    head_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict:
    """回归对比（docs/08 §6）。两侧都必须 completed，否则 409。"""
    base = await get_run(
        session, run_id=base_id, organization_id=organization_id, user_id=user_id
    )
    head = await get_run(
        session, run_id=head_id, organization_id=organization_id, user_id=user_id
    )
    if base.status != "completed" or head.status != "completed":
        raise EvaluationConflictError("只有已完成的评测才能对比")
    diff = diff_metrics(base.metrics or {}, head.metrics or {})
    # JSONB 把对象键强转成字符串：case_results 的 int 键穿过存储层回来就是 "3"，
    # 而 /runs/{id}/result 的 case_no 是 int —— 两个端点必须同型，否则前端拿 '3'
    # 去 === 明细里的 3 会静默对不上。归一只在读取边界做，纯函数不为存储层弯腰。
    # 键的取值域 = 数字串：case_results 的键只由 compute_metrics 用 int case_no 写入
    # （NOT NULL 列，models.py:730），过 JSONB 只会变成纯数字字符串 —— int() 没有
    # 失败路径，所以这里**不加** try 防御（fixround2 对 M2 的裁定：防御代码不给
    # 不存在的场景写，与 Task 4 那条 rowcount=-1 的裁定同一逻辑）。
    for flip in diff.get("case_flips") or []:
        flip["case_no"] = int(flip["case_no"])
    # W3 的 case_coverage 里两个列表同样是 case_no，穿过 JSONB 也是数字串：
    # 与 case_flips 同一条读取边界、同一个归一，否则前端拿 '3' 去和明细里的 3 对不上。
    coverage = diff.get("case_coverage")
    if coverage:
        for key in ("only_in_base", "only_in_head"):
            coverage[key] = sorted(int(no) for no in coverage.get(key) or [])
    return diff
