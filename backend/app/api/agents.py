"""Agents / Tasks API（Phase 5 建；Phase 8b T4 起 202+queued）。

这是 Phase 5 出口「任务列表/详情 API 可见执行状态」的落点，也是端到端演示的入口。
接口形状对齐 docs/06-api-design.md §2.4。

**8b T4 执行面收口**（spec §3.2/§8.1）：三个提交类端点（submit / rerun / follow-up）
不再请求内跑图——service 建 queued 行、commit，本层入队（app.state.arq_pool），
立刻回 **202 + status 恒 "queued"**；执行由 worker 进程领 job 接手，前端轮询详情端点
看 queued→running→终态（进度观测一直是轮询，跨进程后依旧成立，不需要中继设计）。

分层照 chat 一样薄：路由只取身份 → 调 service → 选响应模型 → 入队，业务判断全在 service；
入队是装配面细节（池在 app.state），service 层不认 arq。
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.graph.toolsets import missing_tools
from app.application import agent_task_service, workflow_service
from app.api.deps import CurrentUser
from app.core.audit import write_audit
from app.core.config import settings
from app.core.exceptions import TaskNotFoundError, ToolPermissionError
from app.core.rate_limit import rate_limit_dep
from app.data.db import get_db
from app.data.repositories import task_repo, workflow_repo
from app.schemas.agent_task import (
    TaskCreate,
    TaskDetailOut,
    TaskFollowUp,
    TaskMemoryOut,
    TaskOut,
    TaskRunOut,
    TaskSubmitOut,
    TraceOut,
)
from app.schemas.workflow import ApprovalOut, DecisionIn
from app.workers.jobs import JOB_RUN_AGENT_TASK, JOB_WORKFLOW_RESUME

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

# 图注册表缝死 task_type 直填（spec §8.1）：/agents 提交面只认 agent_analysis。
# "workflow" 走 /workflows 面触发（Phase 7 定型：同 task 叠 agent run = 审批门旁路），
# 这里曾有的直填缝让任意字符串照收落库 + agent 整图直跑，T4 收口。
AGENT_GRAPH_TASK_TYPES = {"agent_analysis"}

# 8b T9 提交面限流（spec §6 表第二行：批量烧模型；identity=user）。桶名 "task" 是
# 跨面的同一个桶——本文件与 workflows.trigger / evaluations.runs 三处各自实例化，
# 但 rl:task:{uid} 一个账本（针⑪验共享）。上限进 Settings，lambda = 测试接缝。
_rl_task = rate_limit_dep("task", lambda: settings.rate_limit_task_per_min)


def get_arq_pool(request: Request):
    """入队前预检（R-8b-7 移交账）：Redis 开机不可达时 lifespan 兜底 None（API 照常起），
    入队面见 None 如实 503——不做「建了行没人跑」的假 202。

    错误形状留痕：spec §3/§8 未给入队面钦定业务码（§6 的 COMMON_503001 是限流面的
    redis-down 码，归 T9），故此处用 HTTPException(503) 框架体；若 T9 落信封后需统一，
    改这一个函数。预检先于建行：503 响应时刻零新行，不给 sweeper 留排不上队的孤儿 queued 行。

    T5 起对 workflow 面也开放（workflows.py / decide 端点复用同一函数、同一口径），
    故名去掉下划线：两个面共用一把闸，不各写一份。
    """
    pool = request.app.state.arq_pool
    if pool is None:
        raise HTTPException(503, "任务队列暂时不可用，任务未提交，请稍后重试")
    return pool


async def enqueue_run_job(
    pool, *, function: str, run_id: uuid.UUID, job_id: str, extra_args: tuple = ()
) -> None:
    """通用入队（8b T5 收口）：只传 UUID 字符串 + _job_id 去重留痕。

    三个提交面（agent / workflow_execute / workflow_resume）共用这一份：
    enqueue_job 的位置参数只有 str(run_id)（resume 多带一个 str(approval_id)，走
    extra_args —— 仍是 UUID 字符串，不是对象），身份、业务数据一律 worker 回库读
    —— 队列消息不可伪造身份（8a 同一条防线）。
    _job_id 由调用方给（形如 f"wfexec:{run_id}"）：arq 对同 id 的在飞/在队 job 拒绝二次入队。

    enqueue_job 返回 None = 去重命中（同 _job_id 的旧 job 还在队/在飞）。本端点语境
    该行必是刚建的待领行、同 id 只可能来自 sweeper 重入队或对同一行的再投递——
    那正是「去重在工作」而非故障：行已持久化且队里必有一个 job 会领它，
    所以**仍回 202**（改口 500 反而是假警报），只记 info 供对账。
    """
    job = await pool.enqueue_job(function, str(run_id), *extra_args, _job_id=job_id)
    if job is None:
        logger.info("arq 去重命中：%s 已在队/在飞，行等认领", job_id)


async def enqueue_agent_task(pool, task_run_id: uuid.UUID) -> None:
    """agent 面入队（enqueue_run_job 的固定参数壳，job_id 前缀 agent:）。"""
    await enqueue_run_job(pool, function=JOB_RUN_AGENT_TASK, run_id=task_run_id,
                          job_id=f"agent:{task_run_id}")


async def precheck_graph_access(
    *, graph_key: str, role: str, organization_id: uuid.UUID, user_id: uuid.UUID,
    audit_action: str, target_type: str, target_id: str | None = None,
) -> None:
    """F1 角色预检（R-8b-2，绑定裁定）：图的工具全集里有一个够不着 → 整单 403。

    判据是 **图的工具全集**（GRAPH_TOOLSETS 静态表），不是「这次会路由到哪几个节点」——
    后者要真跑图才知道，而真跑图的第一个副作用就是花钱。宁可按最宽的那一面拒。

    为什么不只靠执行期的 in-band 闸（registry.execute 的 403002）：
        8a 终评 F1 实测的就是那个洞——member 跑 agent_analysis，data_analyst 站
        sql_query 被拒后模型静默换个工具继续，**任务照样 completed，产物质量降级**。
        提交那一刻信息齐全（角色 + 图 → 工具集），拒得越早用户越不需要猜。
    顺序：本闸先于 pool 预检（权限永远赢过可用性：Redis 挂了也不给无权的人留 503 的
    错觉），也先于建行（403 响应时刻零新行）。
    留痕：audit 独立会话、永不抛，拒单也要留「谁在哪个图上被拒、缺哪些工具」。
    """
    missing = missing_tools(graph_key, role)
    if not missing:
        return
    await write_audit(
        organization_id=organization_id, user_id=user_id, action=audit_action,
        target_type=target_type, target_id=target_id,
        detail={"graph_key": graph_key, "role": role, "missing": sorted(missing)},
    )
    raise ToolPermissionError(
        f"当前角色无权使用该{target_type}：图 '{graph_key}' 需要的工具 "
        f"{sorted(missing)} 未被授权"
    )


@router.post("/tasks", response_model=TaskSubmitOut, status_code=202,
             dependencies=[Depends(_rl_task)])
async def submit_task(
    user: CurrentUser,
    req: TaskCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TaskSubmitOut:
    """提交一次 Multi-Agent 分析任务：**建行 + 入队 + 立刻 202**（8b T4，不再请求内跑图）。

    响应形状不变（两个 id + status），初值恒 "queued"；执行进度靠轮询详情端点。

    8b T5 追加 F1 角色预检：agent 面固定跑 agent_analysis 图，其 DATA_TOOLS 含
    admin-only 的 sql_query → member 提交在此就 403（不再是「跑完了但质量静默降级」）。
    """
    if req.task_type not in AGENT_GRAPH_TASK_TYPES:
        # 直填缝 422 用 RequestValidationError 同形（workflows.py _require_spec_inputs 先例）：
        # FastAPI 原生校验体点名非法字段，前端按 loc 定位，不再「照收落库」给图外类型开门。
        raise RequestValidationError(
            [{"type": "value_error", "loc": ["body", "task_type"],
              "msg": f"task_type 必须在 agent 图注册表内 {sorted(AGENT_GRAPH_TASK_TYPES)}",
              "input": req.task_type}]
        )
    organization_id, user_id = user.organization_id, user.id
    await precheck_graph_access(
        graph_key=req.task_type, role=user.role,
        organization_id=organization_id, user_id=user_id,
        audit_action="task_rejected", target_type="task",
    )
    pool = get_arq_pool(request)
    run = await agent_task_service.create_queued(
        session,
        question=req.question,
        organization_id=organization_id,
        user_id=user_id,
        task_type=req.task_type,
    )
    await enqueue_agent_task(pool, run.id)
    return TaskSubmitOut(task_id=run.task_id, task_run_id=run.id, status="queued")


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    user: CurrentUser,
    status: str | None = Query(None),
    task_type: str | None = Query(None),
    # `ge=1` 不是装饰：少了它，`?limit=-1` 会一路走到 task_repo 的 `.limit(-1)`，
    # 由 PG 抛「LIMIT must not be negative」→ **500**，而 docs/06 §7.1 承诺过滤参数
    # 一律在边界校验（非法值 422）。三个兄弟端点（documents.py:97 / conversation.py:68 /
    # evaluations.py:89）本来就是 `Query(50, ge=1, le=200)`，这里是对齐而不是新口径。
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> list[TaskOut]:
    """任务列表：可按状态/类型过滤，最近创建在前。"""
    organization_id, user_id = user.organization_id, user.id
    tasks = await agent_task_service.list_tasks(
        session,
        organization_id=organization_id,
        user_id=user_id,
        status=status,
        task_type=task_type,
        limit=limit,
        offset=offset,
    )
    return [TaskOut.model_validate(t) for t in tasks]


@router.get("/tasks/{task_id}", response_model=TaskDetailOut)
async def get_task_detail(
    user: CurrentUser,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> TaskDetailOut:
    """任务详情：含最近一次执行摘要 + 报告（在 latest_run.meta.report 里）。

    T8 cache-first 读侧（spec §10 缓存针）：status 一位走 agent_task_service 的
    read helper（命中用缓存、miss/坏值/redis 断静默回落 PG——响应逐字段同形是硬约束，
    两态一致性在 scratch/test_p8b_cache.py 针⑧演）；latest_run 明细恒取 PG，
    缓存不过拟合执行细节（v1 只加速轮询真正需要的 task 状态位）。
    """
    organization_id, user_id = user.organization_id, user.id
    task, latest, count = await agent_task_service.get_task_detail(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    data = TaskOut.model_validate(task).model_dump()
    data["status"] = await agent_task_service.cache_first_status(task)
    return TaskDetailOut(
        **data,
        latest_run=TaskRunOut.model_validate(latest) if latest else None,
        run_count=count,
    )


# ---- Phase 7 Task 7：审批两条路由（审批资源属于 task，挂本家族，不另起 URL）----


@router.get("/tasks/{task_id}/approvals", response_model=list[ApprovalOut])
async def list_task_approvals(
    user: CurrentUser,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> list[ApprovalOut]:
    """某任务的审批记录列表（pending 在前）。审批人=当前 org+user，多用户审批角色不在 Phase 7 范围。

    task 先过 org+user 双过滤（task_repo）：「不存在」与「别人的」同返 404，
    与任务面其他端点同形；列表读经 workflow_repo 的 approvals×tasks JOIN 再滤一遍，
    两道闸同一口径（读侧不赌调用方干净）。

    非 workflow 任务（如 agent_analysis）不特判：过 task 闸后无审批行，回 200 `[]`
    —— 对它回 404 等于泄露「这不是 workflow 任务」这一存在性信号，故表恒空而不转 404。
    """
    organization_id, user_id = user.organization_id, user.id
    task = await task_repo.get_task(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    if task is None:
        raise TaskNotFoundError()
    rows = await workflow_repo.list_approvals(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    return [ApprovalOut.model_validate(r) for r in rows]


@router.post("/tasks/{task_id}/approvals/{approval_id}", response_model=ApprovalOut)
async def decide_task_approval(
    user: CurrentUser,
    task_id: uuid.UUID,
    approval_id: uuid.UUID,
    req: DecisionIn,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ApprovalOut:
    """审批决策：放行/驳回落库 → 入队续跑（8b T5，不再请求内起后台跑）。审批人=当前 org+user。

    业务闸全在 workflow_service.decide_approval（404 同形 / 409 状态冲突 /
    条件 UPDATE 竞态锁），路由只做入参白名单 + 身份注入 + 入队：决策人 id 不进
    DecisionIn —— 谁点的按钮由服务端身份说了算，body 冒充不了审批人。

    pool 预检放在决策**之前**：队列不可用时决策不落库，用户重试即原样再点一次；
    反过来（先决策后入队）会留下「已批准但永远不跑」的僵尸 waiting_approval。
    去重：_job_id=wfresume:{run_id} —— 同一 run 的续跑 job 在队/在飞时二次入队被拒，
    返回 None 只记 info（真发生并发双击时，赢的那次决策已生效，输的这次 409 在半路）。
    """
    organization_id, user_id = user.organization_id, user.id
    pool = get_arq_pool(request)
    approval, run_id = await workflow_service.decide_approval(
        session,
        task_id=task_id,
        approval_id=approval_id,
        decision=req.decision,
        organization_id=organization_id,
        user_id=user_id,
        comment=req.comment,
    )
    if run_id is not None:
        await enqueue_run_job(
            pool, function=JOB_WORKFLOW_RESUME, run_id=run_id,
            extra_args=(str(approval_id),), job_id=f"wfresume:{run_id}",
        )
    return ApprovalOut.model_validate(approval)


@router.post("/tasks/{task_id}/rerun", response_model=TaskSubmitOut, status_code=202,
             dependencies=[Depends(_rl_task)])
async def rerun_task(
    user: CurrentUser,
    task_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TaskSubmitOut:
    """重新执行（新 queued run + 入队 → 202；8b T4）。409=名下已有 queued/running 在飞；404=找不到。"""
    organization_id, user_id = user.organization_id, user.id
    pool = get_arq_pool(request)
    run = await agent_task_service.rerun_queued(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    await enqueue_agent_task(pool, run.id)
    return TaskSubmitOut(task_id=run.task_id, task_run_id=run.id, status="queued")


@router.post("/tasks/{task_id}/follow-up", response_model=TaskSubmitOut, status_code=202,
             dependencies=[Depends(_rl_task)])
async def follow_up_task(
    user: CurrentUser,
    task_id: uuid.UUID,
    req: TaskFollowUp,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TaskSubmitOut:
    """追问（新 queued run + 带记忆标记 + 入队 → 202；8b T4）。

    记忆装配不在本请求内做——建行只落 meta={"memory": True, "question": …} 标记，
    worker 领 job 时才现算（提交侧算会竞态读不到前轮终态快照）。响应与 submit/rerun
    同构，前端复用同一套轮询。
    """
    organization_id, user_id = user.organization_id, user.id
    pool = get_arq_pool(request)
    run = await agent_task_service.follow_up_queued(
        session,
        task_id=task_id,
        question=req.question,
        organization_id=organization_id,
        user_id=user_id,
    )
    await enqueue_agent_task(pool, run.id)
    return TaskSubmitOut(task_id=run.task_id, task_run_id=run.id, status="queued")


@router.get("/tasks/{task_id}/memory", response_model=TaskMemoryOut)
async def get_task_memory(
    user: CurrentUser,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> TaskMemoryOut:
    """记忆可见：这一串轮次各自得出什么 + 下一轮实际会继承到的那份 memory_context。

    每轮视图的投影（快照 isinstance 闸 / 截断 / 窗口命中）在 service 做完：本文件只管
    身份 → service → 响应模型；rounds 是视图 dict，pydantic 按 MemoryRoundOut 校验。
    """
    organization_id, user_id = user.organization_id, user.id
    task, rounds, mc = await agent_task_service.get_task_memory(
        session, task_id=task_id, organization_id=organization_id, user_id=user_id
    )
    return TaskMemoryOut(task_id=task.id, rounds=rounds, next_memory_context=mc)


@router.get("/task-runs/{run_id}", response_model=TaskRunOut)
async def get_run_status(
    user: CurrentUser,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> TaskRunOut:
    """一次执行的状态 / 进度（前端轮询用）。"""
    organization_id, user_id = user.organization_id, user.id
    run = await agent_task_service.get_run(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    return TaskRunOut.model_validate(run)


@router.get("/task-runs/{run_id}/trace", response_model=TraceOut)
async def get_run_trace(
    user: CurrentUser,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> TraceOut:
    """一次执行的 Trace：agent 与 tool 统一节点序列（按 started_at 升序，含每次重试的独立 span）。"""
    organization_id, user_id = user.organization_id, user.id
    run, nodes = await agent_task_service.get_trace(
        session, run_id=run_id, organization_id=organization_id, user_id=user_id
    )
    return TraceOut(
        trace_id=run.trace_id,
        task_run_id=run.id,
        status=run.status,
        nodes=nodes,
    )
