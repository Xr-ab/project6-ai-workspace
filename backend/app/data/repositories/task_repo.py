"""任务 / 执行 / agent span 的数据访问（Phase 5：任务列表·详情 API 的读侧）。

Repository 层约定同 conversation_repo：只读写数据、不做业务判断，查不到返回 None；
所有查询强制带 organization_id 过滤（数据隔离，见 docs/05 §1.4），不依赖上层传参是否干净。

为什么写侧（建 task / task_run / agent_run）不在这里：
    执行过程要「边跑边 commit」做可观测（见 task_runner ①③⑤），事务边界天然在执行层里；
    本仓库只承担 API 的查询，两者不重叠。

task_runs 表本身没有 organization_id 列（它挂在 task 下），所以按 run_id 查它时
必须 JOIN 回 tasks 再按 org 过滤 —— 否则拿到别人的 run_id 就能直读，隔离破了。
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import AgentRun, Task, TaskRun


# ---------------- 任务 ----------------


def product_task_filter() -> ColumnElement[bool]:
    """「排除评测批次造的 task」——全仓唯一谓词（docs/06 §7.1 的 stats/列表同口径）。

    为什么必须是函数而不是两处各抄一遍 SQL：这条谓词同时决定「产品任务列表」和
    「Dashboard 数字」的边界，一旦分叉，就会出现 Dashboard 说 12 个、列表里只有 3 个
    这种自相矛盾的产品（spec §3.1 scope 段的实因）。

    写法用 `NOT IN (子查询)` 而不是 JOIN + DISTINCT：前者走 ix_task_runs_run_type，
    后者要在结果集上去重、还会改变分页语义（原 list_tasks 注释的理由，一字未改地搬过来）。
    task_runs.task_id 是 NOT NULL 外键，子查询不会出 NULL —— NOT IN 遇 NULL 会把整个
    条件判成"一行都不匹配"，那是这条写法唯一真正危险的地方，而它被列定义挡掉了。
    """
    return Task.id.not_in(
        select(TaskRun.task_id).where(TaskRun.run_type == "evaluation")
    )


async def get_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Task | None:
    stmt = select(Task).where(
        Task.id == task_id,
        Task.organization_id == organization_id,
        Task.user_id == user_id,
    )
    return await session.scalar(stmt)


async def list_tasks(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    status: str | None = None,
    task_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    """任务列表：可按 status / task_type 过滤，最近创建的排前面（docs/06 §2.4）。

    **评测批次造的 task 不进这个列表**（Phase 6 Evaluation，docs/08 §5.2）：Runner 是
    按用例调 `agent_task_service.submit_task` 的，一条用例 = 一条新 Task，跑完 10 条
    用例的批次就往用户的产品列表里塞 10 条没人问过的任务（两个批次 = 20 条）。
    过滤只能落在 Task 上：`run_type` 在 task_runs 里，Task 侧没有这一列 ——
    只滤 run 的话任务照旧出现在列表里，点进去还是评测数据。

    写法用 `NOT IN (子查询)` 而不是 JOIN + DISTINCT：前者走 ix_task_runs_run_type
    这个专门的索引，后者要在结果集上去重、还会改变分页语义（DISTINCT 之后再 LIMIT）。
    谓词本体已抽成 `product_task_filter()`（Phase 9a：Dashboard 数字与本列表必须同口径），
    NULL 险与索引理由见那里。
    要读评测的执行痕迹请走 /api/v1/evaluations/*（那边按 evaluation_run_id 定位）。
    """
    stmt = select(Task).where(
        Task.organization_id == organization_id,
        Task.user_id == user_id,
        product_task_filter(),
    )
    if status:
        stmt = stmt.where(Task.status == status)
    if task_type:
        stmt = stmt.where(Task.task_type == task_type)
    stmt = stmt.order_by(Task.created_at.desc()).limit(limit).offset(offset)
    return list(await session.scalars(stmt))


async def is_evaluation_task(
    session: AsyncSession, *, task_id: uuid.UUID, organization_id: uuid.UUID
) -> bool:
    """该 task 名下是否存在任一 run_type='evaluation' 的执行 —— W11 产品入口闸门的判据。

    与 list_tasks 的 `NOT IN (run_type='evaluation' 子查询)` 同源口径：评测 task 的定义
    就是「名下有评测 run 的 task」，两处若分叉，列表藏得住、详情就藏不住。
    task_runs 无 org 列（见模块头），组织过滤只能 JOIN 回 tasks。
    """
    row = await session.execute(
        select(TaskRun.id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            TaskRun.task_id == task_id,
            Task.organization_id == organization_id,
            TaskRun.run_type == "evaluation",
        )
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


# ---------------- 执行（task_runs）----------------


async def get_task_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> TaskRun | None:
    """按 run_id 取一次执行；JOIN tasks 补组织过滤（task_runs 无 org 列，见模块头）。

    user_id 默认不过滤（评估等内部场景）；API 读路径必须传——
    同组织是同事，"最近事件"这种提问内容也不该互相可见。
    """
    stmt = (
        select(TaskRun)
        .join(Task, Task.id == TaskRun.task_id)
        .where(TaskRun.id == run_id, Task.organization_id == organization_id)
    )
    if user_id is not None:
        stmt = stmt.where(Task.user_id == user_id)
    return await session.scalar(stmt)


async def get_latest_run(session: AsyncSession, *, task_id: uuid.UUID) -> TaskRun | None:
    """取某任务最近一次执行（run_no 最大者）。run_no 是「第几次跑」的权威编号。"""
    stmt = (
        select(TaskRun)
        .where(TaskRun.task_id == task_id)
        .order_by(TaskRun.run_no.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def list_recent_runs(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    status: str | None = None,
    limit: int = 50,
    organization_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> list[TaskRun]:
    """某任务的执行记录，**按 run_no 正序**返回；limit 截的是「最近 N 条」不是「最旧 N 条」。

    所以必须先 DESC 再 LIMIT，最后把结果翻回正序 —— 直接 ASC + LIMIT 拿到的是
    最早 N 条，追问链一超过窗口就会"最近几轮的记忆全没了"，而且只在跑够多轮后才显形。

    status 给定则只取该状态：记忆继承只要 completed（docs/11 §7，failed 快照照写但永不回读）。
    走 (task_id, run_no) 这条 UNIQUE 约束自带的索引，state 列不进 WHERE（§3.1 不加索引）。

    传了 identity（org/user 任一）就 JOIN 回 tasks 按归属过滤 —— 与模块头「不依赖上层
    传参干净」同一条规矩：task_id 终究是外部入参，调用方校验过一次的判据不该成为
    本函数唯一的防线（记忆读路径 docs/11 §7 两处都传）；不传则退化为纯内部查询。
    """
    stmt = select(TaskRun).where(TaskRun.task_id == task_id)
    if organization_id is not None or user_id is not None:
        # task_runs 自己没有 org/user 列，身份过滤只能 JOIN 回 tasks（见模块头）
        stmt = stmt.join(Task, Task.id == TaskRun.task_id)
        if organization_id is not None:
            stmt = stmt.where(Task.organization_id == organization_id)
        if user_id is not None:
            stmt = stmt.where(Task.user_id == user_id)
    if status:
        stmt = stmt.where(TaskRun.status == status)
    stmt = stmt.order_by(TaskRun.run_no.desc()).limit(limit)
    rows = list(await session.scalars(stmt))
    return list(reversed(rows))


async def count_runs(session: AsyncSession, *, task_id: uuid.UUID) -> int:
    stmt = select(func.count(TaskRun.id)).where(TaskRun.task_id == task_id)
    return int(await session.scalar(stmt) or 0)


async def has_running_run(session: AsyncSession, *, task_id: uuid.UUID) -> bool:
    """该任务是否已有一条 **活着** 的 queued/running 执行 —— 重跑/重复提交的前置校验。

    Phase 8b（spec §4.3）判活集合含 ("queued", "running")：202 世界里
    「已入队、worker 尚未领走」的窗口同样不许叠第二条提交 ——
    否则双击/重试能在同一 task 下排起两串队列，执行语义直接碎掉。

    但两族状态的**活性时钟不同**（R-8b-8），各自走各自分支，OR 合并：

    - running 行：Phase 7 心跳判活口径一字不动（docs/11:253 收账）——
      running 且（heartbeat_at IS NULL —— 存量行没这列值，保守算活、行为不变；
      或心跳未过期 5 分钟）。心跳过期 = 僵尸放行，让重跑能救回来。
      心跳列在 tasks 上（task_runs 没有），所以要 JOIN 回 tasks —— 与模块头
      「按 run 查必须 JOIN 回 tasks」是同一形状。
    - queued 行：活性时钟是 created_at，窗口同上。理由：心跳是 runner 开跑后才盖
      （task_runner.py touch_heartbeat，progress 推进同事务），queued 行根本没有
      心跳这回事 —— 拿 Task.heartbeat_at 判 queued 死活是借错了表：NULL 分支会让
      超窗 queued 永占 409，而重跑场景下该列存的是**上一次** run 的时间戳，
      新 queued 行被旧钟误判死、判活形同虚设。created_at 建行即盖，诚实。
      超窗即不拦：sweeper（T7）会对超窗 queued 重入队自愈（spec §4.2），
      与本判活的时限同向 —— 闸门放到不该等的行，交给 sweeper 救，而不是永堵。
    """
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=5)
    stmt = (
        select(TaskRun.id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            TaskRun.task_id == task_id,
            or_(
                and_(
                    TaskRun.status == "running",
                    or_(Task.heartbeat_at.is_(None), Task.heartbeat_at >= stale_before),
                ),
                and_(TaskRun.status == "queued", TaskRun.created_at > stale_before),
            ),
        )
        .limit(1)
    )
    return (await session.scalar(stmt)) is not None


async def promote_from_prepared(session: AsyncSession, *, run_id: uuid.UUID,
                                from_statuses: tuple[str, ...]) -> bool:
    """at-least-once 的幂等门（spec §3.2）：条件 UPDATE 推不动 = 重复投递，调用方直接返回。

    用 rowcount 不用「先 SELECT 再 UPDATE」：后者两步之间有别的服务领同一条 job 的话，
    读到的是过眼状态——单条 UPDATE 的原生条件才没有 TOCTOU。
    started_at 只在此处盖（queued/pending 建行时不盖）：它从此诚实表示「真开跑」。

    门内第二笔（T7 Important-2，8b T10 移交界）：门开成功后同事务把
    Task.heartbeat_at 也盖成 now。不盖的残留窗：run 翻 running 后、runner 首拍心跳前
    进程崩溃 —— has_running_run 的 running 分支见 NULL 保守算活（永久 409 拦重跑），
    sweeper 的僵尸扫同样不咬 NULL（永不清扫），两个判活器在同一个窗里各自「都当对方
    会救」，实则都袖手。门内盖戳后，running 行的心跳钟从开跑那一毫秒起就是活的，
    NULL 分支只剩存量行兜底语义。门不开（rowcount≠1）时一个字都不碰 tasks ——
    ⑧⑨ 针钉死「没开跑的行不配活性钟」。

    不 commit —— 仓储层老纪律：事务边界归调用方（worker 领 job 后自行提交/回滚）。
    两语句同事务：崩溃时一起回去，不会出现「running 但门内心跳没盖」的半状态。
    from_statuses 参数化让一条 SQL 服务两族：workflow 面既有谓词是 "pending"、
    agent 面（8b 起）是 "queued"（spec §4.3 状态机 queued/pending 并行现状）。
    """
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(TaskRun)
        .where(TaskRun.id == run_id, TaskRun.status.in_(from_statuses))
        .values(status="running", started_at=now)
    )
    if result.rowcount != 1:
        return False
    await session.execute(
        update(Task)
        .where(Task.id == select(TaskRun.task_id).where(TaskRun.id == run_id).scalar_subquery())
        .values(heartbeat_at=now)
    )
    return True


# ---------------- Sweeper 谓词（Phase 8b T7，spec §4.2） ----------------


async def list_sweepable_prepared_runs(
    session: AsyncSession, *, cutoff: datetime, limit: int = 100
) -> list[tuple]:
    """sweeper 扫①：prepared 态超窗的行（commit 成功但 enqueue 抛的崩溃窗遗留）。

    谓词形状 = has_running_run 里两族 prepared 态的镜像：
      - queued（agent 面）：活性时钟 created_at（R-8b-8 单钟纪律——queued 行没有
        心跳这回事，借 Task.heartbeat_at 会误伤，见 has_running_run docstring）。
      - pending + **Task.task_type='workflow'**（workflow 面首程，8b T5 落库初值）：
        同样 created_at 超窗。pending 不加 task_type 限定会咬到「非 workflow 的
        pending」——那是历史遗留/评测旁路，不该由 sweeper 猜。
    只到 created_at < cutoff（超窗）为止，**不认识 _job_id、不改状态**：
    返回 (run_id, status, task_id, organization_id, user_id) 让 sweeper 决定重入队
    还是耗尽标 failed。org/user 从 JOIN 回的 tasks 取（队列消息与 sweeper 都不
    伪造身份，评测执行面 run_evaluation 要这三枚 UUID）。
    按 created_at 正序 + limit 封顶：老账先清，一轮吃不下的下轮再账（60s 一轮）。
    """
    stmt = (
        select(TaskRun.id, TaskRun.status, Task.id,
               Task.organization_id, Task.user_id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            TaskRun.created_at < cutoff,
            or_(
                TaskRun.status == "queued",
                and_(TaskRun.status == "pending", Task.task_type == "workflow"),
            ),
        )
        .order_by(TaskRun.created_at.asc())
        .limit(limit)
    )
    return [tuple(r) for r in await session.execute(stmt)]


async def list_zombie_running_runs(
    session: AsyncSession, *, stale_before: datetime, limit: int = 100
) -> list[tuple]:
    """sweeper 扫②：running 且 Task.heartbeat_at 过期（早于 stale_before）的僵尸。

    活性时钟 = Task.heartbeat_at（与 has_running_run 的 running 分支一字同尺），
    JOIN 回 tasks 是因为心跳列长在 Task 上（task_runs 没有，同模块头形状）。
    两条排除与 has_running_run 严格对齐，否则判活与清扫互相打架：
      - **heartbeat_at IS NULL 不咬**：NULL 保守算活（存量行没这值 / 首次执行
        还没刷过心跳），has_running_run 的 is_(None) 分支同口径。
      - **Task.status='waiting_approval' 不咬**：Phase 7 断点期间 run 恒 running、
        心跳停在 workflow_service._pause_at_approval 挂起那一刻，越等越「像僵尸」；
        咬它等于替人类审批判超时，与审批不变量打架（留痕：spec §4.2 未点名此型，
        实读 _pause_at_approval 后裁定排除）。
    诚实性（控制器点名核验）：「promote-commit→首拍心跳前崩溃」的窗口在 8b T10
    （T7 Important-2 移交界）后已被门内盖戳封死——promote_from_prepared 在翻
    running 的同事务就把 Task.heartbeat_at 盖成 now，此后该钟与常规"开跑后首拍"
    同尺：worker 若死于首拍前，心跳从 promote 那一刻起停摆，超窗后本谓词照常咬、
    sweeper 标 failed，不再存在"NULL/借陈旧戳 → 判活与清扫各自袖手"的永久 409 窗。
    heartbeat_at IS NULL 只剩存量行（盖戳上线前的历史 running）兜底不咬。
    返回 (run_id, status, task_id, organization_id, user_id) 供 sweeper 标 failed + 留痕。
    """
    stmt = (
        select(TaskRun.id, TaskRun.status, Task.id,
               Task.organization_id, Task.user_id)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            TaskRun.status == "running",
            Task.status != "waiting_approval",
            Task.heartbeat_at.isnot(None),
            Task.heartbeat_at < stale_before,
        )
        .order_by(TaskRun.started_at.asc())
        .limit(limit)
    )
    return [tuple(r) for r in await session.execute(stmt)]


async def next_run_no(session: AsyncSession, *, task_id: uuid.UUID) -> int:
    """该任务下一次执行的序号 = 现有最大 run_no + 1（首次为 1）。

    撞 (task_id, run_no) 唯一约束时靠调用方重试；单用户提交场景几乎不并发。
    """
    stmt = select(func.coalesce(func.max(TaskRun.run_no), 0)).where(
        TaskRun.task_id == task_id
    )
    return int(await session.scalar(stmt) or 0) + 1


# ---------------- Trace（agent_runs）----------------


async def list_agent_runs(
    session: AsyncSession, *, task_run_id: uuid.UUID
) -> list[AgentRun]:
    """一次执行的所有 agent span，按开始时间正序 —— Trace 的节点序列（docs/06 §4）。

    重试产生的多条同节点 span 都在（每次尝试各一行），Trace 页据此显示「失败两次第三次成功」。
    """
    stmt = (
        select(AgentRun)
        .where(AgentRun.task_run_id == task_run_id)
        .order_by(AgentRun.started_at)
    )
    return list(await session.scalars(stmt))
