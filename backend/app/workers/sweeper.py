"""arq cron 清扫器（Phase 8b spec §4）：每 60s 扫一遍崩溃窗口留下的僵尸态。

装配（settings.py）：cron(sweep_all, second={0}) —— 每分钟第 0 秒 = 60s 一轮。
四扫串行、各自自吃异常（spec §4.2「每步独立 try」，针⑧）：一轮的账允许迟到、
不允许被上一步的炸弹带崩。

三笔账（前序 Task 移交债 → 扫步对应）：
  ① prepared 态超窗无队行（T3/T4/T5/T6 共债：commit 成功但 enqueue 抛的窗口，
     行已持久化、队里无名）→ 以**路由同款 _job_id** 重入队自愈：
       agent 面 queued            → run_agent_task   agent:{run_id}
       workflow 面 pending        → workflow_execute wfexec:{run_id}
       evaluation 面 pending      → run_evaluation   eval:{run_id}
     （eval 面不收这笔账 = 孤儿 pending 永占 has_active_run_on_dataset，
      数据集级死锁，T6 review Important。）
  ② running + Task.heartbeat_at 过期 → failed + 留痕，**不重入队**
     （spec §4.2 字面：副作用已发生，自动重放=重复扣款/重复写）。
     诚实性注记（控制器点名核验）：promote-commit→首心跳之间崩溃的窗口，
     Task.heartbeat_at 里躺的是**上一次 run 盖的**陈旧戳（借钟），
     所以这窗口同样落在本桶里被咬 —— 与 task_repo.has_running_run 的重跑放行
     同向（那边超窗僵尸也放行 rerun）。首次执行（心跳恒 NULL）不咬：
     NULL 保守算活，与 has_running_run 的 is_(None) 分支一字同尺。
  ③ evaluation_runs running 超 eval_run_max_minutes 硬上限 → failed 留痕
     （该表无心跳列，config 注记已裁口径：拿 started_at 拉硬上限，宁松勿紧）。
  ④ checkpoint TTL（spec §4.4 + Phase 7 欠账 #5）：终态 run 且 finished_at 早于
     retention_days → 该 trace 的 thread 在三表 DELETE。留痕：spec 原文写
     "updated_at"，实读 task_runs **没有该列**（models.py），单时钟源改用
     finished_at（到达终态时刻，R-8b-8 同族口径）；非终态一律不碰（§4.4 字面）。

入队通道（R-8b-3 实测裁定，探针 scratch/_p8b_t7_ctx_probe.py，真 arq 0.28.0 + 真 redis）：
cron job 的 ctx = {**worker.ctx, job 元数据}，worker.ctx["redis"] 是 ArqRedis
（带 default_queue_name），实测 ctx["redis"].enqueue_job(...) 成功投队 ——
**ctx["redis"] 赢**。不接 get_redis() 单例兜底：那个客户端没有
default_queue_name，投错队列是静默丢任务，比在步内当场炸（本模块一切异常都
自吃、下轮自然重试）更坏。计数键 INCR 用 get_redis() 单例无妨——它不入队，
只是旁路计数器，与队列口径无关。
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Text, cast, column, delete, select, table, update

from app.core.audit import write_audit
from app.core.config import settings
from app.core.security import get_redis
from app.core.task_cache import write_state
from app.data.db import AsyncSessionLocal
from app.data.models import EvaluationRun, Task, TaskRun
from app.data.repositories import evaluation_repo, task_repo
from app.workers.jobs import JOB_RUN_AGENT_TASK, JOB_RUN_EVALUATION, JOB_WORKFLOW_EXECUTE

logger = logging.getLogger(__name__)

# brief 定死值（逐字）
ATTEMPT_TTL_SECONDS = 3600       # 计数键 TTL：1h 自然衰减，耗尽后不清键（防同窗再犯）
ATTEMPT_CAP = 3                  # 重入队最多 3 轮，第 4 轮（计数>3）标 failed
FAILURE_CATEGORY_SWEPT = "swept_exhausted"   # task_runs.failure_category 新值（定死）
AUDIT_ACTION = "run_swept"       # 审计 action（定死）
SCAN_LIMIT = 100                 # 单扫单轮封顶：60s 一轮，宁慢排不吞（防御性，非定死值）

# checkpoint 三表删除顺序：writes/blobs 外键指向 checkpoints，子表先行
_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
# 终态集合（§4.4「终态 run」+ 实读 TaskRun 可达终态：completed/failed/rejected）
_TERMINAL_RUN_STATUSES = ("completed", "failed", "rejected")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enqueue_pool(ctx: dict):
    """R-8b-3 实测赢家通道：ctx["redis"]（ArqRedis，自带 default_queue_name）。
    缺失/不对形状 → 当场抛，由 sweep_all 步级 try 自吃，下轮再试；不做静默兜底
    （兜底走 get_redis() 会投错队列 = 假自愈真丢单，见模块头注记）。"""
    pool = (ctx or {}).get("redis")
    if pool is None or not hasattr(pool, "enqueue_job"):
        raise RuntimeError(
            "sweeper 入队通道缺失：ctx['redis'] 不存在或非 ArqRedis（R-8b-3 赢家通道）")
    return pool


async def _bump_attempt(table_name: str, row_id: uuid.UUID) -> int:
    """sweep_attempt:{表}:{id} 轮次计数（brief 定死键形；表名=物理表名，
    task_runs / evaluation_runs 两表天然分命名空间）。INCR 原子，60s cron
    单 worker 串行本就无并发，原子性是给未来多 worker 留的底。
    首次 INCR（n==1）挂 EXPIRE 3600：此后只增不续期，TTL 自然衰减；
    耗尽（n>CAP）也不删键 —— 留计数当档，防止键窗内换个理由再犯。"""
    key = f"sweep_attempt:{table_name}:{row_id}"
    r = get_redis()
    n = int(await r.incr(key))
    if n == 1:
        await r.expire(key, ATTEMPT_TTL_SECONDS)
    return n


async def _fail_task_run(run_id: uuid.UUID, task_id: uuid.UUID, *,
                         organization_id: uuid.UUID, user_id: uuid.UUID,
                         category: str, message: str,
                         observed_status: str) -> bool:
    """task_runs 终态留痕（task_runner._fail 同款账：failed + failure_category +
    error_message + finished_at + meta 合并；Task 冗余状态同步）。

    meta 合并读后写、UPDATE 以观察到的 status 为 WHERE 守卫：库内 meta 只在终态
    转换时追加（终态转换同时改 status），抢不到守卫 = 别人已收口，让位不覆写。
    failure_category 列实读为开放词表（String(50)，errors.py 只枚举执行期分类），
    清扫侧写两值：swept_exhausted（brief 定死新值）/ internal_error（僵尸型沿用
    workflow_service._graph_for 既有词，brief 未给它定死新值）。
    """
    async with AsyncSessionLocal() as s:
        run = await s.get(TaskRun, run_id)
        if run is None:
            return False
        merged = {**(run.meta or {}), "error": message}  # spec §4.2 字面 meta.error
        res = await s.execute(
            update(TaskRun)
            .where(TaskRun.id == run_id, TaskRun.status == observed_status)
            .values(status="failed", failure_category=category,
                    error_message=message, finished_at=_now(), meta=merged)
        )
        if res.rowcount != 1:
            await s.rollback()
            return False
        # Task 冗余态同步：仅在途态推进（_fail 末尾 task.status="failed" 同向）；
        # 已终态的 task 不动，别的 run 的账不替它改口。
        task_flip = await s.execute(
            update(Task)
            .where(Task.id == task_id,
                   Task.status.in_(("pending", "queued", "running")))
            .values(status="failed")
        )
        await s.commit()
    # T8 终态镜像（8b T8 写侧接线的补账针）：sweeper 判死也是 Task.status 的终态翻转点
    # （实读：这条路径不经 task_runner._fail）。不接线则缓存挂着 running 假象最长 1h——
    # 轮询者会一直等一个 sweeper 已判死的任务。只翻成功（rowcount=1）才盖；
    # 守卫让位的竞态分支里 PG 早已终态、缓存由当时的终态写点管，这里重复盖反而
    # 可能用过期的 failed 覆掉别人刚写的合法终态。write_state 永不抛（加速面纪律）。
    if task_flip.rowcount == 1:
        await write_state(task_id, status="failed", heartbeat_at=_now(), summary=None)
    await write_audit(
        organization_id=organization_id, user_id=user_id,
        action=AUDIT_ACTION, target_type="task_run", target_id=str(run_id),
        detail={"reason": category, "message": message},
    )
    return True


# ---------------- 扫①：prepared 态超窗 → 原路由重入队 ----------------

async def _scan_prepared_runs(ctx: dict) -> None:
    """T3/T4/T5 共债（spec §4.2 第一笔）：commit 成功但 enqueue 抛 → 行在库里
    queued/pending 挂着，has_running_run 判活超窗已放行 rerun，但自愈不该等用户。
    重入队**不改行状态**：认领仍走 worker 的幂等门（promote_from_prepared），
    at-least-once 的账在门处结，清扫器只是把名字重新报给队列。"""
    pool = _enqueue_pool(ctx)
    cutoff = _now() - timedelta(minutes=settings.sweep_stale_minutes)
    async with AsyncSessionLocal() as s:
        rows = await task_repo.list_sweepable_prepared_runs(s, cutoff=cutoff, limit=SCAN_LIMIT)
    for run_id, status, task_id, organization_id, user_id in rows:
        try:
            n = await _bump_attempt("task_runs", run_id)
            if n > ATTEMPT_CAP:
                msg = (f"swept_exhausted: 重入队 {ATTEMPT_CAP} 轮仍无人认领"
                       f"（清扫计数={n}），判队列/worker 故障，标 failed")
                await _fail_task_run(run_id, task_id,
                                     organization_id=organization_id, user_id=user_id,
                                     category=FAILURE_CATEGORY_SWEPT, message=msg,
                                     observed_status=status)
                logger.error("run %s 重入队耗尽（第 %s 次扫）→ failed", run_id, n)
                continue
            if status == "queued":
                job, job_id = JOB_RUN_AGENT_TASK, f"agent:{run_id}"
            else:  # 谓词保证此分支只可能 pending+workflow（task_repo 注释同口径）
                job, job_id = JOB_WORKFLOW_EXECUTE, f"wfexec:{run_id}"
            dup = await pool.enqueue_job(job, str(run_id), _job_id=job_id)
            if dup is None:
                # 去重命中 = 队里/在飞已有一个同 id job —— 债其实已有人背，计数照加
                # 是为了「连续 N 轮 in-flight 不落地」这种病也能被耗尽桶兜住。
                logger.info("sweeper 重入队去重命中：%s 已在队/在飞", job_id)
            else:
                logger.info("sweeper 重入队 %s（第 %s 轮）", job_id, n)
        except Exception:  # noqa: BLE001 —— 单行炸不带崩本扫其余（行级 try）
            logger.exception("sweeper 扫①单行处理炸 run=%s（下轮再账）", run_id)


# ---------------- 扫②：running + 心跳过期 → 僵尸判死 ----------------

async def _scan_zombie_runs(ctx: dict) -> None:
    """spec §4.2 第二笔：不重入队（副作用不回滚），failed + meta.error 留痕，
    救活决定权交还用户 rerun。waiting_approval 型断点行由谓词排除
    （task_repo.list_zombie_running_runs 注释留痕：断点期间 run 恒 running、
    心跳停在挂起时刻，咬了 = 替人类审批判死刑，与 §4.1 审批不变量打架）。
    """
    stale_before = _now() - timedelta(minutes=settings.sweep_stale_minutes)
    async with AsyncSessionLocal() as s:
        rows = await task_repo.list_zombie_running_runs(s, stale_before=stale_before,
                                                        limit=SCAN_LIMIT)
    for run_id, status, task_id, organization_id, user_id in rows:
        try:
            msg = (f"sweeper: 心跳过期（Task.heartbeat_at 早于 "
                   f"{settings.sweep_stale_minutes} 分钟），判 worker 已死，run 标 failed；"
                   f"副作用不回滚、不自动重放，请用户 rerun")
            await _fail_task_run(run_id, task_id,
                                 organization_id=organization_id, user_id=user_id,
                                 category="internal_error", message=msg,
                                 observed_status=status)
            logger.warning("sweeper 判僵尸 run=%s → failed（不重入队）", run_id)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper 扫②单行处理炸 run=%s（下轮再账）", run_id)


# ---------------- 扫③：evaluation_runs pending 超窗 / running 超硬上限 ----------------

async def _scan_evaluation_runs(ctx: dict) -> None:
    """spec §4.2 第三笔的两臂。running 臂先行且不依赖入队通道：
    通道缺失时（针⑧语境）这笔账照样结，pending 臂的炸只炸它自己。
    留痕：EvaluationRun **无 failure_category 列**（models.py 实读，只 Text
    error_message），故 swept_exhausted 词以字面前缀进 error_message ——
    该表无冗余态可同步（它不挂 Task）。
    """
    now = _now()
    overdue_before = now - timedelta(minutes=settings.eval_run_max_minutes)
    cutoff = now - timedelta(minutes=settings.sweep_stale_minutes)
    async with AsyncSessionLocal() as s:
        overdue = await evaluation_repo.list_overdue_running_runs(
            s, started_before=overdue_before, limit=SCAN_LIMIT)
        pending = await evaluation_repo.list_sweepable_pending_runs(
            s, cutoff=cutoff, limit=SCAN_LIMIT)

    for run_id, organization_id, user_id in overdue:
        try:
            msg = (f"sweeper: running 超过 {settings.eval_run_max_minutes} 分钟硬上限"
                   f"未收口（本表无心跳列，started_at 判超），批次标 failed")
            async with AsyncSessionLocal() as s:
                res = await s.execute(
                    update(EvaluationRun)
                    .where(EvaluationRun.id == run_id, EvaluationRun.status == "running")
                    .values(status="failed", error_message=msg, finished_at=now)
                )
                await s.commit()
            if res.rowcount == 1:
                await write_audit(
                    organization_id=organization_id, user_id=user_id,
                    action=AUDIT_ACTION, target_type="evaluation_run",
                    target_id=str(run_id),
                    detail={"reason": "eval_running_overdue", "message": msg},
                )
                logger.warning("sweeper 评测批次超时判死 run=%s", run_id)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper 扫③running 臂单行炸 run=%s（下轮再账）", run_id)

    pool = _enqueue_pool(ctx)  # 通道缺失 → 抛，步级 try 自吃；running 臂已先行结清
    for run_id, organization_id, user_id in pending:
        try:
            n = await _bump_attempt("evaluation_runs", run_id)
            if n > ATTEMPT_CAP:
                msg = (f"swept_exhausted: 重入队 {ATTEMPT_CAP} 轮仍无人认领"
                       f"（清扫计数={n}），批次标 failed"
                       f"（放行 has_active_run_on_dataset，防数据集级死锁）")
                async with AsyncSessionLocal() as s:
                    res = await s.execute(
                        update(EvaluationRun)
                        .where(EvaluationRun.id == run_id, EvaluationRun.status == "pending")
                        .values(status="failed", error_message=msg, finished_at=_now())
                    )
                    await s.commit()
                if res.rowcount == 1:
                    await write_audit(
                        organization_id=organization_id, user_id=user_id,
                        action=AUDIT_ACTION, target_type="evaluation_run",
                        target_id=str(run_id),
                        detail={"reason": FAILURE_CATEGORY_SWEPT, "message": msg, "attempt": n},
                    )
                    logger.error("评测批次 %s 重入队耗尽 → failed", run_id)
                continue
            dup = await pool.enqueue_job(JOB_RUN_EVALUATION, str(run_id),
                                         str(organization_id), str(user_id),
                                         _job_id=f"eval:{run_id}")
            if dup is None:
                logger.info("sweeper 重入队去重命中：eval:%s 已在队/在飞", run_id)
            else:
                logger.info("sweeper 重入队 eval:%s（第 %s 轮）", run_id, n)
        except Exception:  # noqa: BLE001
            logger.exception("sweeper 扫③pending 臂单行炸 run=%s（下轮再账）", run_id)


# ---------------- 扫④：checkpoint TTL ----------------

async def _scan_checkpoint_ttl() -> None:
    """spec §4.4 + T11 手工清账 SQL 的机制化（Phase 7 欠账 #5）。
    thread 口径 = 运行时口径：thread_id = str(task_runs.trace_id)（task_runner ②、
    workflow_service 模块头两处同源），uuid→text 显式 cast 对齐 thread_id TEXT 列。
    EXPLAIN 已验（docker exec p6-postgres psql，提交前跑）：task_runs 侧
    status+finished_at 过滤走 seq scan（量级无妨，60s 一轮 SCAN 的是终态老行），
    checkpoints 三表侧命中 checkpoints_thread_id_idx 等 btree（Index Scan /
    Bitmap），不全表扫。非终态一律不碰：状态白名单 + finished_at 非空（NULL
    比较恒假，天然豁免没到过终态的行）。
    """
    cutoff = _now() - timedelta(days=settings.checkpoint_retention_days)
    # 8b T8 留痕修正：原先套 .subquery() 塞 in_()，SQLAlchemy 2.0 抛
    # SAWarning（Coercing Subquery object…）裸文本进 worker stderr ——
    # 打破 logging 套件针⑦a「worker 每行 json.loads 过」的制式收敛承诺
    # （cron 撞 :00 秒窗即复现，T7 跑运工窗口没压到边界）。渲染 SQL 逐字不变，
    # in_() 收 select() 是被官方推荐的原生形状，行为零差异（test_p8b_sweeper 复跑全绿）。
    threads = (
        select(cast(TaskRun.trace_id, Text))
        .where(TaskRun.status.in_(_TERMINAL_RUN_STATUSES),
               TaskRun.finished_at < cutoff,
               TaskRun.trace_id.isnot(None))
    )
    async with AsyncSessionLocal() as s:
        deleted: dict[str, int] = {}
        for tbl in _CHECKPOINT_TABLES:
            # 裸 table() 不带列就没有 .c.thread_id（首跑实炸教训）——声明用到的那一列即可，
            # DELETE 渲染不关心其余列。
            t = table(tbl, column("thread_id", Text))
            res = await s.execute(delete(t).where(t.c.thread_id.in_(threads)))
            deleted[tbl] = int(res.rowcount or 0)
        await s.commit()
    if any(deleted.values()):
        logger.info("checkpoint TTL 清账（retention=%sd）: %s",
                    settings.checkpoint_retention_days, deleted)


# ---------------- cron 入口 ----------------

async def sweep_all(ctx: dict) -> None:
    """60s 一轮的总入口：四扫串行（互不抢锁，PG 行级锁自然互斥；串行也让
    单轮峰值可控），每步独立 try —— 一步炸、其余照跑、整体不外抛（针⑧）。
    不抛是对的：cron job 抛错在 arq 侧只是重试一次的记账，而清扫本身下一轮
    天然重来，把 job 炸响没有任何收账价值。"""
    steps = (
        ("扫① prepared 重入队", lambda: _scan_prepared_runs(ctx)),
        ("扫② 僵尸判死", lambda: _scan_zombie_runs(ctx)),
        ("扫③ 评测两臂", lambda: _scan_evaluation_runs(ctx)),
        ("扫④ checkpoint TTL", _scan_checkpoint_ttl),
    )
    for name, step in steps:
        try:
            await step()
        except Exception:  # noqa: BLE001 —— 步级自吃，见模块 docstring
            logger.exception("sweeper %s 本轮炸（已自吃，下轮再账）", name)
