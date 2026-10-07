"""arq 任务函数（Phase 8b spec §3）。

入队参数只带 UUID 字符串——业务数据与身份（role）一律领 job 后回库读
（与 8a「role 不从请求体来」同一条防线：队列消息不可伪造身份）。
service 层不认 arq ctx，故 job 形状（ctx 首参 + 自开会话）长在本文件：
jobs.py 是「队列世界」与「service 世界」的翻译层，两侧都不越界。
"""
import logging
import uuid

from app.application import (
    agent_task_service,
    document_service,
    evaluation_service,
    workflow_service,
)
from app.data.db import AsyncSessionLocal
from app.data.models import Document, Task, TaskRun, WorkflowApproval
from app.data.repositories import evaluation_repo, task_repo

JOB_RUN_AGENT_TASK = "run_agent_task"
JOB_WORKFLOW_EXECUTE = "workflow_execute"
JOB_WORKFLOW_RESUME = "workflow_resume"
JOB_RUN_EVALUATION = "run_evaluation"
JOB_INDEX_DOCUMENT = "index_document"

logger = logging.getLogger(__name__)


async def run_agent_task(ctx: dict, task_run_id: str) -> None:
    """领 agent 任务 job（spec §3.2 第一行）。

    幂等门先行：queued→running 推不动 = 重复投递，直接返回（at-least-once 的账在此结清）。
    身份回库读：task.user_id → User.role——不从入队参数来（队列消息不可伪造身份）。
    执行本体是 agent_task_service.run_prepared（run_task 收已建行的薄封装，图取
    worker 进程自己的一份惰性编译）；失败不抛是 run_task 的既有纪律，本函数不加戏。
    """
    run_id = uuid.UUID(task_run_id)
    async with AsyncSessionLocal() as session:
        if not await task_repo.promote_from_prepared(
            session, run_id=run_id, from_statuses=("queued",)):
            await session.rollback()
            logger.info("agent job 重复投递，跳过: %s", task_run_id)
            return
        run = await session.get(TaskRun, run_id)
        task = await session.get(Task, run.task_id)
        await session.commit()  # 认领即落：running+started_at 对全库可见（崩溃可观测）
        memory = None
        if (run.meta or {}).get("memory"):
            # 记忆装配在 worker 侧现算：提交侧算会竞态读不到前轮终态快照。
            # 判据是建行时的 meta["memory"]=True 标记（follow_up_queued 写），
            # 不是 run_no>1——rerun 也是 >1 但固定无记忆（docs/11 §6 口径）。
            memory, _ = await agent_task_service._build_memory_for(
                session, task_id=task.id, organization_id=task.organization_id, user_id=task.user_id)
        await agent_task_service.run_prepared(session, task=task, task_run=run, memory=memory)


async def workflow_execute(ctx: dict, task_run_id: str) -> None:
    """领 workflow 首程 job（8b T5，spec §3.2 第二行）。

    幂等门 from_statuses=("pending","queued")：workflow 面的落库初值是 pending
    （Phase 7 定型，T5 不改触发面语义），queued 一并收下是给 sweeper / 未来统一
    状态机留的口子。推不动 = 重复投递（at-least-once 的账在此结清），直接返回。
    认领后**必须 commit 再执行**：execute_workflow 开的是另一个会话，
    running+started_at 得先对全库可见 —— 否则它自己的 `run.status != running` 守卫
    会把刚领到的 job 当「门没领走」拒掉（自我死锁，形状与 agent 面同构）。

    身份回库读：run → task → (org, user)，execute_workflow 再用 user_id 现取 role。
    两个 id 之外的任何东西都不从入队参数来。
    """
    run_id = uuid.UUID(task_run_id)
    async with AsyncSessionLocal() as session:
        if not await task_repo.promote_from_prepared(
            session, run_id=run_id, from_statuses=("pending", "queued")):
            await session.rollback()
            logger.info("workflow execute job 重复投递，跳过: %s", task_run_id)
            return
        run = await session.get(TaskRun, run_id)
        if run is None:
            logger.error("workflow execute job 认领后行消失 run=%s（并发删行？）", task_run_id)
            return
        task = await session.get(Task, run.task_id)
        if task is None:
            logger.error("workflow execute job 找不到 task run=%s task=%s", task_run_id, run.task_id)
            return
        await session.commit()  # 认领即落：running+started_at 对全库可见（崩溃可观测）
        task_id, organization_id, user_id = task.id, task.organization_id, task.user_id
    await workflow_service.execute_workflow(
        task_id, task_run_id=run_id, organization_id=organization_id, user_id=user_id
    )


async def workflow_resume(ctx: dict, task_run_id: str, approval_id: str) -> None:
    """领 workflow 续跑 job（8b T5，spec §3.2 第三行）。

    与 execute 的**关键差异：不设幂等门**。停在断点期间 run 恒为 running
    （Phase 7 定型：同一条 run 不收尾），promote 的 `status IN (pending,queued)`
    永远推不动 —— 加了门等于给每一次合法续跑判死刑。续跑的重投防护另有三把：
        ① _job_id=wfresume:{run_id} 的 arq 去重（同 run 在队/在飞时拒二次入队）；
        ② decide 侧审批行条件 UPDATE（同一 approval 只可能赢一次 → 只入队一次）；
        ③ resume_workflow 自己的 `run.status != "running"` 守卫（终态后不再续跑）。
    ③ 就是本函数重复投递时结的账：第二次投递会在守卫处停下，终态产物不被覆写。

    decision 回库从审批行现读（approved/rejected），**不从入队参数来**：
    队列消息不可伪造放行 —— 谁批的、批没批都以库为准。
    """
    run_id = uuid.UUID(task_run_id)
    ap_id = uuid.UUID(approval_id)
    async with AsyncSessionLocal() as session:
        run = await session.get(TaskRun, run_id)
        if run is None:
            logger.error("workflow resume job 找不到 run=%s", task_run_id)
            return
        task = await session.get(Task, run.task_id)
        if task is None:
            logger.error("workflow resume job 找不到 task run=%s task=%s", task_run_id, run.task_id)
            return
        approval = await session.get(WorkflowApproval, ap_id)
        if approval is None or approval.task_id != task.id:
            logger.error("workflow resume job 审批行缺失/挂错 task run=%s approval=%s",
                         task_run_id, approval_id)
            return
        if approval.status not in ("approved", "rejected"):
            # pending = 有人绕过 decide 状态机投了个还没发生的决策（伪造 job / 数据缺陷）。
            # 宁可不跑也不替它编一个 decision：落到 resume 会把「未决策」跑成放行或拒绝。
            logger.error("workflow resume job 审批仍为 %s，拒绝续跑 approval=%s",
                         approval.status, approval_id)
            return
        decision = approval.status
        task_id, organization_id, user_id = task.id, task.organization_id, task.user_id
    await workflow_service.resume_workflow(
        task_id, task_run_id=run_id, organization_id=organization_id,
        user_id=user_id, decision=decision,
    )


async def run_evaluation(ctx: dict, eval_run_id: str, org_id: str, user_id: str) -> None:
    """领评测批次 job（8b T6，spec §3.2 第四行）。

    幂等门先行且是条件 UPDATE（evaluation_repo.promote_from_pending，一条 SQL）：
    pending→running 推不动 = 重复投递，at-least-once 的账当场结清返回。
    实测旧形状：execute_evaluation 开头的准入检查是「先 SELECT 再改」且只拒终态，
    崩溃残留 running 的行两步之间放进来就是 TOCTOU 并发重跑——门因此上提到本函数，
    pending-only 是唯一收口（拒 running 残留正是补门要结的账，不是顺手收紧）。
    认领即 commit：running+started_at 先对全库可见，execute_evaluation 开的是
    另一个会话，形状与 workflow_execute 同构（否则它读到的还是旧状态）。

    执行全量 inline 在 worker 进程内（spec §3.2 bug-4 死锁的解法）：逐用例去投
    agent 任务的 job 等于在同池 worker 里等另一个 job —— 池只有那几条槽时
    自己等自己 = 死锁。用例执行的同步核实名是 agent_task_service.submit_task
    （跑完才返回，正是评测 executor「跑完再交账」的契约；R-8b-5：spec 的概念
    执行器名不落符号，实名裁定见下方注记）。
    """
    # R-8b-5 命名映射注记：spec 概念名 run_task_inline 的实际载体 = submit_task。
    # 会话生命周期课（原锚 evaluation_service.py:574，T6 整体迁到本文件）：
    # 请求形状的执行与请求会话共死活——响应一返回会话就关，执行本体必须自开会话；
    # executor 死在失败的 flush 上时会话已进 aborted，不先 rollback 连 error 留痕
    # 都写不进（task_runner._fail 开头同款，execute_evaluation 用例循环里保留其用）。
    run_id = uuid.UUID(eval_run_id)
    async with AsyncSessionLocal() as session:
        if not await evaluation_repo.promote_from_pending(
            session, run_id=run_id,
            organization_id=uuid.UUID(org_id), user_id=uuid.UUID(user_id)):
            await session.rollback()
            logger.info("评测 job 重复投递，跳过: %s", eval_run_id)
            return
        await session.commit()  # 认领即落：running+started_at 对全库可见（崩溃可观测）
    # 自此批次在本进程内同步跑穿：execute_evaluation 自带逐用例会话编排、
    # 单条失败不外抛、终态收尾（completed/failed 由它写，失败兜底不留 running 僵尸）；
    # 本函数不加戏——收口只在门与接线，两处都在这几行里。
    await evaluation_service.execute_evaluation(
        run_id,
        organization_id=uuid.UUID(org_id),
        user_id=uuid.UUID(user_id),
        executor=agent_task_service.submit_task,
    )


async def index_document(ctx: dict, document_id: str) -> None:
    """领文档索引 job（排雷-F 后台化：upload/reindex 入队，本函数是执行体）。

    **不设幂等门**（与 workflow_resume 同理）：_index_document 是全量重建——
    先删旧 chunk 再写新、状态从头推到 ready，重跑一次自然收敛，不会像增量写
    那样撞 UNIQUE(document_id, chunk_index)。重复投递的防护另有两把：
        ① _job_id=docindex:{document_id} 的 arq 去重，且 upload 与 reindex
          两面共用同一键——同一文档跨面在飞也只会有一个 job；
        ② 去重判重面（create_document 对非 failed 同内容行 409）不让在建文档
          再收同内容新行，双 job 撞同一文档的窗口本就来自重投而非双行。
    sweeper 不扫 documents 表（裁定如实记账，09 §11.2 行 19 注记）：worker 崩溃
    残留的在建行恢复路径 = 既有 reindex 端点（用户点「重试」即重新入队）。

    查无此行 = 建行后、领 job 前文档被删除（delete 面删文件+行），静默返回
    是正确语义——不为已删文档补索引，也不许它炸掉 worker 的 job 循环。
    """
    doc_id = uuid.UUID(document_id)
    async with AsyncSessionLocal() as session:
        document = await session.get(Document, doc_id)
        if document is None:
            logger.info("index job 查无此行（可能已删除），跳过: %s", document_id)
            return
        await document_service._index_document(session, document=document)
