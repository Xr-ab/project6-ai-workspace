"""at-least-once 的那道门（spec §3.2，实读 task_repo.py:193/234）。

为什么必须是真库：门的实现是**一条条件 UPDATE 的 rowcount**，
「先 SELECT 再 UPDATE」与它的区别只在真并发/真行锁下才显形（模块 docstring 的
TOCTOU 论断）。unit 层拿假 session 演，等于把被测的那句话换成了自己的复述。
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.data.models import Task, TaskRun
from app.data.repositories import task_repo

pytestmark = pytest.mark.db


async def _mk(db_session, *, run_status: str, task_status: str = "queued",
              task_type: str = "agent_analysis",
              heartbeat=None, created_ago_min: int = 0):
    """造一对（task, run）。created_ago_min 覆盖 task_runs.created_at：
    判活与清扫的时钟就靠这个手动拉开（不靠 sleep，也不靠改代码里的常量）。"""
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    from app.data.models import Organization, User

    db_session.add(Organization(id=org_id, name=f"idem-{uuid.uuid4().hex[:8]}"))
    await db_session.flush()
    db_session.add(User(id=user_id, organization_id=org_id,
                        email=f"idem-{uuid.uuid4().hex[:12]}@example.org"))
    await db_session.flush()
    task = Task(id=uuid.uuid4(), organization_id=org_id, user_id=user_id,
                title="幂等门探针", question="这条能跑第二次吗",
                task_type=task_type, status=task_status, heartbeat_at=heartbeat)
    db_session.add(task)
    await db_session.flush()
    run = TaskRun(id=uuid.uuid4(), task_id=task.id, run_no=1, status=run_status)
    db_session.add(run)
    await db_session.commit()
    if created_ago_min:
        from sqlalchemy import update

        await db_session.execute(
            update(TaskRun).where(TaskRun.id == run.id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=created_ago_min))
        )
        await db_session.commit()
    return task, run


async def test_gate_opens_once_and_closes(db_session):
    """同一个 job 投两次：只有第一次推得动 rowcount（=1），第二次必须 False。"""
    task, run = await _mk(db_session, run_status="queued")
    assert await task_repo.promote_from_prepared(
        db_session, run_id=run.id, from_statuses=("queued",)) is True
    await db_session.commit()
    assert await task_repo.promote_from_prepared(
        db_session, run_id=run.id, from_statuses=("queued",)) is False


async def test_gate_stamp_is_running_plus_started_at(db_session):
    """started_at 只在此处盖（queued 建行时不盖）：它从此诚实表示「真开跑」。"""
    task, run = await _mk(db_session, run_status="queued")
    assert run.started_at is None
    assert await task_repo.promote_from_prepared(
        db_session, run_id=run.id, from_statuses=("queued",)) is True
    await db_session.commit()
    await db_session.refresh(run)
    await db_session.refresh(task)
    assert run.status == "running"
    assert run.started_at is not None
    # 门内第二笔（T7 Important-2 / 8b T10 移交界）：promote 的同一事务就盖心跳
    assert task.heartbeat_at is not None


async def test_closed_gate_does_not_stamp_the_liveness_clock(db_session):
    """⑧⑨ 针的字面兑现：门不开（rowcount≠1）时**一个字都不碰 tasks**。
    否则"没开跑的行"也会有活性钟，判活器就会替一个从未执行的 run 挡重跑。"""
    task, run = await _mk(db_session, run_status="completed", task_status="completed")
    ok = await task_repo.promote_from_prepared(
        db_session, run_id=run.id, from_statuses=("queued",))
    assert ok is False
    await db_session.refresh(task)
    assert task.heartbeat_at is None
    assert task.status == "completed"
    assert run.status == "completed"


async def test_from_statuses_parameter_serves_two_families(db_session):
    """一条 SQL 服务两族：agent 面喂 ("queued",)，workflow 面喂 ("pending",)。
    喂错家族必须不开门 —— 这是两族状态并行现状（spec §4.3）唯一的边界守卫。"""
    _, agent_run = await _mk(db_session, run_status="queued")
    _, wf_run = await _mk(db_session, run_status="pending", task_type="workflow")
    assert await task_repo.promote_from_prepared(
        db_session, run_id=wf_run.id, from_statuses=("queued",)) is False
    assert await task_repo.promote_from_prepared(
        db_session, run_id=agent_run.id, from_statuses=("pending",)) is False
    assert await task_repo.promote_from_prepared(
        db_session, run_id=wf_run.id, from_statuses=("pending",)) is True
    await db_session.commit()


async def test_has_running_run_covers_both_clocks(db_session):
    """R-8b-8 单钟纪律：running 分支看 Task.heartbeat_at，queued 分支看 created_at。
    四格真值表一次演完（(状态 × 时钟新旧)），借错表的话这里就红。"""
    fresh, _ = datetime.now(timezone.utc), None
    old = fresh - timedelta(minutes=10)

    # ① queued + 新建：活 ⇒ 拦重跑
    t1, r1 = await _mk(db_session, run_status="queued")
    assert await task_repo.has_running_run(db_session, task_id=t1.id) is True
    # ② queued + 超 5 分钟：created_at 超窗 ⇒ 放行（交给 sweeper 重入队自愈）
    t2, r2 = await _mk(db_session, run_status="queued", created_ago_min=10)
    assert await task_repo.has_running_run(db_session, task_id=t2.id) is False
    # ③ running + 心跳新鲜：活
    t3, r3 = await _mk(db_session, run_status="running", heartbeat=fresh)
    assert await task_repo.has_running_run(db_session, task_id=t3.id) is True
    # ④ running + 心跳过期：僵尸 ⇒ 放行 rerun
    t4, r4 = await _mk(db_session, run_status="running", heartbeat=old)
    assert await task_repo.has_running_run(db_session, task_id=t4.id) is False
    # ⑤ running + 心跳 NULL：存量行保守算活（与 sweeper 的 IS NOT NULL 同尺）
    t5, r5 = await _mk(db_session, run_status="running")
    assert await task_repo.has_running_run(db_session, task_id=t5.id) is True


async def test_running_gate_survives_a_second_session(db_session):
    """门在**另一个会话**里也认：另一条连接读到同一行状态，
    证明它靠的是库里的行而不是会话内存 —— worker 领 job 的真实形状。"""
    from app.data.db import AsyncSessionLocal

    task, run = await _mk(db_session, run_status="queued")
    await db_session.commit()
    async with AsyncSessionLocal() as other:
        assert await task_repo.promote_from_prepared(
            other, run_id=run.id, from_statuses=("queued",)) is True
        await other.commit()
    # AsyncSession 的 expire→再读必须走 await（直接访问过期对象的属性会 MissingGreenlet）。
    # refresh 开一条新事务重读该行 ⇒ 看见 other 连接已提交的 'running'，正是"跨会话认门"的证明。
    await db_session.refresh(run)
    assert await task_repo.promote_from_prepared(
        db_session, run_id=run.id, from_statuses=("queued",)) is False
