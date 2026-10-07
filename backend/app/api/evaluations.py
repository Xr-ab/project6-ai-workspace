"""Evaluations API（Phase 6 Evaluation）：docs/06 §2.7 的 7 个端点 + docs/08 §7 补的 3 个。

分层约定同 `api/agents.py`：路由只取身份 → 调 service → 选响应模型，业务判断全在 service。

三条接口层的硬边界（都是复核钉下来的，不是本地发明）：

1. **测试接缝不在此处出现**（计划硬约束）。`execute_evaluation` 的 `executor` /
   `judge_enabled` / `default_score` 只服务于编排单测，本文件对执行唯一的触发是
   入队（8b T6）：job 参数只有 run.id 与两枚身份 UUID 字符串，接缝字段永不上队；
   请求模型（`schemas/evaluation.py`）用 `extra="forbid"` 把这类字段
   当场拒成 422。能从 HTTP 关掉判分的接口 = 任何人都能产出一条看着像真测量的 run。
2. **没有重跑 / 续跑端点**。`execute_evaluation` 对 running 中的 run 会撞
   (run_id, case_no) 唯一约束，service 侧的终态守卫只管 completed / failed；
   口子不开在 HTTP 上，比在 service 里补一套"半途中断怎么续"的语义便宜。
3. **target_version 不接受客户端自报**。版本锚由 `evaluation_service` 里那一份
   `_default_target_version()` 生成（Task 5 / Task 6 指的是同一个实现，不各写一遍）。

租户隔离（计划约束 6）：每个端点都从 Depends 注入的当前登录用户（`user: CurrentUser`）
取身份，把 organization_id 与
user_id 一起传进 service（W12 起读链路 org + user 双过滤，同 org 同事之间也不互见）；
跨租户 / 跨 user 一律 404（不是 403），与 `CONV_404001` 同一套探测防护。
"""
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agents import enqueue_run_job, get_arq_pool
from app.application import evaluation_service
from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.rate_limit import rate_limit_dep
from app.data.db import get_db
from app.workers.jobs import JOB_RUN_EVALUATION
from app.schemas.evaluation import (
    CaseIn,
    CaseOut,
    CompareOut,
    DatasetCreate,
    DatasetDetailOut,
    DatasetOut,
    EvalResultCaseOut,
    PromoteCaseIn,
    RunCreate,
    RunMetricsOut,
    RunOut,
    RunResultOut,
    RunSubmitOut,
    RunSummaryOut,
)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

# 8b T9：发起评测与 agent/workflow 提交面共享 "task" 桶（spec §6 表同一行：批量烧模型）。
_rl_task = rate_limit_dep("task", lambda: settings.rate_limit_task_per_min)


def _case_rows(items: list[CaseIn] | None) -> list[dict]:
    """docs/06 的入参形状 → evaluation_repo.upsert_cases 的形状。

    `type` → `category` 的映射只在这一个地方做（文档用词与表用词的差，裁定 D11 同一类）；
    code 缺省不补 —— 续号要读库里现有编号，那是 service 的事。
    """
    return [
        {
            "code": i.code,
            "category": i.type,
            "input": i.input,
            "expected": i.expected,
            "references": i.references,
            "judgement": i.judgement,
            "tags": i.tags,
        }
        for i in (items or [])
    ]


def _with_case_count(dataset, count: int) -> DatasetOut:
    """填 case_count：数字来自 repo 的 GROUP BY 计数，响应模型不现算全表。"""
    return DatasetOut.model_validate(dataset).model_copy(update={"case_count": count})


# ---------------- 数据集 / 用例 ----------------


@router.get("/datasets", response_model=list[DatasetOut])
async def list_datasets(
    user: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> list[DatasetOut]:
    """用例集列表（docs/06 §2.7）：一条 GROUP BY 补齐本页的 case_count。"""
    organization_id, user_id = user.organization_id, user.id
    rows = await evaluation_service.list_datasets(
        session, organization_id=organization_id, user_id=user_id, limit=limit, offset=offset
    )
    counts = await evaluation_service.case_counts(
        session, dataset_ids=[r.id for r in rows], organization_id=organization_id
    )
    return [_with_case_count(r, counts.get(r.id, 0)) for r in rows]


@router.post("/datasets", response_model=DatasetOut, status_code=201)
async def create_dataset(
    user: CurrentUser,
    req: DatasetCreate,
    session: AsyncSession = Depends(get_db),
) -> DatasetOut:
    """创建用例集。cases 在同一次请求里写（docs/06 :138 就是这么写的），一个事务。"""
    organization_id, user_id = user.organization_id, user.id
    dataset = await evaluation_service.create_dataset(
        session,
        organization_id=organization_id,
        user_id=user_id,
        name=req.name,
        description=req.description,
        category_scope=req.category_scope,
        cases=_case_rows(req.cases),
    )
    counts = await evaluation_service.case_counts(
        session, dataset_ids=[dataset.id], organization_id=organization_id
    )
    return _with_case_count(dataset, counts.get(dataset.id, 0))


@router.get("/datasets/{dataset_id}", response_model=DatasetDetailOut)
async def get_dataset(
    user: CurrentUser,
    dataset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> DatasetDetailOut:
    """用例集详情：摘要 + cases 明细（code 正序，与 Run 内 case_no 同向）。"""
    organization_id, user_id = user.organization_id, user.id
    dataset = await evaluation_service.get_dataset(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    cases = await evaluation_service.list_cases(
        session, dataset_id=dataset_id, organization_id=organization_id, user_id=user_id
    )
    # case_count 走与列表页同一个 repo 计数（两处不许分叉）；不拿 len(cases) 凑数：
    # 那是"响应模型/路由自己现算"，与列表页口径不同源，将来一处加过滤另一处就漏。
    counts = await evaluation_service.case_counts(
        session, dataset_ids=[dataset_id], organization_id=organization_id
    )
    return DatasetDetailOut(
        **_with_case_count(dataset, counts.get(dataset_id, 0)).model_dump(),
        cases=[CaseOut.model_validate(c) for c in cases],
    )


@router.post("/datasets/{dataset_id}/cases", response_model=CaseOut, status_code=201)
async def add_case(
    user: CurrentUser,
    dataset_id: uuid.UUID,
    req: CaseIn,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> CaseOut:
    """新增 / 更新一条用例（docs/08 §7 的"用例新增"，落地为嵌套路径，裁定 D13）。

    同 code 再提交 = upsert 覆盖内容列（id 不动，历史结果行的 case_id 才有意义）：
    这种幂等重放返回 200，真正新登记才返回 201 —— 两者必须分得开，否则调用方
    不知道自己是加了用例还是改了用例。
    """
    organization_id, user_id = user.organization_id, user.id
    rows, created = await evaluation_service.upsert_dataset_cases(
        session,
        dataset_id=dataset_id,
        cases=_case_rows([req]),
        organization_id=organization_id,
        user_id=user_id,
    )
    response.status_code = 201 if created else 200
    return CaseOut.model_validate(rows[0])


@router.post("/datasets/{dataset_id}/cases/from-task-run", response_model=CaseOut, status_code=201)
async def promote_case_from_task_run(
    user: CurrentUser,
    dataset_id: uuid.UUID,
    req: PromoteCaseIn,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> CaseOut:
    """从生产任务一键回放沉淀用例（D9 / docs/08 §3.3 主渠道）。

    expected 由服务端置为**待标注态**（空要点 + "待补expected" 标签）——§3.3 原话
    "后续人工补 expected"。重复回放同一条 run 幂等：已存在就返回它（200），
    真新登记才 201（与 add_case 同一套"加/改要分得开"的口径）。
    """
    organization_id, user_id = user.organization_id, user.id
    case_row, created = await evaluation_service.promote_task_run_to_case(
        session,
        task_run_id=req.task_run_id,
        dataset_id=dataset_id,
        organization_id=organization_id,
        user_id=user_id,
    )
    response.status_code = 201 if created else 200
    return CaseOut.model_validate(case_row)


# ---------------- Run：起跑与轮询 ----------------


@router.post("/runs", response_model=RunSubmitOut, status_code=202,
             dependencies=[Depends(_rl_task)])
async def submit_run(
    user: CurrentUser,
    req: RunCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> RunSubmitOut:
    """发起评测：建 run（pending）、入队、立即 202；执行在 worker 进程内发生（8b T6）。

    🔴 顺序是「先建行 commit、后入队」，且入队参数只有三枚 UUID 字符串
    （run/org/user，str 化上队）——请求带来的 session 在响应返回时就被
    `get_db` 关掉了，把它（或任何 ORM 对象）交给执行侧是本端点旧形状的头号坑，
    队列世界与请求世界之间只许传 id（与 agent/workflow 面同一条防线，共用
    `api/agents.py` 的 get_arq_pool / enqueue_run_job，不各写一份变体）。
    池预检先于建行：Redis 不可达时 503 且零新行，不给 sweeper 留排不上队的
    pending 孤儿（假 202 的另一种写法）。_job_id=eval:{run.id} 去重：
    同 id 在队/在飞时二次入队返回 None，行已 pending 且队里必有人领它 → 仍 202。
    """
    organization_id, user_id = user.organization_id, user.id
    pool = get_arq_pool(request)  # 预检先行（503 分支零建行）
    run = await evaluation_service.start_run(
        session,
        dataset_id=req.dataset_id,
        target=req.target,
        organization_id=organization_id,
        user_id=user_id,
        note=req.note,
    )  # start_run 内部已 commit：行先可见，job 后到队（领 job 一侧读得到行）
    await enqueue_run_job(
        pool,
        function=JOB_RUN_EVALUATION,
        run_id=run.id,
        job_id=f"eval:{run.id}",
        extra_args=(str(organization_id), str(user_id)),
    )
    return RunSubmitOut(evaluation_id=run.id, status=run.status)


@router.get("/runs", response_model=list[RunSummaryOut])
async def list_runs(
    user: CurrentUser,
    dataset_id: uuid.UUID | None = Query(None),
    # 上限 50 与 evaluation_repo.list_runs 的钳制（min(limit,50)）同一个数：
    # docs/06 §2.7 对 GET /runs 没写数字上限，两个数只能有一个真相（fixround2 M3）。
    # 之前声明 le=200 而 repo 实给 ≤50 = 对着调用方撒谎，趋势图取 200 拿到 50 条
    # 还 200 OK —— 静默少给比当场 422 坏得多。
    limit: int = Query(50, ge=1, le=50),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> list[RunSummaryOut]:
    """Run 列表（趋势图数据源）。只出终态 run 是 repo 的口径：pending/running
    还没有指标可画，轮询进行中的状态请走 GET /runs/{id}。
    """
    organization_id, user_id = user.organization_id, user.id
    rows = await evaluation_service.list_runs(
        session,
        organization_id=organization_id,
        user_id=user_id,
        dataset_id=dataset_id,
        limit=limit,
        offset=offset,
    )
    return [RunSummaryOut.model_validate(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run_status(
    user: CurrentUser,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> RunOut:
    """评测状态 / 进度：pending / running / completed / failed 都在这一条上服务。"""
    organization_id, user_id = user.organization_id, user.id
    run = await evaluation_service.get_run(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    return RunOut.model_validate(run)


@router.get("/runs/{run_id}/metrics", response_model=RunMetricsOut)
async def get_run_metrics(
    user: CurrentUser,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> RunMetricsOut:
    """评测指标：Task 2 的 metrics 原样透传（API 层不 reshape、不改名、不筛键）。
    未完成 → 409 EVAL_409001，而不是回一坨 null（null 会被读成"指标就是 0"）。
    """
    organization_id, user_id = user.organization_id, user.id
    metrics = await evaluation_service.get_metrics(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    return RunMetricsOut.model_validate(metrics)


def _result_case(row) -> EvalResultCaseOut:
    """一条结果 → 响应项。

    `cost` 由 token 经 `evaluation_service.cost_of_row` 现算（裁定 D5）：结果列
    NOT NULL DEFAULT 0，「未配单价」与「真实 0 元」在列里分不开 —— 读列就是把
    不可得渲成免费。这里与指标层共用同一个函数，两处永不分叉。
    """
    case = EvalResultCaseOut.model_validate(row)
    case.cost = evaluation_service.cost_of_row(
        {"prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens}
    )
    return case


@router.get("/runs/{run_id}/result", response_model=RunResultOut)
async def get_run_result(
    user: CurrentUser,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> RunResultOut:
    """逐用例结果（docs/06 §2.7 :143，外层键就叫 cases）。case_no 正序。"""
    organization_id, user_id = user.organization_id, user.id
    rows = await evaluation_service.list_results(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    return RunResultOut(cases=[_result_case(r) for r in rows])


@router.get("/compare", response_model=CompareOut)
async def compare_runs(
    user: CurrentUser,
    base: uuid.UUID = Query(...),
    head: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_db),
) -> CompareOut:
    """两次 Run 的回归对比（docs/08 §6）。两侧都必须 completed，否则 409。

    diff 的形状由 evaluation_metrics.diff_metrics 决定，本层继续原样透传。
    （唯一例外在 service.compare_runs：JSONB 强转成字符串的 case_no 键在读取边界
    归一回 int，与 /runs/{id}/result 同型。纯函数不为存储层弯腰。）
    """
    organization_id, user_id = user.organization_id, user.id
    diff = await evaluation_service.compare_runs(
        session, base_id=base, head_id=head, organization_id=organization_id, user_id=user_id
    )
    return CompareOut.model_validate(diff)
