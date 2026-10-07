"""评测四表的数据访问（Phase 6 Evaluation，docs/08 §3.2 / §7）。

Repository 层约定同 task_repo：只读写数据、不做业务判断，查不到返回 None / []，
不抛业务异常（404 由 service 层判）；**所有查询强制带 organization_id 过滤**
（docs/05 §1.4）。Phase 6 记忆侧刚在「少一个身份过滤」上栽过一次（list_recent_runs），
这条规矩不靠调用方传参干净来兜底。

W12 收口（Phase 7 Task 2）：七个**读**函数（get_dataset / list_datasets /
list_cases / count_cases / get_run / list_runs / list_results）在 org 之外再带
**必填 keyword-only 的 user_id**，对齐 task_repo.get_task 的 org+user 双过滤形状。
user_id 不给默认 —— "忘了传"必须在调用处当场 TypeError 炸，而不是运行期漏过滤。
写侧与 has_active_run_on_dataset（W11，量的是活跃性不是归属）保持 org 级，见各自注释。

两个签名上的例外与处理：
    update_run_progress / mark_task_run_as_evaluation 的接口签名（计划 Task 4 Interfaces，
    Task 5 Runner 按它逐字调用）不带 organization_id —— 尊重冻结签名，org 做成
    **可选关键字**：传了就过滤（Runner 应传），不传按主键直取。这不放松隔离：
    两个函数的 run_id / task_run_id 都由调用方先过 get_run(带 org + user，W12) 拿到。

三条不许踩的坑（计划 Step 4 点名）：
    1. upsert_cases **不许先全删再全插**：evaluation_results.case_id 引用用例
       （ondelete=SET NULL），删了重插会把历史结果指向的行毁掉，"当时测的是哪条"
       就永远丢了。幂等键是 (dataset_id, code)：命中就地 UPDATE，未命中才 INSERT。
    2. progress 由库现算（case_done/case_total），只存整数，case_total=0 记 0 不除零。
    3. started_at **只在为空时填**：同一个 Run 重跑/多次 update 不许覆盖第一次的开始时刻。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import (
    EvaluationCase,
    EvaluationDataset,
    EvaluationResult,
    EvaluationRun,
    Task,
    TaskRun,
)


def _now() -> datetime:
    """带时区的当前时刻。列是 DateTime(timezone=True)，写 naive datetime 会被
    asyncpg 按服务器时区误读，这里统一 UTC aware。"""
    return datetime.now(timezone.utc)


# ---------------- 数据集 / 用例 ----------------


async def create_dataset(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    description: str | None,
    category_scope: list[str],
) -> EvaluationDataset:
    ds = EvaluationDataset(
        organization_id=organization_id,
        user_id=user_id,
        name=name,
        description=description,
        category_scope=category_scope,
    )
    session.add(ds)
    await session.flush()
    return ds


async def get_dataset(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> EvaluationDataset | None:
    """W12 收口：org + user 双过滤（对齐 task_repo.get_task 的形状）——
    同 org 的同事也不许按 id 直读彼此的数据集。user_id 必填无默认：
    "忘了传"必须当场 TypeError 炸，而不是运行期悄悄漏。"""
    stmt = select(EvaluationDataset).where(
        EvaluationDataset.id == dataset_id,
        EvaluationDataset.organization_id == organization_id,
        EvaluationDataset.user_id == user_id,
    )
    return await session.scalar(stmt)


async def list_datasets(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[EvaluationDataset]:
    stmt = (
        select(EvaluationDataset)
        .where(
            EvaluationDataset.organization_id == organization_id,
            EvaluationDataset.user_id == user_id,
        )
        .order_by(EvaluationDataset.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(stmt))


async def upsert_cases(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    cases: list[dict],
) -> int:
    """按 (dataset_id, code) 幂等写入用例，返回处理条数。

    命中 → 只覆盖内容列（category/input/expected/references/judgement/tags），
    **id 与 created_at 不动** —— 历史结果的 case_id 指的就是这个 id；
    未命中 → 新建。绝不全删重插（模块头坑 1）。
    """
    existing = {
        c.code: c
        for c in await session.scalars(
            select(EvaluationCase).where(
                EvaluationCase.dataset_id == dataset_id,
                EvaluationCase.organization_id == organization_id,
            )
        )
    }
    count = 0
    for case in cases:
        code = case["code"]
        content = {
            "category": case["category"],
            "input": case["input"],
            # JSONB 非空列：调用方没给时落成 {} / []，与 server_default 口径一致
            "expected": case.get("expected") or {},
            "references": case.get("references") or {},
            "judgement": case.get("judgement") or {},
            "tags": case.get("tags") or [],
        }
        row = existing.get(code)
        if row is None:
            session.add(
                EvaluationCase(
                    organization_id=organization_id,
                    user_id=user_id,
                    dataset_id=dataset_id,
                    code=code,
                    **content,
                )
            )
        else:
            for key, value in content.items():
                setattr(row, key, value)
        count += 1
    await session.flush()
    return count


async def list_cases(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[EvaluationCase]:
    """**code 正序**：Run 内 case_no = 本列表下标 + 1，排序稳则两次 Run 的 case_no
    才对得齐（diff 靠它）。仍带 org + user 双过滤（W12）—— 它虽由 Runner 在建 Run 时调用，
    dataset_id 终究是可被伪造的入参。"""
    stmt = (
        select(EvaluationCase)
        .where(
            EvaluationCase.dataset_id == dataset_id,
            EvaluationCase.organization_id == organization_id,
            EvaluationCase.user_id == user_id,
        )
        .order_by(EvaluationCase.code.asc())
    )
    return list(await session.scalars(stmt))


async def count_cases(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """**单集**用例计数（seed 自检与接口测试的交叉核对口径）。

    它不是"列表页 case_count 的唯一口径"（fixround2 M1 订正）：Task 6 起列表页与
    详情页的 case_count 都走 count_cases_by_datasets（一次 GROUP BY），本函数在
    生产路径已零调用 —— 留着只为核对 by_datasets 的数没有分叉
    （scratch/seed_eval_dataset.py 与 test_evaluations_api.py 各用一次）。
    WHERE 必须与 by_datasets 同口径再加 user_id（W12 收口：dataset_id +
    organization_id + user_id）：
    带 org/user 是因为不带就等于允许拿别人的 dataset_id 探测其数据集大小。
    """
    stmt = (
        select(func.count(EvaluationCase.id))
        .where(
            EvaluationCase.dataset_id == dataset_id,
            EvaluationCase.organization_id == organization_id,
            EvaluationCase.user_id == user_id,
        )
    )
    return int(await session.scalar(stmt) or 0)


async def count_cases_by_datasets(
    session: AsyncSession,
    *,
    dataset_ids: list[uuid.UUID],
    organization_id: uuid.UUID,
) -> dict[uuid.UUID, int]:
    """数据集列表页的 case_count：一次 GROUP BY 出全部，不按数据集循环 count（避免 N+1）。

    WHERE 口径自 W12 起与 count_cases **不再相同**：本函数仍 org 级（批量计数不是
    归属视图，入参 dataset_ids 由已收口的 list_datasets 供给）。同一列表页的
    详情计数（count_cases）带 user_id —— 生产路径两者仍对同一 dev 身份等值。
    没有用例的数据集不出现在结果里（调用方 .get(id, 0)），这是 GROUP BY 的语义，
    不是"计数失败"。
    """
    if not dataset_ids:
        return {}
    stmt = (
        select(EvaluationCase.dataset_id, func.count(EvaluationCase.id))
        .where(
            EvaluationCase.dataset_id.in_(dataset_ids),
            EvaluationCase.organization_id == organization_id,
        )
        .group_by(EvaluationCase.dataset_id)
    )
    return {row[0]: int(row[1]) for row in await session.execute(stmt)}


# ---------------- 评测 Run ----------------


async def create_run(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    target: str,
    target_version: str,
    note: str | None,
    case_total: int,
) -> EvaluationRun:
    """初始态 pending/0。status/progress/case_done 显式写 Python 值而不是依赖
    server_default：expire_on_commit=False 时服务端默认值不回读，commit 后
    调用方读 run.status 会是 None —— 值与默认一致，图个「拿到的对象立即可用」。"""
    run = EvaluationRun(
        organization_id=organization_id,
        user_id=user_id,
        dataset_id=dataset_id,
        target=target,
        target_version=target_version,
        note=note,
        status="pending",
        progress=0,
        case_total=case_total,
        case_done=0,
    )
    session.add(run)
    await session.flush()
    return run


async def get_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> EvaluationRun | None:
    stmt = select(EvaluationRun).where(
        EvaluationRun.id == run_id,
        EvaluationRun.organization_id == organization_id,
        EvaluationRun.user_id == user_id,
    )
    return await session.scalar(stmt)


async def list_runs(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    dataset_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[EvaluationRun]:
    """趋势图数据源：只取终态（completed/failed —— pending/running 没有指标可画），
    created_at 倒序，limit 封顶 50（docs/08 §7 轮询/趋势都不该一次拖穿表）。

    dataset_id=None = 不限数据集（Task 6 的 GET /runs?dataset_id= 是可选参数）。
    user_id 收口（W12）：同事的批次不进我的趋势图。
    """
    stmt = (
        select(EvaluationRun)
        .where(
            EvaluationRun.organization_id == organization_id,
            EvaluationRun.user_id == user_id,
            EvaluationRun.status.in_(("completed", "failed")),
        )
        .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        .limit(max(1, min(int(limit), 50)))
        .offset(max(0, offset))
    )
    if dataset_id is not None:
        stmt = stmt.where(EvaluationRun.dataset_id == dataset_id)
    return list(await session.scalars(stmt))


async def has_active_run_on_dataset(
    session: AsyncSession, *, dataset_id: uuid.UUID, organization_id: uuid.UUID
) -> bool:
    """该数据集名下是否已有未完成（pending/running）的评测批次 —— W11 叠跑闸门的判据。

    只算活跃态：completed/failed 是历史，不挡新批次（挡了就变成"一个数据集只能测一次"）。
    organization_id 照模块头规矩强制带上 —— dataset_id 是外部入参，不靠调用方传参干净。
    """
    row = await session.execute(
        select(EvaluationRun.id).where(
            EvaluationRun.dataset_id == dataset_id,
            EvaluationRun.organization_id == organization_id,
            EvaluationRun.status.in_(("pending", "running")),
        ).limit(1)
    )
    return row.scalar_one_or_none() is not None


async def promote_from_pending(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """8b T6 评测面的幂等门：条件 UPDATE 把 pending 行认领成 running。

    与 task_repo.promote_from_prepared 同一条纪律（spec §3.2）：**不用「先 SELECT 再
    UPDATE」**——execute_evaluation 开头的入口检查正是那个形状（读 status → 非终态就
    往下推），两步之间另一个领用者插进来就是 TOCTOU；单条 UPDATE 的 WHERE 谓词原生
    没有这个窗口。rowcount==0 的三种情形都该被调用方当「重复投递」处理：行不存在、
    租户没命中、状态已非 pending（在飞/终态）。
    谓词只认 pending：旧的入口检查放行 running（崩溃残留），条件 UPDATE 把它拒在外面
    ——「半路重投不重跑」是补门要结的账，不是顺手收紧。
    started_at 在这里盖（与 promote_from_prepared 同口径：它诚实表示「真开跑」）；
    update_run_progress 的 `started and started_at is None` 分支由此退化为兜底。
    不 commit —— repo 老纪律：事务边界归调用方（worker 领 job 后自行提交）。
    """
    result = await session.execute(
        update(EvaluationRun)
        .where(
            EvaluationRun.id == run_id,
            EvaluationRun.organization_id == organization_id,
            EvaluationRun.user_id == user_id,
            EvaluationRun.status == "pending",
        )
        .values(status="running", started_at=_now())
    )
    return result.rowcount == 1


async def list_sweepable_pending_runs(
    session: AsyncSession, *, cutoff: datetime, limit: int = 100
) -> list[tuple]:
    """sweeper 扫③ pending 臂：超窗仍 pending 的批次（enqueue 崩在 commit 之后）。

    与 task_repo.list_sweepable_prepared_runs 同一笔账的评测面（spec §4.2），
    活性时钟同为 created_at（pending 行没有 started_at —— 它只在
    promote_from_pending 认领时盖，同 queued 族不借别的钟的纪律 R-8b-8）。
    这笔账不收的后果比 agent 面更重：孤儿 pending 会被
    has_active_run_on_dataset 永久认活，把整个数据集的叠跑闸门钉死
    （数据集级死锁，T6 review Important）。
    系统级清扫扫描（同 promote_from_pending 的按主键形状）：WHERE 里量的是
    活跃性+时刻、不是归属，归属从行里**读出来**给 sweeper 重入队与审计用
    —— run_evaluation 的入队签名要 (run_id, org, user) 三枚字符串。
    返回 (run_id, organization_id, user_id)。
    """
    stmt = (
        select(EvaluationRun.id, EvaluationRun.organization_id, EvaluationRun.user_id)
        .where(EvaluationRun.status == "pending", EvaluationRun.created_at < cutoff)
        .order_by(EvaluationRun.created_at.asc())
        .limit(limit)
    )
    return [tuple(r) for r in await session.execute(stmt)]


async def list_overdue_running_runs(
    session: AsyncSession, *, started_before: datetime, limit: int = 100
) -> list[tuple]:
    """sweeper 扫③ running 臂：started_at 早于硬上限仍未收口的批次。

    本表无心跳列（models 实读，config.eval_run_max_minutes 注记同款），
    判活只剩 started_at 拉硬上限这一把尺 —— 120 分钟对 10 用例全链最坏实测
    宁松勿紧（误杀 = 丢真在跑的批次结果）。started_at IS NULL 的 running
    不咬：认领必盖 started_at，NULL+running 是数据缺陷不是崩溃窗，
    保守与 task 面 NULL 心跳同口径。返回 (run_id, organization_id, user_id)
    供 sweeper 标 failed + 审计（系统级扫描，说明见 list_sweepable_pending_runs）。
    """
    stmt = (
        select(EvaluationRun.id, EvaluationRun.organization_id, EvaluationRun.user_id)
        .where(
            EvaluationRun.status == "running",
            EvaluationRun.started_at.isnot(None),
            EvaluationRun.started_at < started_before,
        )
        .order_by(EvaluationRun.started_at.asc())
        .limit(limit)
    )
    return [tuple(r) for r in await session.execute(stmt)]


async def update_run_progress(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    status: str | None = None,
    case_done: int | None = None,
    metrics: dict | None = None,
    error_message: str | None = None,
    started: bool = False,
    finished: bool = False,
    organization_id: uuid.UUID | None = None,
) -> None:
    """Runner 的进度回写口。None 入参 = 不动该列（error_message 想清空请走 service 层）。

    progress 现算：case_done/case_total 取整数百分比，case_total=0 记 0（不除零）。
    started_at 只填一次（重跑不覆盖）；finished_at 每次 finished=True 都刷 ——
    "最后一次到达终态的时刻"更有用，且终态转换本就幂等。
    organization_id 传了就加过滤（Runner 应传），不传按主键直取（见模块头例外说明）。
    """
    stmt = select(EvaluationRun).where(EvaluationRun.id == run_id)
    if organization_id is not None:
        stmt = stmt.where(EvaluationRun.organization_id == organization_id)
    run = await session.scalar(stmt)
    if run is None:
        return
    if status is not None:
        run.status = status
    if case_done is not None:
        run.case_done = case_done
        run.progress = int(case_done * 100 / run.case_total) if run.case_total else 0
    if metrics is not None:
        run.metrics = metrics
    if error_message is not None:
        run.error_message = error_message
    if started and run.started_at is None:
        run.started_at = _now()
    if finished:
        run.finished_at = _now()
    # commit 交给调用方：Runner 要「改进度」和「落结果」 share 同一个事务边界
    # （见 task_repo 模块头对写侧事务归属的同款判断）。


async def add_result(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    **fields,
) -> EvaluationResult:
    """单条用例结果。fields 直传模型列（case_id/case_no/case_snapshot/status/
    passed/score/reasons/latency_ms/prompt_tokens/completion_tokens/cost/...），
    本函数不做任何指标加工 —— 扁平化成指标 rows 是 Runner/指标层的活。

    (run_id, case_no) 唯一约束撞了会抛 IntegrityError，由调用方决定重跑语义。
    """
    row = EvaluationResult(
        run_id=run_id,
        organization_id=organization_id,
        user_id=user_id,
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def list_results(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[EvaluationResult]:
    """一次 Run 的全部结果，case_no 正序（与用例 code 正序同向，详情页对照用）。

    case_id 可为 NULL（用例被删后 SET NULL，历史结果仍在）—— 排序与渲染都不许假设它非空。
    user_id 收口（W12）：结果行的 user_id 与 run 同源写入（add_result），
    同 run 同归属，双过滤与 service 层 get_run(带 user) 的 404 判据一致。
    """
    stmt = (
        select(EvaluationResult)
        .where(
            EvaluationResult.run_id == run_id,
            EvaluationResult.organization_id == organization_id,
            EvaluationResult.user_id == user_id,
        )
        .order_by(EvaluationResult.case_no.asc())
    )
    return list(await session.scalars(stmt))


# ---------------- 评测 Trace 标记（docs/08 §5.2） ----------------


async def mark_task_run_as_evaluation(
    session: AsyncSession,
    *,
    task_run_id: uuid.UUID,
    evaluation_run_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
) -> int:
    """把一次执行标记为评测 Run 名下（只 UPDATE run_type / evaluation_run_id 两列）。

    返回值 = 实际被 UPDATE 的行数：0 表示一条都没打上（run 不存在或organization_id
    过滤没命中）—— 静默 no-op 与成功在旧签名里分不开，Runner（Task 5）拿这个数报错，
    别把跨租户的标记请求当成做完了。

    **全库唯一写 task_runs.run_type 的口子**：产品路径（task_runner / API）从不写
    非默认值，评测标记只能从这里来 —— 别处再写就把「产品任务列表按 run_type 过滤」
    这条口径破坏了。除这两列外不碰任何列：状态 / 耗时 / 快照留在原处，
    评测只引用执行痕迹不复制（§5.2）。

    organization_id 传了则要求该 task_run 归属此 org（经 tasks 反查，task_runs 无 org 列，
    同 task_repo 的 JOIN 规矩）；不传按主键直取（见模块头例外说明）。
    """
    stmt = update(TaskRun).where(TaskRun.id == task_run_id)
    if organization_id is not None:
        stmt = stmt.where(
            TaskRun.task_id.in_(
                select(Task.id).where(Task.organization_id == organization_id)
            )
        )
    result = await session.execute(
        stmt.values(run_type="evaluation", evaluation_run_id=evaluation_run_id)
    )
    return int(result.rowcount or 0)
