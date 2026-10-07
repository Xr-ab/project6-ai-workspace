"""Workflow 审批执行引擎（Phase 7 Task 6）：trigger / execute / 停在断点 / 决策 / resume。

机制裁定（计划 Task 6 钦定 + spec §6 勘误，绑定实现，勿再翻案）：
    **静态断点组合** —— 编译期 interrupt_before=["approval"]（Task 5 图自带）+
    aupdate_state(config, {"approval_decision": decision})（**不填 as_node**）+
    astream(None, config) 续跑。
    为什么不用动态 interrupt()/Command(resume)：spec §6 同文两种写法互斥属内部矛盾，
    裁定取静态——build_task_graph 现有管道同形（workflow.py:117-120）、决策走 state
    通道让 route_after_approval 纯函数可单测。as_node 一旦填上就代表「该节点已跑过」，
    会跳过断点语义 —— 我们要的是「改状态、仍停在它前面」。

与 task_runner 的分工（Trace 不发明第二套格式）：
    spans → agent_runs、tool_call_sink → tool_calls、终态失败落库，全部复用 task_runner
    的 _persist_agent_runs / _persist_tool_calls / _fail（同一张 Trace 表，Workflow 与
    Agent 任务同构可读）。TaskRun.run_type 保持默认 'product'：workflow 任务在产品侧，
    W11/W12 评测闸门只认 'evaluation'，绝不能被误挡/误放。

会话与图实例生命周期（Task 4/5 遗留口径在此收账）：
    后台执行不能复用请求会话（响应后即关），execute/resume 各自开 AsyncSessionLocal
    （模式照抄 evaluation.execute_evaluation）；build_workflow_graph 每次现编译、不缓存
    （Task 5 裁定），execute 与 resume 各拿新实例，绑同一 checkpointer 单例 +
    thread_id = str(trace_id)（§2 红线：thread 挂 run 不挂 task，审批续跑同一条 run）。

执行面归属（8b T5 换血）：
    本模块**不再自己起后台任务**。trigger / decide_approval 只落库并返回行 id，
    真正的 execute/resume 由 arq worker 领 job 后调用（app/workers/jobs.py），
    入队发生在路由层 commit 之后（api/workflows.py、api/agents.py）。
    为什么：进程内起协程 = 「spawn 后进程死」那类丢执行的形态学根源；
    队列把「谁跑」与「谁批准起跑」解耦，重启不丢单，幂等交给 promote 门。

状态机（Task 3 已扩 statuses）：
    pending → running → (停在断点) waiting_approval → (决策放行) running → completed
    / rejected（人为拒绝，非失败）；任一执行段抛 TaskNodeError → failed（带分类）。
    pending 的含义在 8b 后变了：「已落库、等 worker 领走」，不再是「本进程马上跑」。
"""
import logging
import uuid
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.graph.errors import TaskNodeError
from app.ai.graph.workflows import WORKFLOW_GRAPHS, build_workflow_graph
from app.ai.tools.base import ToolContext
from app.application import task_runner
from app.core.audit import write_audit
from app.core.exceptions import ApprovalConflictError, TaskNotFoundError
from app.data.db import AsyncSessionLocal
from app.data.models import Task, TaskRun, User, Workflow, WorkflowApproval
from app.data.repositories import task_repo, workflow_repo

logger = logging.getLogger(__name__)

# Task.status 的等待审批态（Task 3 迁移扩的状态机值）
WAITING_APPROVAL = "waiting_approval"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- 触发 + 首程执行 ----------------


async def trigger(
    session: AsyncSession,
    *,
    workflow: Workflow,
    inputs: dict,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """触发一次 workflow 执行：只落 Task/TaskRun 行，返回 (task_id, task_run_id)。

    两个 id 必须在返回前已存在（路由层拿 task_run_id 入队 + 回响应体），
    所以 TaskRun 行（pending）在这里建好；起跑由 worker 领 job 后调
    execute_workflow，经 promote_from_prepared 门把 pending 推成 running。
    inputs 落进 task_run.meta["workflow_input"]：后台会话读不到请求内存，执行入参
    必须持久化 —— 这顺带白赚一个性质（进程重启后入参仍在库里，可观测可追责）。
    commit 必须先于返回：worker 开的是另一个会话，看不见未提交的行
    （路由层严格 commit → enqueue，本函数的 commit 就是那条界线）。
    编目归属校验归调用方（Task 7 按 org 查 workflow 行后才进这里）。
    """
    question = str(inputs.get("question") or "").strip() or \
        f"Workflow 触发：{workflow.name or workflow.graph_key}"
    task = Task(
        organization_id=organization_id,
        user_id=user_id,
        title=question[:200],
        question=question,
        task_type="workflow",
        workflow_id=workflow.id,
        status="pending",
    )
    session.add(task)
    await session.flush()
    task_run = TaskRun(
        task_id=task.id, run_no=1, status="pending", meta={"workflow_input": inputs}
    )
    session.add(task_run)
    await session.flush()
    task_run.trace_id = task_run.id  # thread_id = trace_id 约定与 task_runner ② 同源
    await session.commit()
    # T8 建行点镜像：pending 进缓存（写侧「状态推进 SETEX」含首拍；不盖则
    # 同名 task 前轮的旧状态留在缓存里骗轮询者，与 agent 面 queued 针同形）。
    await task_runner.cache_state(task)
    return task.id, task_run.id


async def execute_workflow(
    task_id: uuid.UUID,
    *,
    task_run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """worker 侧执行入口（jobs.workflow_execute 的下游）：自开会话，从头流式跑到断点/终态。

    前置条件：调用方（worker）已用 task_repo.promote_from_prepared 把 run 从
    pending/queued 推成 running 并盖了 started_at，且已 commit —— 那是幂等门，
    重复投递在门那里就撞 rowcount=0 被拦掉，进不到这里。
    本函数因此**不再自己推 run 状态**（8b T5 删掉的自我提拔）：
    run 不是 running 就是「门没领走 / 已终态」，一律拒绝开跑。
    task.status 仍由这里推进（冗余列，Task 侧口径归执行段管）。

    异常绝不外逃：外层是 worker 的 job 边界，逃出去只会被记一次 job 失败而库里
    留着 running 悬案 —— running 挂库里没人收尾是 docs/11 §7 点名的僵尸形态（心跳判活是
    事后放行重跑，让行不进僵尸态才是事前纪律），所以这里自己兜底落 failed + 日志。
    """
    try:
        async with AsyncSessionLocal() as session:
            task = await task_repo.get_task(
                session, task_id=task_id, organization_id=organization_id, user_id=user_id
            )
            run = await task_repo.get_task_run(
                session, run_id=task_run_id, organization_id=organization_id, user_id=user_id
            )
            if task is None or run is None:
                logger.error("Workflow 执行找不到行 task=%s run=%s（触发后即删？）", task_id, task_run_id)
                return
            if run.status != "running":
                logger.info("Workflow 执行 %s 状态已是 %s，拒绝开跑（幂等门未领走或重复投递）", task_run_id, run.status)
                return
            graph = await _graph_for(session, task, run)
            if graph is None:
                return  # 编目失配，_graph_for 已落 failed
            task.status = "running"
            await task_runner.touch_heartbeat(session, task.id)
            await session.commit()
            # T8 首拍镜像（execute 段）：running + 心跳落库同拍进缓存（永不抛）
            await task_runner.cache_state(task)
            config, spans, ctx = await _build_config(session, task, run)
            initial = {"workflow_input": (run.meta or {}).get("workflow_input") or {}}
            await _drive(session, task, run, graph, config, initial, spans, ctx, resume=False)
    except Exception:  # noqa: BLE001 —— 后台执行抛出去只会留僵尸行，必须自己吼
        logger.exception("Workflow 后台执行 %s 出现未捕获异常（task=%s）", task_run_id, task_id)


# ---------------- 决策 + 续跑 ----------------


async def decide_approval(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    approval_id: uuid.UUID,
    decision: str,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    comment: str | None = None,
) -> tuple[WorkflowApproval, uuid.UUID | None]:
    """人工决策：条件更新原子生效 → commit → 返回 (审批行, 待续跑 run id)。

    第二个返回值是给路由层入队用的（8b T5：本函数不再自己起续跑）。
    None = 审批已生效但 task 没有 run 行 —— 理论不该发生，路由层据此不入队，
    这里留一条 error 日志（决策是真发生了的，缺执行行是数据缺陷，得吼）。

    检查顺序即安全顺序：
      1) 审批不在本 org+user 视野 / 不挂在给定 task 上 → TaskNotFoundError（404 同形，
         「不存在」「别人的」「挂错 task」三种输入不可区分，不给存在性探测信号）；
      2) task 不在 waiting_approval / approval 非 pending → ApprovalConflictError（409）；
      3) 原子 UPDATE...WHERE status='pending' RETURNING 命中 0 行 → 409
         （检查与写之间被并发决策抢跑 —— 读后写裸改禁止，race 归 Postgres 行锁裁）。
    决策归一：!= "approved" 一律按 rejected 落（与 route_after_approval 的宁拒不放同形，
    引擎侧不得比路由宽松；垃圾输入不会变成第二次放行机会）。
    """
    approval = await workflow_repo.get_approval(
        session, approval_id=approval_id, organization_id=organization_id, user_id=user_id
    )
    if approval is None or approval.task_id != task_id:
        raise TaskNotFoundError("审批记录不存在")
    task = await task_repo.get_task(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    if task is None or task.status != WAITING_APPROVAL:
        raise ApprovalConflictError()
    if approval.status != "pending":
        raise ApprovalConflictError()
    verdict = "approved" if decision == "approved" else "rejected"
    decided = await workflow_repo.decide_pending_approval(
        session,
        approval_id=approval_id,
        organization_id=organization_id,
        user_id=user_id,
        decision=verdict,
        decided_by=user_id,
        comment=comment,
    )
    if decided is None:
        raise ApprovalConflictError()
    run = await task_repo.get_latest_run(session, task_id=task_id)
    await session.commit()  # 决策 + （竞态时）行锁在同一条语句线里，先落地再放给队列
    # spec §2.6 点名的「审批 decide」埋点（Task 8 出口⑥补齐：T3–T6 分解时的漏项）：
    # 放在业务 commit 之后——决策真生效了才留痕，回滚路径不伪造"发生过"；
    # write_audit 独立会话、永不抛，不会拖住后续入队。
    await write_audit(
        organization_id=organization_id, user_id=user_id, action="approval_decide",
        target_type="approval", target_id=str(approval_id),
        detail={"decision": verdict, "task_id": str(task_id)},
    )
    if run is None:
        logger.error("审批 %s 已决策但 task %s 无执行行，无法续跑", approval_id, task_id)
        return decided, None
    return decided, run.id


async def resume_workflow(
    task_id: uuid.UUID,
    *,
    task_run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    decision: str,
) -> None:
    """worker 侧续跑入口（jobs.workflow_resume 的下游）：Task 推回 running（心跳续刷）→ aupdate_state → astream(None)。

    aupdate_state(config, {"approval_decision": decision}) **不填 as_node**（机制裁定，
    见模块头）；随后同一 thread_id 的 astream(None, config) 从 checkpoint 里 pending 的
    approval 节点起继续。run.status 必须是 running（暂停期间同一条 run 未收尾），
    不是就拒绝 —— 该闸门只挡「终态后再续跑」；真正挡并发双 resume 的是审批行的
    条件 UPDATE 409 锁（decide_approval 第 3 步，每个 approval 至多入队一次续跑）。
    """
    try:
        async with AsyncSessionLocal() as session:
            task = await task_repo.get_task(
                session, task_id=task_id, organization_id=organization_id, user_id=user_id
            )
            run = await task_repo.get_task_run(
                session, run_id=task_run_id, organization_id=organization_id, user_id=user_id
            )
            if task is None or run is None:
                logger.error("Workflow 续跑找不到行 task=%s run=%s", task_id, task_run_id)
                return
            # 职责澄清（审查 m2）：暂停中与"续跑在途"两态下 run 都是 running，本闸门
            # 区分不了二者，只挡终态（completed/failed）后的再续跑；双 resume 的真闸是
            # 审批行条件 UPDATE（同一 approval 只可能赢一次 decide → 只 spawn 一次）。
            if run.status != "running":
                logger.info(
                    "Workflow 续跑 %s 被拒：run 状态 %s（断点等待态的 run 恒为 running，非 running 即已终态或未开跑）",
                    task_run_id, run.status,
                )
                return
            graph = await _graph_for(session, task, run)
            if graph is None:
                return
            task.status = "running"
            await task_runner.touch_heartbeat(session, task.id)
            await session.commit()
            # T8 首拍镜像（resume 段）：断点放行后推回 running 同拍进缓存
            await task_runner.cache_state(task)
            config, spans, ctx = await _build_config(session, task, run)
            await graph.aupdate_state(config, {"approval_decision": decision})  # 不填 as_node
            await _drive(session, task, run, graph, config, None, spans, ctx, resume=True)
    except Exception:  # noqa: BLE001
        logger.exception("Workflow 续跑 %s 出现未捕获异常（task=%s）", task_run_id, task_id)


# ---------------- 内部：共用管线件 ----------------


async def _graph_for(
    session: AsyncSession, task: Task, run: TaskRun
) -> CompiledStateGraph | None:
    """task.workflow_id → 编目行 → 现编译图。编目缺失 = 缺陷：failed 留痕 + None，不兜底。

    两条编目失败路径都走同一 _fail 通道（修复轮 1 M1）：
      (a) 编目行缺失（workflow_id 为 NULL 或指不到行）；
      (b) wf.graph_key 不在 WORKFLOW_GRAPHS 注册表 → build 抛 KeyError。
    此前 (b) 裸抛 KeyError 会被 execute/resume 的 outer-catch 只记日志，Task/TaskRun
    永悬 pending —— 与上面那句自述不变量相悖（编目表现存 3 行、注册表只有 1 键，
    Task 7 暴露触发面后即刻可达）。_fail 对 pending 行（started_at 未设）的兼容性
    由 _run_meta 的 latency 降级保证（task_runner 修复轮 M1②）。
    """
    wf = await session.get(Workflow, task.workflow_id) if task.workflow_id is not None else None
    if wf is None:
        await task_runner._fail(
            session, task, run, [], {}, "internal_error",
            f"Workflow 编目行缺失（workflow_id={task.workflow_id}）", None, None,
        )
        return None
    try:
        return build_workflow_graph(wf.graph_key)
    except KeyError:
        await task_runner._fail(
            session, task, run, [], {}, "internal_error",
            f"graph_key '{wf.graph_key}' 未注册（注册表只有 {sorted(WORKFLOW_GRAPHS)}）", None, None,
        )
        return None


async def _build_config(
    session: AsyncSession, task: Task, run: TaskRun
) -> tuple[RunnableConfig, list[dict], ToolContext]:
    """执行段的运行依赖注入：与 task_runner 同一形状（thread/tool_context/spans）。

    spans 列表按执行段各一份：断点前一段、续跑一段各落各的 agent_runs 行，
    trace_id 相同所以 Trace 页仍是同一条链。
    role 回库取用户行（8a 权限闸，与 task_runner 同款）：行必在（FK + 无删除路径），
    查不到就让 AttributeError 炸出来，不兜。
    """
    ctx = ToolContext(
        session=session,
        organization_id=task.organization_id,
        user_id=task.user_id,
        role=(await session.get(User, task.user_id)).role,
        tool_call_sink=[],
    )
    spans: list[dict] = []
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(run.trace_id),  # thread_id = trace_id（§2 红线，run 级）
            "tool_context": ctx,
            "spans": spans,
        }
    }
    return config, spans, ctx


async def _drive(
    session: AsyncSession,
    task: Task,
    run: TaskRun,
    graph: CompiledStateGraph,
    config: RunnableConfig,
    initial: dict | None,
    spans: list[dict],
    ctx: ToolContext,
    *,
    resume: bool,
) -> None:
    """execute/resume 共用流水：astream 逐节点推进 → 停审批落 pending / 到终态收尾。

    progress 口径：首程 = 已完成节点/业务节点总数（task_runner 同款，__ 内部通道不算）；
    续跑段只含断点后的节点，done 不从 0 重数 —— 每节点按步长递增（封顶 99），
    终态统一 100。心跳与 progress 同拍刷（推进即证活）。
    流完后 graph.aget_state(config) 看 snap.next：非空 = 停在静态断点上。
    """
    total = len([n for n in (getattr(graph, "nodes", {}) or {}) if not n.startswith("__")]) or 1
    step = max(1, 100 // total)
    done = 0
    partials: dict[str, dict] = {}
    snapshot: dict | None = None
    try:
        async for mode, chunk in graph.astream(
            initial, config, stream_mode=["updates", "values"]
        ):
            if mode == "values":
                snapshot = dict(chunk)  # 每超步覆盖，循环结束即本段终态
                continue
            for node_name, partial in chunk.items():
                if node_name.startswith("__"):
                    continue
                done += 1
                partials[node_name] = partial or {}
                if resume:
                    run.progress = min(99, run.progress + step)
                else:
                    run.progress = min(100, int(done / total * 100))
                await task_runner.touch_heartbeat(session, task.id)
                await session.commit()
                # T8 每节点 commit 同拍镜像（workflow 面 beat，touch_heartbeat 已在处）
                await task_runner.cache_state(task, summary={"progress": run.progress})

        snap = await graph.aget_state(config)
        if snap.next:
            await _pause_at_approval(session, task, run, snap, spans, partials, ctx)
        else:
            await _settle_terminal(session, task, run, dict(snap.values or {}), spans, partials, ctx)
    except TaskNodeError as exc:
        # 节点错误不向上抛（task_runner 同纪律）：failed + 分类照落，span 照存
        await task_runner._fail(
            session, task, run, spans, partials, exc.category, str(exc),
            ctx.tool_call_sink, snapshot,
        )
    except Exception as exc:  # noqa: BLE001 —— 图外层异常也不留 running 悬案
        await task_runner._fail(
            session, task, run, spans, partials, "internal_error",
            f"{type(exc).__name__}: {exc}", ctx.tool_call_sink, snapshot,
        )


async def _pause_at_approval(
    session: AsyncSession,
    task: Task,
    run: TaskRun,
    snap,
    spans: list[dict],
    partials: dict[str, dict],
    ctx: ToolContext,
) -> None:
    """真到了断点：TaskRun 保持 running（同一条 run 续跑），Task 挂 waiting_approval + 落 pending 审批。

    断点前跑完的 span 先落库（审批等待期间 Trace 页要能看到「已经跑了哪几步」），
    续跑段的 span 到终态再落 —— 同 trace_id，agent_runs 行自然累加。
    """
    await task_runner._persist_agent_runs(session, spans, task, run, partials)
    await task_runner._persist_tool_calls(session, ctx, task, run)
    task.status = WAITING_APPROVAL
    session.add(WorkflowApproval(
        task_id=task.id,
        graph_node=snap.next[0],  # 当前编目图的断点即 APPROVAL_NODE（base.py 常量锚定）
        status="pending",
    ))
    await task_runner.touch_heartbeat(session, task.id)
    await session.commit()
    # T8 审批暂停点镜像（brief Step 2 点名的 waiting_approval 态）：断点期间轮询者
    # 靠这一位看到「在等人」而不是「还在跑」；不接线则缓存停在 running 最长 1h。
    await task_runner.cache_state(task)


async def _settle_terminal(
    session: AsyncSession,
    task: Task,
    run: TaskRun,
    values: dict,
    spans: list[dict],
    partials: dict[str, dict],
    ctx: ToolContext,
) -> None:
    """跑到 END：deliver 出 result → completed；reject 支路 result 为空 → rejected。

    人为拒绝不是链路失败（spec §6：不进失败分类表）—— TaskRun 依然 completed
    （图正常跑完了），只有 Task 的冗余状态区分 completed / rejected。
    meta 合并写：保留 trigger 落的 workflow_input，追加终态产物 result。
    """
    await task_runner._persist_agent_runs(session, spans, task, run, partials)
    await task_runner._persist_tool_calls(session, ctx, task, run)
    result = values.get("result")
    run.status = "completed"
    run.progress = 100
    run.finished_at = _now()
    try:
        run.state = task_runner._as_json_safe(values)
    except Exception:  # noqa: BLE001 —— 快照是收尾里最不要紧的一样（_fail 同款教训）
        run.state = None
        logger.exception("Workflow 任务 %s 终态快照序列化失败，state 记 NULL", task.id)
    run.meta = {
        **(run.meta or {}),
        "result": task_runner._as_json_safe(result) if result is not None else None,
        "latency_ms": (
            int((run.finished_at - run.started_at).total_seconds() * 1000)
            if run.started_at else None
        ),
    }
    task.status = "completed" if result else "rejected"
    await session.commit()
    # T8 终态写侧接线（workflow 面的 completed/rejected 两处合一）：task.status 刚按
    # 支路定值，cache_state 读的就是它——rejected 也在终态三处之内（brief Step 2）。
    await task_runner.cache_state(task, summary={"progress": 100})
