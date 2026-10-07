"""Workflows API（Phase 7 Task 7）：编目列表/详情 + 触发（docs/06 §2.6 / spec §8）。

分层同 `api/evaluations.py`：路由只取身份 → 读闸 → 调 service → 选响应模型。
与那边的一处刻意差异：本文件的读侧直接调 `workflow_repo`（编目列表/详情、纯投影
读，没有业务判断可下沉——为这点查询再包一层 service 函数是传话代码）；
**执行与决策全部走 `workflow_service`**，路由不碰状态机。

三条硬口径（控制器裁定，绑定实现）：
1. **trigger 的归属闸在路由**：`workflow_service.trigger` 不校验 workflow.organization_id
   （其 docstring 写明「编目归属校验归调用方」），所以路由必须先经
   `workflow_repo.get_workflow`（org 过滤 + is_active）拿行——「不存在」「跨租户」
   「已停用」同返 404（`WF_404001`），过了闸才把 ORM 行交给 trigger。
2. **inputs 必填闸对着编目行的 input_spec 做**（数据驱动的形状，静态模型锁不住）：
   缺键 raise `RequestValidationError` 走 FastAPI 原生 422（错误体点名缺的字段），
   与 pydantic 自己拒字段同形 —— 不另立业务码。
3. **响应 202 + 异步口径**：trigger 落 Task/TaskRun 行 + 入队即返，图在 worker 进程跑
   （引擎自开会话），进度/终态/Trace 复用 `GET /agents/tasks/...` 家族，不新开端点。
   8b T5 起这条是真话：此前「后台跑图」是请求进程内的裸协程（进程死 = 任务蒸发）。

入队与预检两处共用 `api/agents.py` 的 helper（get_arq_pool / enqueue_run_job /
precheck_graph_access）：三个提交面共用一把闸，不各写第三份。

审批两条路由不在本文件：审批资源属于 task，挂在 `api/agents.py` 的
`/tasks/{task_id}/approvals` 家族（裁定：不另起 URL 家族）。

租户隔离：编目表只有 org 列（Task 2 建表即 org 级共享目录），读闸 org 一维；
审批经 tasks JOIN 做 org+user 双过滤 —— 两套口径各自的边界见 workflow_repo 注释。
"""
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import workflow_service
from app.api.agents import enqueue_run_job, get_arq_pool, precheck_graph_access
from app.api.deps import CurrentUser
from app.core.audit import write_audit
from app.core.config import settings
from app.core.exceptions import DocumentNotFoundError, WorkflowNotFoundError
from app.core.rate_limit import rate_limit_dep
from app.data.db import get_db
from app.data.repositories import document_repo, workflow_repo
from app.schemas.workflow import TriggerIn, TriggerOut, WorkflowOut
from app.workers.jobs import JOB_WORKFLOW_EXECUTE

router = APIRouter(prefix="/workflows", tags=["workflows"])

# 8b T9：trigger 与 agent 提交面共享 "task" 桶（spec §6 表同一行，rl:task:{uid} 一个账本）。
_rl_task = rate_limit_dep("task", lambda: settings.rate_limit_task_per_min)


def _require_spec_inputs(inputs: dict, spec: dict) -> None:
    """input_spec 的每个键都是触发必填；缺键或空值（None / 全空格字符串）→ 422（fastapi 原生校验体）。

    只判「在不在 + 空不空」不判值类型：spec 的值是给人看的类型声明（"string"/"uuid"），
    没有正式语法，硬编码一套解析器反而多一个真相源；值坏到跑不动图，执行侧
    自会有 failed + error_message（引擎侧口径，不在 HTTP 层抢答）。
    「必填」对 str/None 含非空之意：`question=""` 放进闸，换来的是一次没人要的
    合成 run（服务侧还回填默认标题），不是 422 也不是诚实的 failed —— 空值即缺。
    其他值类型（dict/list/int…）刻意不校验，执行侧 failed 兜底。
    input_spec 之外的多余键不限：照收并随 inputs 落进 task_run.meta["workflow_input"]
    （闸只把必填键的存在性，不给 inputs 白名单收口——收口的是 body 顶层，见 TriggerIn）。
    """
    missing = sorted(
        k for k in (spec or {})
        if k not in inputs
        or inputs[k] is None
        or (isinstance(inputs[k], str) and not inputs[k].strip())
    )
    if missing:
        raise RequestValidationError(
            [
                {"type": "missing", "loc": ["body", "inputs", k], "msg": "Field required",
                 "input": inputs}
                for k in missing
            ]
        )


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> list[WorkflowOut]:
    """编目列表：本 org 的可用 workflow（预置 3 条起步），创建序在前。"""
    organization_id = user.organization_id
    rows = await workflow_repo.list_workflows(session, organization_id=organization_id)
    return [WorkflowOut.model_validate(r) for r in rows]


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    user: CurrentUser,
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    """编目详情（前端渲触发表单靠这行的 input_spec + graph_key）。"""
    organization_id = user.organization_id
    wf = await workflow_repo.get_workflow(
        session, workflow_id=workflow_id, organization_id=organization_id
    )
    if wf is None:
        raise WorkflowNotFoundError()
    return WorkflowOut.model_validate(wf)


@router.post("/{workflow_id}/trigger", response_model=TriggerOut, status_code=202,
             dependencies=[Depends(_rl_task)])
async def trigger_workflow(
    user: CurrentUser,
    workflow_id: uuid.UUID,
    req: TriggerIn,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TriggerOut:
    """触发一次执行：归属闸 → input_spec 必填闸 → document 归属预检（8a）→ F1 角色预检
    → 队列可用性预检 → 落 Task/TaskRun → 入队 → 202。

    闸的顺序就是它的语义：先「这东西存不存在、是不是你的」（404）、再「形状对不对」
    （422）、再「你有没有资格跑它」（403）、最后才「有没有人跑得动」（503）。
    资格闸排在可用性闸之前：无权的人不该因为 Redis 恰好挂了收到 503 而以为
    「我有权限只是在排队」。

    202（而非 200）：返回时执行还没开始 —— status 恒为落库初值 pending
    （worker 领 job 时才由幂等门推成 running），真实进度请轮询
    GET /agents/tasks/{task_id}（workflow 任务与 agent 任务同构可读）。
    """
    organization_id, user_id = user.organization_id, user.id
    wf = await workflow_repo.get_workflow(
        session, workflow_id=workflow_id, organization_id=organization_id
    )
    if wf is None:
        raise WorkflowNotFoundError()
    _require_spec_inputs(req.inputs, wf.input_spec)
    # 8a 预检（Phase 7 欠账 #8）：document_id 必须先证实"是本 org 的"。
    # 不预检的话错误要等图跑到 ingest 才以"文档未就绪"现形——泄漏了他 org 资源的存在；
    # 预检后错因统一成"不存在"（DOC_404001 同形闸），与 404 探测防护口径对齐。
    doc_id = req.inputs.get("document_id")
    if doc_id is not None:
        try:
            doc_uuid = uuid.UUID(str(doc_id))
        except ValueError:
            raise DocumentNotFoundError()
        if await document_repo.get_document(session, document_id=doc_uuid, organization_id=organization_id) is None:
            await write_audit(organization_id=organization_id, user_id=user_id, action="trigger_rejected", target_type="document", target_id=str(doc_id))
            raise DocumentNotFoundError()
    # 8b T5 / R-8b-2 F1 预检：这张图的工具全集里有一个本角色够不着 → 整单 403。
    # 判据取图全集（不是"这次会走到哪几个节点"）：放行后在中段吃 in-band 403002
    # 静默降质正是 8a 终评 F1 实测的洞（member 跑 business_qa：sql_query 被拒后
    # 模型换工具继续，任务照样 completed，产物已经是坏的）。
    # 编目行是真 id、审计沿用 trigger_rejected（与上面 document 预检同一 action，
    # detail.missing 区分拒因）；agent 面另立 task_rejected，见 api/agents.py。
    await precheck_graph_access(
        graph_key=wf.graph_key, role=user.role,
        organization_id=organization_id, user_id=user_id,
        audit_action="trigger_rejected", target_type="workflow", target_id=str(wf.id),
    )
    # 队列预检先于建行（agent 面同款纪律）：503 时刻零新行，不给 sweeper 留孤儿
    pool = get_arq_pool(request)
    task_id, task_run_id = await workflow_service.trigger(
        session, workflow=wf, inputs=req.inputs,
        organization_id=organization_id, user_id=user_id,
    )
    # 入队必须在 trigger 的 commit 之后（service 里那句 commit 就是这条界线）：
    # worker 开的是另一个会话，未提交的行它看不见 —— 早投一秒就是一次「领不到行」的空跑。
    # _job_id=wfexec:{run_id}：同一 run 的重复触发投递被 arq 去重拒掉（幂等门的第二道）。
    await enqueue_run_job(
        pool, function=JOB_WORKFLOW_EXECUTE, run_id=task_run_id,
        job_id=f"wfexec:{task_run_id}",
    )
    # 恒与 trigger 落库初值一致（workflow_service.trigger 建 TaskRun(status="pending")
    # 后才返回）；改 trigger 初始态时同批改这里。
    return TriggerOut(task_id=task_id, task_run_id=task_run_id, status="pending")
