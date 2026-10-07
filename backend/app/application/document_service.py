"""文档入库业务逻辑（Phase 2 RAG）。

链路（docs/09-roadmap.md §5）：
    保存文件 → parse → chunk → embed → 落库 → 更新状态

status 流转：
    uploaded → parsing → chunking → embedding → ready
                                            ↘ failed（任一步出错）

为什么每步都要"先落状态、再干活"：
    状态机是用来**暴露过程**的。失败时能直接看出卡在哪一步 ——
    只记一个 failed 的话，"解析失败"还是"向量化失败"得翻日志才知道。

为什么文档解析在后台跑（排雷-F 兑现 06 §2.3 补记 ①）：
    解析/切分/向量化是秒级到分钟级的重活，摆在请求里 = 上传接口随文档大小
    线性变慢，且大文件会吃掉 worker 槽位之外的整条请求链。8b 已落 arq 底座，
    本面是第四个迁移的执行面：请求只落盘 + 建行（status=uploaded）+ 入队，
    状态机推进全在 workers/jobs.index_document 里 —— 状态机本身一行没改，
    这正是当初把状态设计成多阶段的价值。
"""
import hashlib
import uuid
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embedding_service import embedding_service
from app.ai.rag.chunker import chunk_pages
from app.ai.rag.parser import SUPPORTED_TYPES, parse
from app.core.config import settings
from app.core.exceptions import (
    DocumentDuplicateError,
    DocumentNotFoundError,
    FileTooLargeError,
    UnsupportedFileTypeError,
)
from app.data.models import Document
from app.data.repositories import document_repo


async def create_document(
    session: AsyncSession,
    *,
    filename: str,
    content: bytes,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Document:
    """接收上传内容：校验 → 落盘 → 建行（uploaded）→ 入队，立即返回。

    入口先做两项校验（格式、大小）—— 这两类问题在建记录**之前**就拒绝，
    不留下"上传失败但库里多一条垃圾记录"。
    索引阶段的失败不再影响本请求：链路已搬进 worker（jobs.index_document），
    失败时置 failed 并留原因，用户从列表看到后走 reindex 重试。
    身份从 router 的 CurrentUser 传入（8a）。
    """
    file_type = validate_intake(filename=filename, content=content)

    # 内容指纹去重。放在校验之后、落盘之前：
    #   放前面 → 不支持的格式会先被存进磁盘再拒绝，白留一个垃圾文件；
    #   放后面 → 已经落盘、建了记录才发现重复，还得回滚删文件。
    checksum = hashlib.sha256(content).hexdigest()
    existing = await document_repo.get_document_by_checksum(
        session, organization_id=organization_id, checksum=checksum
    )
    # 只放行 failed（重传是修好文件的正道），在建（uploaded/parsing/…）也判重：
    # 后台化后在建窗口从"请求内几毫秒"变成"秒级到分钟级"，不闸住就会有
    # 两份同内容文档各领一个 job —— 双双走到 add_chunks 时删旧写新交错，
    # 撞 UNIQUE(document_id, chunk_index) 或留下半套 chunk。
    if existing is not None and existing.status != "failed":
        # 同名时括号里再写一遍文件名是废话（"请假制度.md 已存在（请假制度.md）"），
        # 只在文件名不同、但内容撞车时才点出是跟哪一份重复
        if existing.filename == filename:
            message = f"{filename} 已存在，无需重复上传"
        else:
            message = f"{filename} 的内容与已入库的 {existing.filename} 相同，无需重复上传"
        raise DocumentDuplicateError(message)

    saved_path = await _save_file(filename, content)
    document = await document_repo.create_document(
        session,
        organization_id=organization_id,
        user_id=user_id,
        filename=filename,
        file_path=saved_path,
        file_type=file_type,
        size_bytes=len(content),
        checksum=checksum,
    )
    return document


async def list_documents(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Document]:
    """文档列表，最新上传的排前面。

    8b T10 R-T5b 收口：本函数的可见性分档只有两档 ——
    给了 user_id = member 只见自己上传；None = org 全量（router 只在 admin 时给 None）。
    """
    return await document_repo.list_documents(
        session, organization_id=organization_id, user_id=user_id,
        limit=limit, offset=offset,
    )


async def get_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> Document:
    """取文档详情。查不到（含不属于本组织、member 够不到他人文档）统一抛 404。

    他人文档与不存在**同形**（同 DOC_404001 同 message）：不泄漏"这份文档存在"
    这个事实本身（设计文档 §9.2 裁定，与 conversations 的 404 口径同款）。
    """
    document = await document_repo.get_document(
        session, document_id=document_id, organization_id=organization_id,
        user_id=user_id,
    )
    if document is None:
        raise DocumentNotFoundError()
    return document


async def delete_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> None:
    """删除文档：先删库记录（分块 CASCADE 一起删），再尽力删磁盘文件。

    顺序为什么是这样：
        反过来的话，文件先删掉、记录删除却失败，就会留下"记录还在但文件没了"，
        之后任何重新索引都会报文件不存在。
        现在这个顺序最坏情况只是留下一个没人引用的孤儿文件，功能不受影响。

    身份依赖（8a + 8b T10 收口）：user_id 从 router 按角色分档传入
    （member=本人 / admin=None 全 org），读闸 get_document 与写闸 repo.delete_document
    各滤一次；越权即 404，删除只发生在已验证归属的行上。
    """
    document = await get_document(
        session, document_id=document_id, organization_id=organization_id,
        user_id=user_id,
    )
    deleted = await document_repo.delete_document(
        session, document_id=document_id, organization_id=organization_id,
        user_id=user_id,
    )
    if deleted and document.file_path:
        await run_in_threadpool(_remove_file, document.file_path)


async def reindex_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    filename: str | None = None,
    content: bytes | None = None,
) -> Document:
    """重新索引：把文档打回 uploaded 并入队，索引用磁盘上现有文件（或新替换的）重跑。

    三个典型用途：
        1. failed 文档重试 —— 修好文件后重传（filename + content 都给）
        2. 调过 chunker 参数（size / overlap）后，让已有文档按新参数重新切分
        3. 更新知识库里的文件内容

    为什么不做"同名文件自动替换"：
        不同人完全可能各自上传同名的《周报.docx》，内容不同、意图也不同。
        按文件名自动覆盖会静默吞掉别人的文档，且事后查不出来。
        更新必须是**显式**动作 —— 传了 file 才替换。

    filename / content 要么都给、要么都不给：只给一个说明调用方搞错了，
    与其猜它想干什么，不如直接报错（这里用断言式的显式判断，见下）。
    """
    if (filename is None) != (content is None):
        raise ValueError("filename 和 content 必须同时提供或同时省略")

    document = await get_document(
        session, document_id=document_id, organization_id=organization_id,
        user_id=user_id,
    )

    if filename is not None and content is not None:
        # 复用上传那套校验，保证"更新"和"上传"的准入标准完全一致
        file_type = validate_intake(filename=filename, content=content)

        new_path = await _save_file(filename, content)
        old_path = document.file_path
        # 先落新文件、再改记录：反过来的话记录已指向新路径但文件还没写完，
        # 紧接着的索引会报"文件不存在"，而记录已经是脏的
        document = await document_repo.update_document_file(
            session,
            document=document,
            filename=filename,
            file_path=new_path,
            file_type=file_type,
            size_bytes=len(content),
            checksum=hashlib.sha256(content).hexdigest(),
        )
        # 删旧文件放在记录改成功之后：中途失败最坏只留个孤儿文件，
        # 不会出现"记录指向一个已经被删掉的文件"
        if old_path:
            await run_in_threadpool(_remove_file, old_path)

    # 打回 uploaded + 清旧错误，然后由 router 入队（commit-then-enqueue 的硬顺序：
    # worker 是另一个会话，看不见未提交的重置）。不清 error_message 的话，
    # 重试期间前端会同时看到「索引中」和上一次的报错，自相矛盾。
    # chunk_count 故意不清：旧 chunk 此刻还在库里（job 会先删后写），
    # 归零等于对前端谎报"这份文档没内容了"；但检索面按 status=ready 过滤
    # （document_repo:285/:326），所以排队期间这份文档暂不参与检索 —— 这是
    # 后台化换来的真实代价，宁缺不歪（旧 chunk 可能已与被替换的文件不符）。
    document.error_message = None
    await document_repo.set_status(session, document=document, status="uploaded")
    return document


async def _index_document(session: AsyncSession, *, document: Document) -> None:
    """解析 → 切分 → 向量化 → 落库。任一步失败则置 failed 并记录原因。

    调用方是 workers/jobs.index_document（后台化后唯一的执行入口）：
    本函数不往外抛，所以 job 侧无需再包 try——失败已落成 failed 行。
    """
    try:
        # 先清掉上一次的失败原因。不清的话，重索引成功后 error_message 还留着
        # 旧的报错，前端会同时看到 status=ready 和一条错误信息，自相矛盾。
        # 直接改 ORM 对象、由下面 set_status 的 commit 一起落库，不再多一次写。
        document.error_message = None

        await document_repo.set_status(session, document=document, status="parsing")
        # parse 是同步阻塞的（读盘 + 第三方解析库），必须丢线程池。
        # 直接在 async 里调用会卡住事件循环 —— 表现是"有人上传时，
        # 全服务的其他请求一起变慢"，而不是报错，很难联想到上传。
        pages = await run_in_threadpool(
            parse, Path(document.file_path), document.file_type
        )

        await document_repo.set_status(session, document=document, status="chunking")
        chunks = chunk_pages(pages)

        await document_repo.set_status(session, document=document, status="embedding")
        vectors = await embedding_service.embed_texts([c.text for c in chunks])

        # 先清旧 chunk 再写新：重新索引时不清会撞 UNIQUE(document_id, chunk_index)
        await document_repo.delete_chunks(session, document_id=document.id)
        await document_repo.add_chunks(
            session,
            document_id=document.id,
            organization_id=document.organization_id,
            rows=[
                {
                    "chunk_index": chunk.index,
                    "content": chunk.text,
                    # 中文里 1 个字大致 1 个 token，用字符数近似够用；
                    # 要精确就得上 tokenizer，等 Phase 6 做成本统计时再说
                    "token_count": len(chunk.text),
                    "embedding": vector,
                    # page 是引用来源的页码，从 parser 一路传到这里
                    "metadata": {"page": chunk.page},
                }
                for chunk, vector in zip(chunks, vectors)
            ],
        )
        await document_repo.set_status(
            session, document=document, status="ready", chunk_count=len(chunks)
        )
    except Exception as exc:
        # 故意不往外抛：文件已收下、记录已建立，把 failed + 原因落库让前端展示，
        # 比让整个上传请求 500 更有用。状态先落库，调用方拿到的 document 才是新状态。
        #
        # 先 rollback 再落 failed：异常可能源自 DB 本身（如 embedding 维度≠列宽，
        # add_chunks 报错后事务已被判 abort）。不 rollback 直接 set_status（内含 commit）
        # 会二次抛 PendingRollbackError → 文档卡在 embedding、旧 chunk 已被上一步删空、
        # 且请求 500 —— 恰好违背这里「落 failed 而非 500」的本意。
        # rollback 会让 document 过期，async 下取属性要先 refresh 重新加载。
        await session.rollback()
        await session.refresh(document)
        #
        # 消息里带原始文件名：解析层只知道磁盘路径（uuid 文件名），
        # 那对用户没有意义；用户认得的名字在 document.filename 里。
        await document_repo.set_status(
            session,
            document=document,
            status="failed",
            error_message=f"{document.filename}：{exc}",
        )


def validate_intake(*, filename: str, content: bytes) -> str:
    """入站硬校验（415 格式 / 413 超限），纯函数零副作用，返回 file_type。

    为什么 router 要在**池预检之前**单独调它一次：
        后台化后上传面多了 503「队列暂时不可用，请稍后重试」这条通道。一个
        永远不会被接受的文件（.bin / 超限）不该因为队列状态而收到"稍后重试"——
        那是让用户对错误输入无限重试。同 agents.precheck_graph_access 的口径：
        确定性判定（权限/校验）永远赢过可用性判定。
    create_document 内部复用同一份，两个入口的准入标准不会走岔。
    """
    file_type = _detect_file_type(filename)
    _check_size(content)
    return file_type


def _detect_file_type(filename: str) -> str:
    """从后缀判断文件类型；不支持的直接拒绝（415）。"""
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_TYPES:
        raise UnsupportedFileTypeError(
            f"不支持的文件类型：{suffix or '（无扩展名）'}；"
            f"支持 {', '.join(sorted(SUPPORTED_TYPES))}"
        )
    return suffix


def _check_size(content: bytes) -> None:
    """入口拦超限文件（413）。"""
    limit = settings.max_upload_mb * 1024 * 1024
    if len(content) > limit:
        raise FileTooLargeError(
            f"文件 {len(content) / 1024 / 1024:.1f}MB，"
            f"超过上限 {settings.max_upload_mb}MB"
        )


async def _save_file(filename: str, content: bytes) -> str:
    """落盘并返回保存路径。

    磁盘文件名为什么用 uuid 前缀、而不是原文件名：
        1. 重名：两个人传同名文件会互相覆盖，后传的把先传的顶掉
        2. 安全：原文件名可能带 ../ 或绝对路径，直接拼进目录有路径穿越风险
    原文件名只存在数据库里给用户看，磁盘上只认 uuid。
    """
    upload_dir = Path(settings.upload_dir)
    await run_in_threadpool(upload_dir.mkdir, parents=True, exist_ok=True)
    target = upload_dir / f"{uuid.uuid4().hex}{Path(filename).suffix.lower()}"
    await run_in_threadpool(target.write_bytes, content)
    return str(target)


def _remove_file(path: str) -> None:
    """尽力删磁盘文件。删不掉不算错 —— 库记录已删，功能上没影响。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
