"""任务执行层（Phase 4 最后一块；8b T4 起「收已建行」）：task / task_runs / agent_runs 落库。

与 Checkpointer 的分工（见 TaskRun 模型 docstring，别混淆）：
    Checkpointer 存**图内部的状态快照**（机制层，可丢可换，用于中断恢复）；
    本模块存**这次执行对外的产品数据**（状态 / 进度 / 耗时 / 失败分类 / agent span），
    供列表页、Trace 页、Phase 6 Evaluation 长期读取。

**Phase 8b T4 的收口**（spec §3.2「run_task 的建行动搬去 API」）：
    建行（Task + TaskRun + run_no 分配 + trace_id）已整体外移到
    agent_task_service 的 *_queued 入口——HTTP 面先落 queued 双行、commit、入队，
    worker 领 job 经幂等门（task_repo.promote_from_prepared）把行推进到 running，
    然后才调本模块。run_task 从此**只把一条已存在的 run 跑到终态，失败不抛**。
    run_no 归建行方（API 侧 create/rerun/follow_up 各自定号），本函数不再问仓储取号。

一次 run_task 的完整数据流（建行段退场后）：
    ① 入参 task/task_run 已是幂等门推好的 running 行；本函数把 task 冗余状态推回
       running、立刻随首拍 progress commit —— 先落 running 再跑图的崩溃可观测性不变
    ② thread_id = str(trace_id)：Checkpointer 快照与 Trace 数据用同一个 id 对上
       （trace_id 由建行方盖 = run.id，本函数只读）
    ③ astream(stream_mode="updates") 边跑边观察：每个节点完成 → 推进 progress → commit；
       node_guard 顺手往 config["configurable"]["spans"] 里记 agent span（含耗时/成败）
    ④ 全部完成 → spans 落 agent_runs + task_run/task 置 completed
    ⑤ TaskNodeError 穿透 → 不向上抛（**错误不崩任务**）：task_run 置 failed +
       failure_category（七分类）+ task 置 failed，span 照样落库后正常返回

为什么 run_no 的分配权在调用方（8b 后的形状）：
    「这次给定的执行完整记下来」仍是本模块的职责不变项。变的是 202 语义要求
    queued 行**入队前就存在且定好号**（API commit → enqueue → worker 认领），
    worker 侧再 next_run_no 就是两头猜号——UniqueConstraint(task_id, run_no)
    只留给真并发 bug。见 agent_task_service.create_queued / rerun_queued / follow_up_queued。
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import pricing
from app.ai.graph import memory as memory_mod
from app.ai.graph.errors import TaskNodeError
from app.ai.tools.base import ToolContext
from app.application.chat_service import to_tool_call_row
from app.application import report_service
from app.core.config import settings
from app.core.task_cache import write_state
from app.data.models import AgentRun, Task, TaskRun, User
from app.data.repositories import tool_call_repo

logger = logging.getLogger(__name__)


async def cache_state(task: Task, *, summary: dict | None = None) -> None:
    """T8 写侧接线：状态/心跳落 PG 的**同拍**镜像进 task_state 缓存（加速面，永不抛）。

    纪律（写点在 commit 之后）：缓存只镜像「PG 已提交的状态」，不领先真值源；
    write_state 内部吞一切异常只 warning，所以本函数也不加 try——业务面永不被它拖死。
    heartbeat_at 用当下重算而非回读 PG：缓存心跳是活性镜像不是权威存根
    （详情响应模型根本没有 heartbeat 字段，读侧只消费 status），
    回读 = 多一次查询，买一个没人读的一致性，不值。
    """
    await write_state(task.id, status=task.status,
                      heartbeat_at=datetime.now(timezone.utc), summary=summary)


async def touch_heartbeat(session: AsyncSession, task_id: uuid.UUID) -> None:
    """把 Task.heartbeat_at 刷成当下（Phase 7 心跳判活，docs/11:253 收账）。

    语义：每次推进 progress 顺带一跳心跳 —— 供 has_running_run 区分「真在跑」与
    「进程崩剩的僵尸 running 行」（过期即放行重跑）。
    刻意**不 commit**：与调用方的 progress 推进同处一个事务（「边跑边 commit」的
    那一次 commit 一起落），刷新与进度原子同现。用 Core update 直写，不回读 ORM 对象
    （调用方后续也不读这个字段，避免 expire 抖动）。返回 None：不改 run_task 返回结构。
    """
    await session.execute(
        update(Task).where(Task.id == task_id).values(heartbeat_at=datetime.now(timezone.utc))
    )


async def run_task(
    graph,
    *,
    question: str,
    session: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    task: Task,
    task_run: TaskRun,
    memory: dict | None = None,
) -> TaskRun:
    """把一条**已建好的 run** 跑到终态并全程落库。**不抛节点异常**：失败也返回
    TaskRun（status=failed），调用方看 status 决定下一步，而不是被异常炸掉。

    8b T4 收口：task / task_run 必传已建行（幂等门推好的 queued→running 或
    评测/脚本直建的 running 行），本函数只负责推进执行——建行、run_no 分配、
    trace_id 归建行方（agent_task_service 的 *_queued / 测试脚本）。
    run 行的 running/started_at 归幂等门 promote（task_repo.promote_from_prepared），
    本函数不重复盖：已推过的行到这里状态就是 running，天然一致；
    task 行的冗余状态由这里推回 running（与心跳同字段族，列表页直读）。

    memory 是 `app.ai.graph.memory.build_memory()` 的**原样返回值**
    （`{"context": …, "stats": …}`），由调用方（worker job / service）算好传进来：
    「取几轮快照」是仓储的事、「怎么压缩」是记忆层的事、本模块只是把 context 塞进初始
    state、把 stats 落进 meta.memory —— 三件事不混在一层。
    None = 无记忆开局（首轮 submit / rerun，§6 口径 rerun 固定不带记忆）。
    """
    # task.status 由建行时的 "queued" 推回 "running"：不单独 commit，
    # 与首拍 progress 推进同事务落（① 先落 running 的崩溃可观测性由 promote 的
    # commit + 这里的 progress commit 接力：run 行早已是 running，task 冗余列最多晚一拍）。
    task.status = "running"

    # 只多带一个键：无记忆时连这个键都不出现 —— 保证第一轮的初始 state 与现状同构
    initial: dict = {"question": question}
    if memory and memory.get("context"):
        initial["memory_context"] = memory["context"]

    ctx = ToolContext(
        session=session,
        organization_id=organization_id,
        user_id=user_id,
        # 8a 权限闸的 role：后台执行器里没有请求态身份，回库取用户行。
        # 行必在（task.user_id 是 FK 且无删除路径）；查不到就让 AttributeError 炸出来，不兜。
        role=(await session.get(User, user_id)).role,
        tool_call_sink=[],  # 节点往里塞 ToolCallRecord，末尾由 _persist_tool_calls 落库
    )
    spans: list[dict] = []
    # 本轮的记账：问题原文（tasks.question 保持首轮不动，§6）+ 记忆继承明细
    meta_ctx: dict = {"question": question, "memory": (memory or {}).get("stats") or memory_mod.empty_stats()}
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(task_run.trace_id),  # ② Checkpointer 与 Trace 同 id
            "tool_context": ctx,
            "spans": spans,
        }
    }

    # 只数真正的业务节点：graph.nodes 含 __start__ / __end__ 等内部通道，算进去会让
    # 分母比可计数节点多 1 → 无回炉时 progress 卡在 85% 才跳 100（Phase 5 审查发现）。
    total = len([n for n in (getattr(graph, "nodes", {}) or {}) if not n.startswith("__")]) or 1
    done = 0
    partials: dict[str, dict] = {}  # 每个节点最终一次尝试的输出（重试覆盖旧值）
    snapshot: dict | None = None        # 最后一次看到的完整 state = 终态快照

    try:
        # stream_mode 同时要 updates（逐节点进度 / partials）和 values（每个超步的完整 state）。
        # 终态快照取自 values 流的最后一颗，而不是事后 graph.aget_state(config)：
        # 后者依赖 checkpointer 里还留着这条 thread，而失败路径（_fail）恰恰是最需要快照的时候，
        # 不该让"记忆"依赖机制层组件的存活（§2 红线把 thread 的用途压回单次 run 的中断恢复）。
        async for mode, chunk in graph.astream(initial, config,
                                               stream_mode=["updates", "values"]):
            if mode == "values":
                snapshot = dict(chunk)      # 每超步覆盖一次，循环结束时即终态
                continue
            for node_name, partial in chunk.items():
                if node_name.startswith("__"):  # __start__ / __end__ 等内部通道不算节点
                    continue
                done += 1
                partials[node_name] = partial or {}
                # ③ 每个节点完成即提交：崩了也知道跑到第几步，progress 供 SSE 推
                task_run.progress = min(100, int(done / total * 100))
                # 心跳与 progress 同拍（Phase 7）：推进即证活，同一次 commit 落库
                await touch_heartbeat(session, task.id)
                await session.commit()
                # T8：commit 同拍镜像（写侧接线点之「每节点 beat」；永不抛，见 cache_state）
                await cache_state(task, summary={"progress": task_run.progress})

        await _persist_agent_runs(session, spans, task, task_run, partials)
        await _persist_tool_calls(session, ctx, task, task_run)
        task_run.status = "completed"
        task_run.progress = 100
        task_run.finished_at = datetime.now(timezone.utc)
        task_run.state = _as_json_safe(snapshot)
        task_run.meta = _run_meta(task_run, partials, meta_ctx)
        # review_timeout 特判（docs/05 §5.2 / errors.py 注释预告的"执行层特判"）：
        # 跑完但最后一次审核仍是 fail 判词 = retry 超限被降级放行——
        # 状态照旧 completed（报告带警告，§2.5），failure_category 记下这次"没真过关"，
        # 否则 Evaluation 统计里这类"带水通过"的任务全算成干净成功。
        review = (partials.get("reviewer") or {}).get("review") or {}
        if review.get("verdict") != "pass" and review.get("reasons"):
            task_run.failure_category = "review_timeout"
        task.status = "completed"
        # Phase 9b：把这次执行的报告投影成 reports 表的一行（没报告就是 no-op）。
        # 位置在 `task_run.meta` 写完之后、本拍 commit 之前 —— 与执行结论**同事务**：
        # 报告行和它引用的执行行要么一起在、要么一起不在。
        # 为什么投影而不是把报告正文留在 meta 里就够：`/reports` 要的是"独立资源"
        # （列表 / 按 id 取 / 删除 / 回跳来源），挂在执行上的一个键给不出这些形状。
        # 失败不阻断落库：报告是投影层，它出问题不该把一条已经跑成的执行判成 failed
        #（真值本来就在 task_run.meta / state 里）——只留痕，由运维重投影。
        try:
            await report_service.write_for_terminal_run(session, task=task, task_run=task_run)
        except Exception:  # noqa: BLE001
            logger.exception("报告投影落库失败（执行已成功，不影响 task_run 结论）: task=%s", task.id)
            # 必须 rollback：flush 失败会把会话留在 "PendingRollbackError" 状态，
            # 下面那句 commit 会直接抛 —— 那就是"投影层出问题把已跑成的执行炸成 failed"
            # 的路径，正是这个 try 要挡的事。rollback 会把 task/task_run 标过期，
            # 所以两个对象要在 commit 前 refresh 回来（形状照 _fail 的同一段）。
            await session.rollback()
            await session.refresh(task)
            await session.refresh(task_run)
        await session.commit()
        # T8 终态写侧接线（completed）：轮询者下一次读命中缓存即见终态，不必等 TTL
        await cache_state(task, summary={"progress": 100})
    except TaskNodeError as exc:
        # ⑤ 节点级错误已带分类：直接落库，不猜
        await _fail(session, task, task_run, spans, partials, exc.category, str(exc),
                    ctx.tool_call_sink, snapshot, meta_ctx)
    except Exception as exc:  # noqa: BLE001 —— 图外层（网络断连等）也不让任务悬在 running
        await _fail(session, task, task_run, spans, partials, "internal_error", str(exc),
                    ctx.tool_call_sink, snapshot, meta_ctx)
    return task_run


def _run_meta(task_run: TaskRun, partials: dict[str, dict], meta_ctx: dict | None = None) -> dict:
    """跑完后的零散结论收进 task_run.meta（不单独建列，见 TaskRun.meta 注释）。

    report / reviewer_verdict / retry_count 都从 partials 取：每个节点只留最终一次尝试的
    输出（重试会覆盖旧值），所以这里拿到的正是「最后定稿的报告」和「最后一次审核的结论」。
    report 落 task_run.meta 而非独立 reports 表：Phase 5 出口只要求「出报告 + Trace 可查」，
    reports 表是后面 Phase（06-api §2.5 报告详情）的事，这里不提前建。
    """
    reviewer_partial = partials.get("reviewer") or {}
    review = reviewer_partial.get("review") or {}
    # latency 降级（Phase 7 修复轮 M1②）：_fail 是公共收尾件，不得假设调用方设过
    # started_at —— workflow trigger 预建的 pending 执行行就没设（ models 该列可空）。
    # 缺任一时间戳时 latency_ms 记 None（键仍在，形状不破），而不是 TypeError
    # 把整个 failed 落库半途崩掉。
    started_at, finished_at = task_run.started_at, task_run.finished_at
    return {
        "latency_ms": (
            int((finished_at - started_at).total_seconds() * 1000)
            if started_at is not None and finished_at is not None else None
        ),
        "report": (partials.get("report") or {}).get("report"),
        "reviewer_verdict": review.get("verdict"),
        "retry_count": reviewer_partial.get("retry_count"),
        # question：tasks.question 恒为首轮问题（它是列表页标题，不能被追问改写），
        #           每轮实际问题记在 meta 里，供列表页 / 评测直读而不用解整块 state。
        "question": (meta_ctx or {}).get("question"),
        # memory：本轮到底继承了什么、被裁掉多少、哪几轮快照坏掉（docs/11 §7 降级必须留痕）
        "memory": (meta_ctx or {}).get("memory"),
    }


def _as_json_safe(value: dict | None) -> dict | None:
    """快照 → JSONB 插得进去的形状。能挡的都挡，但**不保证一定转得动**：

    state 里的值是模型/工具产出的，类型不受控（Phase 5 审计就因工具返回 None 崩过一次）。
    default=str 兜住「转不动但能字符串化」的对象（坏对象变可读字符串，比整块快照丢失好得多）；
    allow_nan=False 把 inf/nan 提前拦在这里 —— 不拦的话 json.dumps 默认放行，
    到 jsonb 强转时才被库拒掉，那炸的是 commit，比这里抛异常难收拾得多。
    残余风险（循环引用、非字符串键等）仍可能抛：成功路径有 run_task 外层 try 接住转 _fail，
    _fail 里再抛则由调用处的降级闸兜住（见 _fail 快照赋值处）—— 那条注释解释了为什么必须加闸。
    """
    if value is None:
        return None
    return json.loads(
        json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)
    )


def _summary(value: dict | None, limit: int = 500) -> str:
    """节点输出 → Trace 页摘要：content 类取正文，其余 JSON 串化，一律截断。"""
    if not value:
        return ""
    text = value.get("content") if isinstance(value, dict) and "content" in value else json.dumps(value, ensure_ascii=False, default=str)
    return str(text)[:limit]


async def _persist_agent_runs(
    session: AsyncSession,
    spans: list[dict],
    task: Task,
    task_run: TaskRun,
    partials: dict[str, dict],
) -> None:
    """spans → agent_runs。重试产生的多条 span 各自成行（Trace 显示每次尝试）。

    token 取 span 自带的「本次尝试增量」（node_guard 用 meta 差值算好，见 errors.py），
    不从 partials 的累计 meta 取——后者逐行相加会三角式虚高。
    """
    for span in spans:
        prompt_tokens = span.get("prompt_tokens", 0)
        completion_tokens = span.get("completion_tokens", 0)
        session.add(
            AgentRun(
                # span 身份由 node_guard 在节点进入时发号（重试各尝试各一个），落库沿用同一个：
                # tool_calls.parent_span_id 指的就是它，两侧必须是同一个 id，树才连得起来。
                # 兜 uuid4 只给手工造的旧 span（无 "span_id" 键）用，不让既有 span 落不进库。
                span_id=span.get("span_id") or uuid.uuid4(),
                trace_id=task_run.trace_id,
                task_id=task.id,
                task_run_id=task_run.id,
                agent_name=span["node"],
                status=span["status"],
                output_summary=_summary(partials.get(span["node"])),
                duration_ms=span["duration_ms"],
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                # cost 从 Phase 6 起才有值（docs/05 建表时留的欠账）。未配置单价时写 0
                # 而**不是 None**：这列 NOT NULL DEFAULT 0，给 None 直接报错。
                # "不可得"这个信息记在评测层的 cost_available 上（Task 2），别在源头造假。
                cost=pricing.compute_cost(prompt_tokens, completion_tokens) or 0,
                model=settings.llm_model,
                error_message=span["error_message"],
                started_at=span["started_at"],
                finished_at=span["finished_at"],
            )
        )


async def _persist_tool_calls(
    session: AsyncSession, ctx: ToolContext, task: Task, task_run: TaskRun
) -> None:
    """节点累积的 ToolCallRecord → tool_calls 表（Phase 6 工具成功率 / Trace 工具层数据源）。"""
    rows = [to_tool_call_row(r) for r in (ctx.tool_call_sink or [])]
    await tool_call_repo.add_task_tool_calls(
        session,
        task_id=task.id,
        task_run_id=task_run.id,
        trace_id=task_run.trace_id,
        organization_id=task.organization_id,
        user_id=task.user_id,
        rows=rows,
    )


async def _fail(
    session: AsyncSession,
    task: Task,
    task_run: TaskRun,
    spans: list[dict],
    partials: dict[str, dict],
    category: str,
    message: str,
    tool_call_records: list | None = None,
    snapshot: dict | None = None,
    meta_ctx: dict | None = None,
) -> None:
    """终态落库：failed + failure_category。span 照样写——失败的执行也是数据。

    queued 可达性核对（spec §4.3 终态守卫，8b T4 收口时逐调用点实核）：_fail 只被
    run_task 内部两条 except 支路与 workflow_service 的既有支路调用，进入时 run 行
    都已被幂等门（或 trigger 侧 promote）推成 running —— **不存在 queued 直落终态路径**，
    终态守卫无需为 queued 加白名单。
    """
    logger.error("任务 %s 第 %s 次执行失败（%s）: %s", task.id, task_run.run_no, category, message)
    # 失败可能源自 DB 本身（会话已被判 abort）：先回滚恢复会话，否则下面的留痕落库会二次失败。
    # rollback 会把 task/task_run 标过期，async 下同步访问过期属性会炸——refresh 恢复。
    await session.rollback()
    await session.refresh(task)
    await session.refresh(task_run)
    await _persist_agent_runs(session, spans, task, task_run, partials)
    # 失败前已发生的工具调用照样留痕（ToolCallRecord 是内存对象，不随 rollback 丢）：
    # 正是 Failure Analysis 要下钻的「哪一步的工具先炸了」。
    rows = [to_tool_call_row(r) for r in (tool_call_records or [])]
    await tool_call_repo.add_task_tool_calls(
        session,
        task_id=task.id,
        task_run_id=task_run.id,
        trace_id=task_run.trace_id,
        organization_id=task.organization_id,
        user_id=task.user_id,
        rows=rows,
    )
    task_run.status = "failed"
    task_run.failure_category = category
    task_run.error_message = message
    task_run.finished_at = datetime.now(timezone.utc)
    # 失败也写快照：失败的执行也是数据（§7）。读侧只认 completed，所以这份快照
    # 只用于评测 / Trace 下钻，永远不会被当成"上一轮的结论"喂回去。
    # 快照是 _fail 要写的最不要紧的一样：_as_json_safe 仍转不动（残余形态见它的 docstring）
    # 时宁可 state=NULL 也不能让异常逃出 except 块 —— 一逃，末尾 commit 不执行，
    # 那条 running 的 task_runs 就永久挂库，has_running_run 让 rerun 和 follow-up 双双 409，
    # 任务被一个「序列化失败」钉死在 running（docs/11 §1 明说本 Phase 不做记忆重置端点，没有恢复通道）。
    try:
        task_run.state = _as_json_safe(snapshot)
    except Exception:  # noqa: BLE001 —— 失败记录必须落地，快照让路
        task_run.state = None
        logger.exception(
            "任务 %s 第 %s 轮快照序列化失败，state 记 NULL（failed 记录照常落库）",
            task.id, task_run.run_no,
        )
    # 成功失败同口径：失败也留 latency（+ 已跑到哪一步的零散结论），供 Evaluation 统计耗时分布
    # 合并而不是覆盖（Phase 7 workflow 复用本路径）：trigger 在开跑前就把 workflow_input
    # 写进了 meta，失败留痕要带着它。agent 侧执行前 meta 恒为 None，合并是原样行为。
    task_run.meta = {**(task_run.meta or {}), **_run_meta(task_run, partials, meta_ctx)}
    task.status = "failed"
    await session.commit()
    # T8 终态写侧接线（failed）：_fail 是 agent/workflow 两族共用的失败收尾，
    # 挂这里一处 = 两条执行面的 failed 都进缓存（终态三处：completed/_fail/rejected 各一接线点）。
    await cache_state(task)
