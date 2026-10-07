"""sweeper 的集成面（8b T7，实读 task_repo.py:274/308 + sweeper.py:96/149/187）。

分工：谓词（扫①/扫②的 SQL）与收口动作（_fail_task_run 的三方留痕）。
不演 sweep_all：它是 cron 入口，四扫串起来会把三张无关表拖进断言，
而它的"每步自吃异常"性质已经在 scratch/test_p8b_sweeper 针⑧演过（历史取证身份保持）。
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.data.models import AuditLog, Task, TaskRun
from app.data.repositories import task_repo
from app.workers import sweeper

pytestmark = [pytest.mark.db, pytest.mark.redis]


@pytest.fixture(autouse=True)
async def _purge_probe_rows(db_session):
    """用例前后各清一次本层的 task / task_run 行。

    为什么必须有：扫①②③④ 的谓词是**全库**的（sweeper 不认识 org，它就是要扫全场），
    而 `db_session` 只负责 dispose，不管行。没有这层清理，`limit=2` 那种"封顶"断言
    会因为上一条用例留下的 overdue 行而莫名变红——那是夹具的形状问题，不是产品的 bug。
    它依赖一条本层的不变式：**没有用例去读不是自己种的行**（每条用例都现造 org+user+task），
    所以清掉前人的行不会让它瞎；反过来，若将来真出现跨用例读，就得改成按 org 白名单过滤而不是删行。

    删两张表、**不删 organizations / users**：`audit_logs` 有 org 外键，
    `_fail_task_run` 那几枚针会写审计行，删 org 会撞约束——留着无害（org 名带 uuid，不会串味）。
    """
    from sqlalchemy import delete

    from app.data.models import Organization

    probe_orgs = select(Organization.id).where(
        Organization.name.like("sw-%") | Organization.name.like("p6test-org-%"))
    probe_tasks = select(Task.id).where(Task.organization_id.in_(probe_orgs))

    async def _purge():
        await db_session.execute(delete(TaskRun).where(TaskRun.task_id.in_(probe_tasks)))
        await db_session.execute(delete(Task).where(Task.id.in_(probe_tasks)))
        await db_session.commit()

    await _purge()
    yield
    await _purge()


async def _mk(db_session, *, run_status: str, task_type: str = "agent_analysis",
              task_status: str = "queued", heartbeat=None,
              created_ago_min: int = 0, run_no: int = 1):
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    from app.data.models import Organization, User

    db_session.add(Organization(id=org_id, name=f"sw-{uuid.uuid4().hex[:8]}"))
    await db_session.flush()
    db_session.add(User(id=user_id, organization_id=org_id,
                        email=f"sw-{uuid.uuid4().hex[:12]}@example.org"))
    await db_session.flush()
    task = Task(id=uuid.uuid4(), organization_id=org_id, user_id=user_id,
                title="sweeper 探针", question="这条该不该被扫到",
                task_type=task_type, status=task_status, heartbeat_at=heartbeat)
    db_session.add(task)
    await db_session.flush()
    run = TaskRun(id=uuid.uuid4(), task_id=task.id, run_no=run_no, status=run_status)
    db_session.add(run)
    await db_session.commit()
    if created_ago_min:
        from sqlalchemy import update

        await db_session.execute(
            update(TaskRun).where(TaskRun.id == run.id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=created_ago_min)))
        await db_session.commit()
    return task, run


# ---------------- 扫①：prepared 态超窗 ----------------

async def test_scan1_hits_overdue_queued_only(db_session):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)
    _, old_queued = await _mk(db_session, run_status="queued", created_ago_min=10)
    _, fresh_queued = await _mk(db_session, run_status="queued", created_ago_min=1)

    rows = await task_repo.list_sweepable_prepared_runs(db_session, cutoff=cutoff)
    ids = {r[0] for r in rows}
    assert old_queued.id in ids
    assert fresh_queued.id not in ids


async def test_scan1_distinguishes_the_two_prepared_families(db_session):
    """pending 必须配 Task.task_type='workflow' 才算"workflow 面首程"。
    不加这层限定会咬到"非 workflow 的 pending"（历史遗留 / 评测旁路），
    那是谓词注释里点名不许发生的事。"""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    _, wf = await _mk(db_session, run_status="pending", task_type="workflow",
                      task_status="pending", created_ago_min=10)
    _, other = await _mk(db_session, run_status="pending", task_type="agent_analysis",
                         task_status="pending", created_ago_min=10)
    ids = {r[0] for r in await task_repo.list_sweepable_prepared_runs(
        db_session, cutoff=cutoff)}
    assert wf.id in ids
    assert other.id not in ids


async def test_scan1_returns_identity_from_the_join(db_session):
    """队列消息与 sweeper 都不伪造身份：org/user 必须是从 tasks JOIN 回来的真值
    （run_evaluation 执行面要这三枚 UUID）。返回形状 = (run_id, status, task_id, org, user)。"""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    task, run = await _mk(db_session, run_status="queued", created_ago_min=10)
    rows = [r for r in await task_repo.list_sweepable_prepared_runs(
        db_session, cutoff=cutoff) if r[0] == run.id]
    assert len(rows) == 1
    run_id, status, task_id, organization_id, user_id = rows[0]
    assert (run_id, status, task_id) == (run.id, "queued", task.id)
    assert (organization_id, user_id) == (task.organization_id, task.user_id)


async def test_scan1_is_oldest_first_and_capped(db_session):
    """正序 + 封顶：老账先清，一轮吃不下的下轮再账（60s 一轮）。

    不写成「造三行、limit=2、断言 len(rows)==2」——那等于假设全库只有自己这三行，
    别的用例文件留一行更老的 overdue queued 就会把它顶红（而红因完全看不出跟本用例有关）。
    改用排名：newest 在全库有序结果里的位次，就是「吃掉它所需的最小 limit」。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    _, oldest = await _mk(db_session, run_status="queued", created_ago_min=30)
    _, middle = await _mk(db_session, run_status="queued", created_ago_min=20)
    _, newest = await _mk(db_session, run_status="queued", created_ago_min=10)

    ids = [r[0] for r in await task_repo.list_sweepable_prepared_runs(
        db_session, cutoff=cutoff)]
    for run in (oldest, middle, newest):
        assert run.id in ids
    assert ids.index(oldest.id) < ids.index(middle.id) < ids.index(newest.id)

    rank = ids.index(newest.id) + 1          # 1-based 位次
    capped = await task_repo.list_sweepable_prepared_runs(
        db_session, cutoff=cutoff, limit=rank - 1)
    assert len(capped) == rank - 1
    assert newest.id not in {r[0] for r in capped}


# ---------------- 扫②：running + 心跳过期 ----------------

async def test_scan2_zombie_shapes_are_separated_from_alive_ones(db_session):
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=5)
    now = datetime.now(timezone.utc)
    _, dead = await _mk(db_session, run_status="running", task_status="running",
                        heartbeat=now - timedelta(minutes=20))
    _, alive = await _mk(db_session, run_status="running", task_status="running",
                         heartbeat=now)
    _, null_beat = await _mk(db_session, run_status="running", task_status="running")
    _, awaiting = await _mk(db_session, run_status="running",
                            task_status="waiting_approval",
                            heartbeat=now - timedelta(minutes=20))
    ids = {r[0] for r in await task_repo.list_zombie_running_runs(
        db_session, stale_before=stale_before)}
    assert dead.id in ids
    assert alive.id not in ids
    # NULL 保守算活：与 has_running_run 的 is_(None) 分支一字同尺，否则判活与清扫打架
    assert null_beat.id not in ids
    # 人类审批不替 worker 判超时：咬它 = 与审批不变量打架（spec §4.2 实读裁定）
    assert awaiting.id not in ids


# ---------------- 收口：_fail_task_run 的三方留痕 ----------------

async def test_fail_task_run_writes_run_task_audit_and_cache(db_session):
    """判死是一次转换写三处：task_runs 终态 + Task 冗余态 + 审计行，外加 T8 的缓存镜像。
    少一处就是"库里死了、界面上还在跑"或"审计查不到谁判的"。"""
    now = datetime.now(timezone.utc)
    task, run = await _mk(db_session, run_status="running", task_status="running",
                          heartbeat=now - timedelta(minutes=20))
    ok = await sweeper._fail_task_run(
        run.id, task.id, organization_id=task.organization_id, user_id=task.user_id,
        category="internal_error", message="集成层判僵尸", observed_status="running")
    assert ok is True

    await db_session.refresh(run)
    await db_session.refresh(task)
    assert (run.status, run.failure_category) == ("failed", "internal_error")
    assert run.error_message == "集成层判僵尸"
    assert run.finished_at is not None
    assert run.meta["error"] == "集成层判僵尸"          # spec §4.2 字面 meta.error
    assert task.status == "failed"

    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == sweeper.AUDIT_ACTION,
                               AuditLog.target_id == str(run.id)))).scalars().all()
    assert len(rows) == 1
    assert rows[0].organization_id == task.organization_id
    assert rows[0].detail["reason"] == "internal_error"

    # T8 终态镜像：sweeper 判死也走 write_state，否则轮询者最长 1h 还在等一个已死任务
    from app.core.task_cache import read_state

    cached = await read_state(task.id)
    assert cached is not None and cached["status"] == "failed"


async def test_fail_task_run_yields_when_the_guard_loses(db_session):
    """UPDATE 以观察到的 status 为 WHERE 守卫：抢不到（别人已收口）就让位，
    不覆写别人的终态、也不写审计。返回 False 是这条竞态的唯一可见形状。"""
    task, run = await _mk(db_session, run_status="completed", task_status="completed")
    ok = await sweeper._fail_task_run(
        run.id, task.id, organization_id=task.organization_id, user_id=task.user_id,
        category=sweeper.FAILURE_CATEGORY_SWEPT, message="不该发生",
        observed_status="running")   # 我们"以为"它是 running，库里它是 completed
    assert ok is False
    await db_session.refresh(run)
    assert run.status == "completed"
    assert run.failure_category is None


# ---------------- 入队通道（R-8b-3 的失败面） ----------------

def test_enqueue_channel_shape_is_enforced():
    """ctx['redis'] 必须是带 enqueue_job 的 ArqRedis；缺失/错形状当场抛。
    不静默兜底到 get_redis()：那个客户端没有 default_queue_name，
    投错队列是**静默丢任务**，比在步内炸更坏（模块 docstring 的实测裁定）。"""
    with pytest.raises(RuntimeError, match="ArqRedis"):
        sweeper._enqueue_pool({})
    with pytest.raises(RuntimeError, match="ArqRedis"):
        sweeper._enqueue_pool({"redis": object()})
    class _Fake:
        async def enqueue_job(self, *a, **kw):
            return None
    assert sweeper._enqueue_pool({"redis": _Fake()}) is not None
