"""知识库文档 API（Phase 2）：上传 / 列表 / 详情 / 重新索引 / 删除。

为什么上传用 multipart/form-data：
    文件是二进制，JSON 装不下（base64 能塞但体积膨胀 33%，还得手动解码）。
    FastAPI 的 UploadFile 直接接 multipart。

为什么校验规则住在 Service 层：
    规则本体（支持哪些后缀、上限多少 MB）是业务规则，放 Service 才能被别的
    入口复用（批量导入、URL 导入、reindex 换文件都是同一个 validate_intake）。
    上传面在调 create_document 之前先调一次 validate_intake，不是把规则搬到
    路由层抄一遍，而是**闸的顺序**要求它先跑：确定性拒绝（415/413）必须先于
    可用性拒绝（503 队列不可用），否则一个永远传不进去的文件会收到
    "稍后重试"，用户就对着它无限重试（agents.precheck_graph_access 同口径）。

上传 / 重新索引是 202 不是 201/200（排雷-F 后台化）：
    请求只负责落盘 + 建行 + 入队，索引链路在 worker 里跑，响应时刻文档恒为
    uploaded。前端据此轮询列表看终态（06 §2.3 补记 ① 的兑现）。
"""
import uuid

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agents import enqueue_run_job, get_arq_pool
from app.api.deps import CurrentUser
from app.application import document_service
from app.core.config import settings
from app.core.rate_limit import rate_limit_dep
from app.data.db import get_db
from app.schemas.document import DocumentOut
from app.workers.jobs import JOB_INDEX_DOCUMENT

router = APIRouter(prefix="/documents", tags=["documents"])

# 8b T9 上传面限流（spec §6 表第三行：解析/嵌入面堆积）。只闸写面（上传）——
# v1 不做读限流（spec §6 逐字 YAGNI），reindex 不在表内也不闸。
_rl_upload = rate_limit_dep("upload", lambda: settings.rate_limit_upload_per_min)


async def _enqueue_index(pool, document_id: uuid.UUID) -> None:
    """索引 job 入队：_job_id 用 docindex:{id}，upload 与 reindex 两面共用同一键。

    共用是刻意的：arq 对同 id 的在队/在飞 job 拒二次入队，跨面也因此不会
    给同一文档同时排两个重建（去重命中仍回 202 —— 行已持久化且队里必有一个
    job 会领它，enqueue_run_job 里记 info 供对账）。
    """
    await enqueue_run_job(pool, function=JOB_INDEX_DOCUMENT, run_id=document_id,
                          job_id=f"docindex:{document_id}")


def _doc_scope(user) -> uuid.UUID | None:
    """documents 读/删/重索引面的可见性分档（8b T10 R-T5b，设计文档 §9.2）。

    member → 本人 user_id（列表只见自己上传，碰他人 → 与不存在同形 404）；
    admin → None = 本组织全量（审计页 admin 先例同款分档）。
    执行面工具（chat/rag 按 id 翻他人文档的 chunk）不经这里，走 repo 默认 None，
    chunk 组织级共享的语义不变（docs/05 §1.4）。
    """
    return None if user.role == "admin" else user.id


@router.post("", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(_rl_upload)])
async def upload_document(
    user: CurrentUser,
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    """上传文档：落盘 + 建行 + 入队即返回，索引在 worker 里跑（排雷-F 后台化）。

    返回的 status 恒为 uploaded（"已收下、已排队"），终态靠轮询列表看：
        ready  = 入库成功，可以检索了
        failed = 收下了但处理失败，error_message 里有原因（可 reindex 重试）

    闸的顺序（确定性判定赢过可用性判定，同 agents.precheck_graph_access 口径）：
        1. 入站校验（415 格式 / 413 超限）—— 永远不会被接受的文件不因队列
           状态收到"稍后重试"，否则用户对着错误输入无限重试
        2. 池预检（503）—— Redis 不可达时零落盘零新行，不给库里留
           "建了行没人跑"的孤儿（agents.get_arq_pool 同一条口径）
        3. 落盘 + 建行 + 入队
    """
    content = await file.read()
    document_service.validate_intake(filename=file.filename or "", content=content)
    pool = get_arq_pool(request)
    document = await document_service.create_document(
        session, filename=file.filename or "", content=content,
        organization_id=user.organization_id, user_id=user.id,
    )
    await _enqueue_index(pool, document.id)
    return DocumentOut.model_validate(document)


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    user: CurrentUser,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> list[DocumentOut]:
    """文档列表（前端知识库页用）。member 只见自己上传（8b T10 收口）。"""
    documents = await document_service.list_documents(
        session, organization_id=user.organization_id, user_id=_doc_scope(user),
        limit=limit, offset=offset,
    )
    return [DocumentOut.model_validate(d) for d in documents]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    user: CurrentUser,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    """文档详情（轮询处理状态用）。member 碰他人文档 → 与不存在同形 404。"""
    document = await document_service.get_document(
        session, document_id=document_id, organization_id=user.organization_id,
        user_id=_doc_scope(user),
    )
    return DocumentOut.model_validate(document)


@router.post("/{document_id}/reindex", response_model=DocumentOut,
             status_code=status.HTTP_202_ACCEPTED)
async def reindex_document(
    user: CurrentUser,
    request: Request,
    document_id: uuid.UUID,
    file: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_db),
) -> DocumentOut:
    """重新索引文档：把行打回 uploaded 并入队，响应时刻不跑索引。

    两种用法（用 multipart 传不传 file 区分）：
        不传 file = 用磁盘上现有文件重跑（failed 重试 / 改了切分参数后重切）
        传 file   = 先替换文件内容再重跑（更新知识库里的文件）

    为什么不做成 PUT：PUT 语义是"整体替换资源"，而这里不传 file 时
    文件根本没变、只是重跑了一遍处理。POST 到 /reindex 这个子资源上，
    表达的是"触发一次重新索引动作"，更贴合实际行为。

    闸顺序同上传面：带了 file 就先做入站校验（415/413），再池预检（503），
    再打回 uploaded + 入队。池预检必须早于 service —— reindex_document 会
    commit 那次"打回 uploaded"，队列不可达时才走到那儿等于把一个 ready/failed
    文档静默推进成没人领的在建行。
    """
    content = await file.read() if file is not None else None
    filename = (file.filename or "") if file is not None else None
    if content is not None:
        document_service.validate_intake(filename=filename or "", content=content)
    pool = get_arq_pool(request)
    document = await document_service.reindex_document(
        session, document_id=document_id, organization_id=user.organization_id,
        user_id=_doc_scope(user), filename=filename, content=content,
    )
    await _enqueue_index(pool, document.id)
    return DocumentOut.model_validate(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    user: CurrentUser,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> None:
    """删除文档（分块一起删，磁盘文件一并清理）。member 只能删自己的。"""
    await document_service.delete_document(
        session, document_id=document_id, organization_id=user.organization_id,
        user_id=_doc_scope(user),
    )
