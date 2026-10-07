"""Agent 任务服务（Phase 5 业务层；8b T4 起「建行收口 API、执行入队」）。

分层职责（docs/02 §4）：
    API 层（api/agents.py）  ── 解析请求 / 取身份 / 选响应模型 / 入队（池在 app.state）
    Service 层（本文件）      ── 业务判断：该不该跑、找不到怎么办、并发冲突怎么办；
                                建行（queued 双行 + run_no + trace_id）在此收口
    Repository / 执行层       ── 读写数据、把已建行跑到终态

执行入口两条（spec §3.2 评测行定稿，双入口）：
    HTTP 三端点      → *_queued：建 queued 行 + commit，端点随后入队，worker 领 job 执行；
    评测 executor 核 → submit_task：同步跑完语义原样保留（名字不动，evaluation_service
                       零改动），内部 = create_queued + 幂等门 promote + run_prepared。
本层不 import arq：入队是 API 装配面的事（worker 在另一进程，service 层保持队列无感）。
"""
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.graph import checkpointer
from app.ai.graph.memory import (
    MEMORY_CONCLUSION_CHARS,
    MEMORY_ROUNDS_MAX,
    build_memory,
    clip_text,
)
from app.ai.graph.workflow import build_task_graph
from app.application import task_runner
from app.core.exceptions import TaskNotFoundError, TaskRunConflictError
from app.core.task_cache import read_state, write_state
from app.data.models import AgentRun, Task, TaskRun, ToolCall
from app.data.repositories import task_repo, tool_call_repo
from app.schemas.agent_task import TraceNodeOut

logger = logging.getLogger(__name__)

# 图编译一次复用：compile 出来的 app 无请求态（运行依赖全走 config 注入），
# 每个请求重建只是白烧 CPU。checkpointer 是其内部的模块级单例，跨请求共享符合预期。
_graph = None


def _invalidate_graph() -> None:
    """checkpointer 关池后复位图缓存：旧 _graph 绑的是已关池的 saver，留着它
    同进程 shutdown→ensure_setup 之后会拿死池（PoolClosed）而不是新单例。"""
    global _graph
    _graph = None


# 模块导入期登记一次（register 幂等）：跨 lifespan 复用同进程时的脱钩保险
checkpointer.register_shutdown_hook(_invalidate_graph)


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_task_graph()
    return _graph


async def _guard_not_evaluation(session: AsyncSession, task: Task) -> None:
    """评测 Task 对全部产品入口隐身：404 与"不存在"同形（W11，闸门在 service 层）。

    一处判定四处复用（get_task_detail / rerun_queued / follow_up_queued / get_task_memory），
    不在 API 层复制四遍。抛的是本文件现有 TaskNotFoundError() —— 与各函数"task 不存在"
    路径逐字同一构造（无参 = 默认文案），对外响应与随机 UUID 撞 404 完全同形，
    不给评测任务留任何存在性信号。判据 is_evaluation_task 与 list_tasks 的
    not_in(evaluation) 过滤同源：列表藏得住，四个入口也必须藏得住。
    """
    if await task_repo.is_evaluation_task(
        session, task_id=task.id, organization_id=task.organization_id
    ):
        raise TaskNotFoundError()


def _guard_not_workflow(task: Task) -> None:
    """workflow 任务对 /agents 面的两个执行入口（rerun / follow_up）关门（终审修复轮 M1）。

    形状逐字仿 _guard_not_evaluation：同款 TaskNotFoundError() 无参构造 —— 404、
    TASK_404001、默认文案「任务不存在」，与随机 UUID 撞 404 完全同形，不加新错误码
    （docs/06 §5 无未登记的号段可用；评审给的两案「同形 404 / 明示 400」取前者）。
    与评测闸的一处刻意差异：本闸**只关执行入口**，不关详情/列表读面 —— workflow
    任务在 /agents 列表继续可见（控制器裁定，可见性归 Phase 8 产品裁定），
    WorkflowPage 的轮询就靠 get_task_detail，读面一并 404 会直接打死前端。

    为什么必须是闸而不是「碰巧没人点」：这两个入口用 _get_graph()（Phase 5 agent
    整图）+ task.question 起全新 agent run，对 workflow 任务放行等于三连环事故 ——
    ① sales_analysis 这类任务可被整图无审批直跑出稿，本 Phase 立身之本的
    human-in-the-loop 审批门在 /agents 面整体旁路；② 违 spec §3「rejected 为
    终态」：被拒任务可被 rerun 洗成 completed 且产物落地；③ decide_approval 经
    get_latest_run 定位断点 run/thread，agent rerun 追新 run 后 latest 不再是
    断点 run，批准续跑指向无审批 checkpoint 的 thread（绑定漂移）。
    workflow 任务的「重跑」语义 = 经 /workflows 面重新触发建新 Task（Task 6 定型），
    不是同 task 叠 agent run。判据 task_type == "workflow" 与 WorkflowPage 拉列表、
    list_tasks 的 task_type 过滤同源。签名不设 async：纯行内字段判定，零查询。
    """
    if task.task_type == "workflow":
        raise TaskNotFoundError()


async def create_queued(session, *, question, organization_id, user_id,
                        task_type: str = "agent_analysis") -> TaskRun:
    """HTTP 面首提交：建 queued 双行并 commit，返回已持久的 TaskRun。

    run_no 在此刻定为 1（建行即定号——API 与 worker 两头都不再猜号，
    UniqueConstraint(task_id, run_no) 的后墙只留给真并发 bug）。
    trace_id = run.id 也在建行时盖：thread_id=trace_id 的约定（task_runner ②）
    要求执行开始前 id 已知——建行方是唯一同时看得见 id 与行的角色。
    commit 先于入队是硬顺序：worker 是另一个进程/会话，看不见未提交的行（Phase 7 :86 同款纪律）。
    """
    task = Task(organization_id=organization_id, user_id=user_id,
                title=question[:200], question=question, task_type=task_type, status="queued")
    session.add(task)
    await session.flush()
    run = TaskRun(task_id=task.id, run_no=1, status="queued")
    session.add(run)
    await session.flush()
    run.trace_id = run.id
    await session.commit()
    # T8 建行点镜像（queued 进缓存）：写侧若不盖这一位，rerun 把上一轮的 completed
    # 留在缓存里骗轮询者最长 1h（overlay 滞后头号事故形）——「状态推进 SETEX」的
    # 「推进」含 queued 这一步，不只 running/终态。write_state 永不抛，加速面纪律。
    await write_state(task.id, status="queued", heartbeat_at=None, summary=None)
    return run


async def rerun_queued(session, *, task_id, organization_id, user_id) -> TaskRun:
    """重跑的建行面（HTTP 端点专用）：守卫后建新 queued run 行，不执行。

    守卫链与旧同步版逐字同形状：get_task 404 → 评测闸 → workflow 闸 → has_running_run 409。
    判活扩集 {queued, running}（T3）让「已入队未领走」窗口里的二次提交同样吃 409。
    run_no 在建行时刻定号（next_run_no）；question 复用 task.question（重跑不换题，§6 可比较性）。
    **rerun 固定无记忆**口径不变：不写 meta["memory"] 键——worker 侧按 meta 标记装配，
    无键 = 不带记忆（docs/11 §6；判据不是 run_no>1，rerun 也是 >1）。
    """
    task = await task_repo.get_task(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    if task is None:
        raise TaskNotFoundError()
    await _guard_not_evaluation(session, task)
    _guard_not_workflow(task)  # M1：workflow 任务不许经 agent 整图旁路审批门
    if await task_repo.has_running_run(session, task_id=task.id):
        raise TaskRunConflictError()
    run_no = await task_repo.next_run_no(session, task_id=task.id)
    task.status = "queued"  # 冗余状态推回排队位，列表页在 worker 认领前也报 queued
    run = TaskRun(task_id=task.id, run_no=run_no, status="queued")
    session.add(run)
    await session.flush()
    run.trace_id = run.id
    await session.commit()
    # T8 建行点镜像：同 create_queued，queued 复位进缓存（旧终态不许盖新轮）
    await write_state(task.id, status="queued", heartbeat_at=None, summary=None)
    return run


async def follow_up_queued(session, *, task_id, question, organization_id, user_id) -> TaskRun:
    """追问的建行面（HTTP 端点专用）：同守卫 + 「本轮带记忆」标记落 meta。

    与 rerun_queued 的差集只有一条追问问题与新 meta。记忆装配本身挪到 worker 侧
    job 内现算（提交侧算会竞态读不到前轮终态快照）——这里只持久化
    question 与 meta={"memory": True} 标记（brief 收口裁定；job 读到 True 才装配，
    rerun 不写该键 = 固定无记忆口径不变）。
    """
    task = await task_repo.get_task(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    if task is None:
        raise TaskNotFoundError()
    await _guard_not_evaluation(session, task)
    _guard_not_workflow(task)  # M1：与 rerun 同闸 —— 追问同样会起一条无审批的 agent run
    if await task_repo.has_running_run(session, task_id=task.id):
        raise TaskRunConflictError()
    run_no = await task_repo.next_run_no(session, task_id=task.id)
    task.status = "queued"
    run = TaskRun(task_id=task.id, run_no=run_no, status="queued",
                  meta={"memory": True, "question": question})
    session.add(run)
    await session.flush()
    run.trace_id = run.id
    await session.commit()
    # T8 建行点镜像：同 create_queued（追问轮同形）
    await write_state(task.id, status="queued", heartbeat_at=None, summary=None)
    return run


async def run_prepared(session, *, task, task_run, memory=None) -> TaskRun:
    """把已建好的行交给本进程惰性编译的图跑到终态（worker / 同步核共用薄封装）。

    「取图」刻意留在这层而不是 jobs.py：图是进程级的（worker 有自己的一份惰性编译图，
    checkpointer 是模块级单例），service 层持有 _graph 缓存的所有权不变；
    jobs.py 只认 ctx 与 run_id，不认图对象。
    question 取行不取参：queued 行的本轮问题在 meta["question"]（follow_up_queued 写），
    缺省回退 task.question（首轮 / rerun 复用原题）——入队参数只带 UUID 的另一半账。
    """
    question = (task_run.meta or {}).get("question") or task.question
    return await task_runner.run_task(
        _get_graph(),
        question=question,
        session=session,
        organization_id=task.organization_id,
        user_id=task.user_id,
        task=task,
        task_run=task_run,
        memory=memory,
    )


async def submit_task(
    session: AsyncSession,
    *,
    question: str,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    task_type: str = "agent_analysis",
) -> TaskRun:
    """同步跑完一次分析任务并返回终态 run —— **评测 executor 的默认值形状**（spec §3.2）。

    8b T4 双入口裁定：HTTP 三端点不再走这里（只走 *_queued + 入队）；本函数保留
    「跑完才返回」语义与名字不动，evaluation_service 侧零改动——它判的就是返回行
    的 Trace，必须拿到 COMPLETED/FAILED 的 run 而不是 queued。

    适配裁定（ledger 记）：取「自建 queued 行后立即 promote」而非「直接建 running 行」——
    建行代码只有一份（create_queued），状态推进只经幂等门（promote_from_prepared），
    同步核与 202 面在行形状上零分叉；多付的代价是一次 UPDATE + 一次 commit，
    换「两头都不猜形状」。
    """
    run = await create_queued(
        session, question=question, organization_id=organization_id,
        user_id=user_id, task_type=task_type,
    )
    task = await session.get(Task, run.task_id)
    # 与 worker 领 job 同一道门：queued→running + started_at。
    # 推不动 = 本会话没领到这一行（行形状异常 / 有人先领了），此时**不能继续跑**：
    # T4 移交时的 warn-and-continue 让函数在「没认领成功」的行上照样产出终态 run 并返回，
    # 而评测侧判的正是「返回行 = 本次调用跑的那次执行」——继续就是把别人的在飞行
    # 当自己的成果交出去（回归分数会莫名其妙地好看）。
    # 选择 raise 而不是 return：契约是「跑完才返回终态行」，抛错是唯一诚实的形状；
    # 行本身留在库里（queued 由 T7 sweeper 自愈重投），不替异常路径伪造终态。
    promoted = await task_repo.promote_from_prepared(
        session, run_id=run.id, from_statuses=("queued",))
    if not promoted:  # pragma: no cover —— 理论不可达，响地失败不静默
        run_id = run.id  # 先取 id：rollback 会过期会话内对象，别拿异常路径去赌一次刷新
        await session.rollback()
        raise RuntimeError(
            f"submit_task 幂等门未命中（run={run_id} 非 queued 或已被领走），拒绝继续执行"
        )
    await session.commit()
    return await run_prepared(session, task=task, task_run=run, memory=None)


async def _build_memory_for(
    session: AsyncSession,
    *,
    task_id,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[dict, int]:
    """按 §6 的口径装配"本轮记忆"：取最近 N 条 completed run 的快照 → build_memory。

    返回 (memory, round_no)。round_no 就是本轮 run 的序号 —— 用它而不是 retry_count
    是硬要求（§4.1：retry_count 归本轮白板，混进记忆就是"上一轮审了几次"）。

    身份照旧往下传：task_id 是外部入参，读快照这条链上多一道 org+user JOIN 过滤不要钱，
    而漏一次校验的代价是§7点名最重的「跨组织读别人业务数字」，不赌调用方永远记得查。
    """
    run_no = await task_repo.next_run_no(session, task_id=task_id)
    prev = await task_repo.list_recent_runs(
        session, task_id=task_id, status="completed", limit=MEMORY_ROUNDS_MAX,
        organization_id=organization_id, user_id=user_id,
    )
    memory = build_memory(
        [{"run_no": r.run_no, "status": r.status, "state": r.state} for r in prev],
        round_no=run_no,
    )
    return memory, run_no


async def get_task_memory(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[Task, list[dict], dict | None]:
    """记忆可见性（§1 纪律③）：这条 task 跑过哪些轮、下一轮会实际继承到什么。

    纯读，不跑图、不调模型。只经带归属校验的 get_task + task_repo 拿数据：
    记忆内容全是业务数字，不提供"按 state 内容查"的口子（§7）。

    每轮视图的投影（isinstance 闸 / conclusion 截断 / 窗口命中判定）是业务判断，
    在本层做完 —— 路由层只拿身份 → 调本函数 → 包响应模型（文件头分层契约）。
    返回的 list[dict] 键与 MemoryRoundOut 字段一一对应，pydantic 校验后响应形状不变。
    截断长度用 MEMORY_CONCLUSION_CHARS 而不是字面量：与记忆装配里的 conclusion 同源，
    "截断口径只有一份"（clip_text docstring 的承诺）才是真的。
    """
    task = await task_repo.get_task(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    if task is None:
        raise TaskNotFoundError()
    await _guard_not_evaluation(session, task)
    runs = await task_repo.list_recent_runs(
        session, task_id=task.id,
        organization_id=organization_id, user_id=user_id,
    )
    memory, _round_no = await _build_memory_for(
        session, task_id=task.id, organization_id=organization_id, user_id=user_id
    )
    mc = memory["context"]
    window = {h["round"] for h in (mc or {}).get("history", [])}
    rounds = []
    for r in runs:
        # 快照是 JSONB，形状由模型/工具产出，历史轮可能是 None、也可能是当年写坏的半截结构。
        # 这里 isinstance 两道闸：记忆视图是「看历史」的接口，绝不该因为某轮快照畸形而 500。
        state = r.state if isinstance(r.state, dict) else {}
        rounds.append({
            "run_no": r.run_no,
            "status": r.status,
            "has_snapshot": r.state is not None,
            "question": state.get("question"),
            "conclusion": clip_text(state.get("analysis"), MEMORY_CONCLUSION_CHARS) or None,
            "facts": len(state.get("data_results") or []),
            "reused_by_next": r.run_no in window,
        })
    return task, rounds, mc


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
    return await task_repo.list_tasks(
        session,
        organization_id=organization_id,
        user_id=user_id,
        status=status,
        task_type=task_type,
        limit=limit,
        offset=offset,
    )


async def get_task_detail(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[Task, TaskRun | None, int]:
    """任务详情所需的三样：task 本身 + 最近一次执行 + 一共跑过几次。找不到抛 404。"""
    task = await task_repo.get_task(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    if task is None:
        raise TaskNotFoundError()
    await _guard_not_evaluation(session, task)
    latest = await task_repo.get_latest_run(session, task_id=task.id)
    count = await task_repo.count_runs(session, task_id=task.id)
    return task, latest, count


async def cache_first_status(task: Task) -> str:
    """T8 读侧 helper（get_task_detail 的 cache-first 状态位）：命中用缓存，回落用 PG 行。

    只回答 Task.status 这一位——run 明细（latest_run/progress/meta）**恒取 PG**：
    v1 缓存只加速高频轮询真需要的最小集（task 状态位），不过拟合。
    类型闸（read_state 返回的是 JSON dict，值形态不受代码控制）：status 必须是非空 str
    才许覆盖，坏值静默当 miss 走 PG —— 「在档但值坏」与「删光键」必须同形。
    read_state 自带永不抛纪律，这里不再包 try。
    """
    state = await read_state(task.id)
    status = (state or {}).get("status")
    return status if isinstance(status, str) and status else task.status


async def get_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> TaskRun:
    run = await task_repo.get_task_run(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    if run is None:
        raise TaskNotFoundError("执行记录不存在")
    return run


def merge_trace_nodes(
    agents: Sequence[AgentRun], tools: Sequence[ToolCall]
) -> list[TraceNodeOut]:
    """把 agent 行与 tool 行合成统一节点序列，按 started_at 升序。

    纯函数（不碰 session）——scratch 里可直接断言孤儿/混排/字段映射，不必造库行。
    归属不在这里判断：parent_span_id 是执行层落库时就写好的（models.py:283-284 的注释
    就指望前端拼树），这里只做形状归一。
    兄弟序/孤儿提升是前端 buildTraceTree 的事（spec §4.2），后端不猜渲染策略。
    """
    nodes = [
        TraceNodeOut(
            kind="agent",
            span_id=a.span_id,
            parent_span_id=a.parent_span_id,
            name=a.agent_name,
            status=a.status,
            duration_ms=a.duration_ms,
            total_tokens=a.total_tokens,
            cost=float(a.cost or 0),
            model=a.model,
            summary=a.output_summary,
            error_message=a.error_message,
            started_at=a.started_at,
            finished_at=a.finished_at,
        )
        for a in agents
    ]
    nodes.extend(
        TraceNodeOut(
            kind="tool",
            span_id=t.span_id,
            parent_span_id=t.parent_span_id,
            name=t.tool_name,
            status=t.status,
            duration_ms=t.duration_ms,
            # tool_calls 没有 token 列：这里是 0 而不是 None，因为响应模型的 total_tokens
            # 是非空 int，且"这个节点没花 token"与"不知道花多少"在产品语义上等价于 0
            total_tokens=0,
            cost=float(t.cost or 0),
            model=None,
            summary=t.output_summary,
            error_message=t.error_message,
            started_at=t.started_at,
            finished_at=t.finished_at,
        )
        for t in tools
    )
    nodes.sort(key=lambda n: n.started_at)
    return nodes


async def get_trace(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[TaskRun, list[TraceNodeOut]]:
    """一次执行的 Trace：run 概要 + 其下全部节点（agent + tool，按开始时间正序）。

    归属校验保持在 get_task_run（JOIN tasks 逐 org/user），跨组织/跨用户到这里就是
    TaskNotFoundError → 与"不存在"同形的 404（spec §3.2 scope 针 + docs/06 §5 探测防护口径）。
    """
    run = await task_repo.get_task_run(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    if run is None:
        raise TaskNotFoundError("执行记录不存在")
    agents = await task_repo.list_agent_runs(session, task_run_id=run.id)
    tools = await tool_call_repo.list_task_run_tool_calls(session, task_run_id=run.id)
    return run, merge_trace_nodes(agents, tools)
