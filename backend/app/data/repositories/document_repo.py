"""文档 / 分块的数据访问（Phase 2 RAG）。

Repository 层约定（见 docs/02-architecture.md §4）：
    只负责读写数据，不做业务判断、不抛业务异常。
    查不到返回 None / 空列表，怎么处理交给 Service 层。
    事务边界在本层：每次写操作自己 commit。

数据隔离（见 docs/05-database-design.md §1.4）：
    documents 按 organization_id + user_id 过滤；
    document_chunks 只带 organization_id —— chunk 是"组织级知识"，
    不按个人隔离（同一组织的人共享知识库）。
"""
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import Document, DocumentChunk


# ---------------- 文档 ----------------


async def create_document(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    filename: str,
    file_path: str,
    file_type: str,
    size_bytes: int,
    checksum: str | None = None,
) -> Document:
    """新建文档记录，初始状态由数据库默认值给 uploaded。"""
    document = Document(
        organization_id=organization_id,
        user_id=user_id,
        filename=filename,
        file_path=file_path,
        file_type=file_type,
        size_bytes=size_bytes,
        checksum=checksum,
    )
    session.add(document)
    await session.commit()
    await session.refresh(document)
    return document


async def get_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> Document | None:
    """按 id 取文档；不存在或不在可见范围时返回 None。

    user_id（8b T10 R-T5b）：给了就在 org 闸之上再叠 user 闸（member 只见自己的）；
    None = 本组织全量 —— admin 分档和**执行面工具**（chat/rag 按 id 翻他人文档的
    chunk，见 docs/05 §1.4「chunk 组织级共享」）走的都是这条默认路。
    """
    stmt = select(Document).where(
        Document.id == document_id,
        Document.organization_id == organization_id,
    )
    if user_id is not None:
        stmt = stmt.where(Document.user_id == user_id)
    return await session.scalar(stmt)


async def get_document_by_checksum(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    checksum: str,
) -> Document | None:
    """按内容指纹查已入库的文档（上传去重用）。

    按 organization_id 而不是 user_id 过滤：知识库是**组织级**的
    （见本文件顶部说明），同事传过的文件你这边也算已存在。

    order_by + limit 是必要的：修复去重之前可能已经存进了重复记录，
    不加排序的话取到哪一条不确定，报错信息里的文件名会随机变。
    """
    stmt = (
        select(Document)
        .where(
            Document.organization_id == organization_id,
            Document.checksum == checksum,
        )
        .order_by(Document.created_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def update_document_file(
    session: AsyncSession,
    *,
    document: Document,
    filename: str,
    file_path: str,
    file_type: str,
    size_bytes: int,
    checksum: str,
) -> Document:
    """替换文档对应的文件（更新用）。

    只改"文件相关的字段"，**不碰 status** —— 状态由调用方随后重跑索引时
    自己流转（parsing → … → ready）。在这里顺手置 uploaded 会让
    "文件已换但还没索引"和"刚上传"两种状态混在一起，看不出区别。
    """
    document.filename = filename
    document.file_path = file_path
    document.file_type = file_type
    document.size_bytes = size_bytes
    document.checksum = checksum
    await session.commit()
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

    user_id 语义同 get_document：None = org 全量（admin 分档 / 执行面），
    给了 = 只列该用户的。
    """
    stmt = (
        select(Document)
        .where(Document.organization_id == organization_id)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if user_id is not None:
        stmt = stmt.where(Document.user_id == user_id)
    return list(await session.scalars(stmt))


async def set_status(
    session: AsyncSession,
    *,
    document: Document,
    status: str,
    error_message: str | None = None,
    chunk_count: int | None = None,
) -> None:
    """更新处理状态，立即 commit。

    为什么状态要单独 commit、而不是攒到最后一起提交：
        状态机是用来"暴露过程"的。如果整条链路共用一个事务，
        中途失败会整体回滚，status 又变回 uploaded，前端永远看不到
        "解析失败"这个事实。所以每步状态都要独立落库。
    """
    document.status = status
    if error_message is not None:
        document.error_message = error_message
    if chunk_count is not None:
        document.chunk_count = chunk_count
    await session.commit()
    # 这里不用 refresh：db.py 里 AsyncSessionLocal 配了 expire_on_commit=False，
    # commit 后对象属性依然可读（默认的 True 才会把对象标记过期、需要回库重查）。


async def delete_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> bool:
    """删除文档（分块由外键 ON DELETE CASCADE 一起删）。返回是否真删到。

    user_id 给了就在 DELETE 的 WHERE 里再叠一维 —— 读闸（service 层 get_document）
    之外写闸也自守一道，不依赖"调用方先查过"的口头约定。
    """
    stmt = delete(Document).where(
        Document.id == document_id,
        Document.organization_id == organization_id,
    )
    if user_id is not None:
        stmt = stmt.where(Document.user_id == user_id)
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


async def count_documents(session: AsyncSession, *, organization_id: uuid.UUID) -> int:
    """文档总数（列表页分页用）。"""
    stmt = (
        select(func.count())
        .select_from(Document)
        .where(Document.organization_id == organization_id)
    )
    return await session.scalar(stmt) or 0


# ---------------- 分块 ----------------


async def add_chunks(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    rows: list[dict],
) -> int:
    """批量落分块（含向量）。

    rows 每项：chunk_index / content / token_count / embedding / metadata

    为什么一次性 add_all + 单次 commit：
        一份文档动辄几百上千个 chunk，逐个 commit 会让数据库往返次数爆炸
        （每个 chunk 一次网络往返 + 一次 WAL 刷盘）。
    """
    if not rows:
        return 0
    session.add_all(
        [
            DocumentChunk(
                document_id=document_id,
                organization_id=organization_id,
                chunk_index=row["chunk_index"],
                content=row["content"],
                token_count=row.get("token_count", 0),
                embedding=row["embedding"],
                chunk_metadata=row.get("metadata") or {},
            )
            for row in rows
        ]
    )
    await session.commit()
    return len(rows)


async def delete_chunks(session: AsyncSession, *, document_id: uuid.UUID) -> int:
    """按文档清空分块。

    重新索引前必须先清：document_chunks 上有 UNIQUE(document_id, chunk_index)，
    旧 chunk 不清掉，新一批插进来会直接撞唯一约束。
    """
    stmt = delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount


async def search_chunks(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    query_vector: list[float],
    top_k: int = 5,
    document_ids: list[uuid.UUID] | None = None,
) -> list[tuple[DocumentChunk, str, float]]:
    """余弦距离检索：返回 [(chunk, 来源文件名, distance), ...]，按 distance 升序。

    query_vector 必须和 embedding 同维度（settings.embedding_dim=512），
    维度不一致会直接报错，不会静默返回错结果。

    distance 用 `<=>`（余弦距离，范围 [0,2]）。HNSW 索引是 vector_cosine_ops，
    所以**必须**用 cosine（<=>）。用 L2 / 内积也不会报错，
    但会绕过索引退化成全表扫描 —— 数据小时无所谓，数据大了就慢。
    """
    distance_expr = DocumentChunk.embedding.cosine_distance(query_vector)
    stmt = (
        select(DocumentChunk, Document.filename, distance_expr.label("distance"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.organization_id == organization_id,
            # 只有 ready 的文档才参与检索：
            #   1. failed 的文档在 reindex 失败时旧 chunks 可能还留在库里
            #      （_index_document 在 embed 成功之后才删旧块），不过滤状态的话，
            #      会引用到"已失败文档的旧内容"，用户看到的是过时知识
            #   2. embedding 可空，failed 的中途步骤可能没向量，一并排除
            Document.status == "ready",
            DocumentChunk.embedding.isnot(None),
        )
    )
    # Metadata Filter：把检索范围限定到指定文档（例如"只在这个知识库里搜"）
    if document_ids:
        stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))
    stmt = stmt.order_by(distance_expr.asc()).limit(top_k)
    rows = await session.execute(stmt)
    return [(chunk, filename, distance) for chunk, filename, distance in rows.all()]


async def search_chunks_text(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    query: str,
    top_k: int = 10,
    document_ids: list[uuid.UUID] | None = None,
) -> list[tuple[DocumentChunk, str, float]]:
    """全文召回（Hybrid 的另一条腿）：返回 [(chunk, 来源文件名, ts_rank), ...] 降序。

    和 search_chunks 保持**同一套过滤条件**（org / ready 文档 / 可选文档范围）——
    两条召回腿的可见范围不一致的话，全文侧会把已删除/失败文档喂进融合结果。

    分词用 'simple' 配置：不做起词分析，中文按整段非数字符切并做小写归一。
    对中文它的召回不如专业分词器，但精确术语、编号、英文词（错误码、
    "P0 故障"、制度编号）这类向量容易糊掉的命中，simple 就能兜住——
    这正是加全文这条腿要解决的问题。换 zhparser 是 PG 扩展层的事，
    只影响这里和生成列的 text search config，不影响上层 RRF。

    plainto_tsquery：把用户原话按 AND 组合成查询（'请假 备案' → '请假 & 备案'）。
    不用 to_tsquery：那个要求调用方自己写 & | ! 语法，模型/用户给的是自然语言。
    """
    tsquery = func.plainto_tsquery("simple", query)
    rank = func.ts_rank(DocumentChunk.content_tsv, tsquery)
    stmt = (
        select(DocumentChunk, Document.filename, rank.label("rank"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.organization_id == organization_id,
            Document.status == "ready",  # 同 search_chunks：失败文档的残留块不参与
            DocumentChunk.content_tsv.op("@@")(tsquery),
        )
        .order_by(rank.desc())
        .limit(top_k)
    )
    if document_ids:
        stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))
    rows = await session.execute(stmt)
    return [(chunk, filename, float(rank_)) for chunk, filename, rank_ in rows.all()]


async def cosine_distances(
    session: AsyncSession,
    *,
    chunk_ids: list[int],
    query_vector: list[float],
) -> dict[int, float]:
    """给定一批 chunk id，返回 {id: 余弦距离}。

    为什么单独有这个查询：Hybrid 融合后，只从全文那条腿进来的命中
    （向量 top_k1 没捞到它）没有距离，可 citation / 工具回填都要一个真实的
    0~1 相似度。用主键 IN 一次取回，比给它们现算向量便宜得多，
    也比"塞个假分数 0.0"诚实——假分数会让模型以为那条命中毫不相关。
    """
    if not chunk_ids:
        return {}
    distance_expr = DocumentChunk.embedding.cosine_distance(query_vector)
    stmt = select(DocumentChunk.id, distance_expr.label("distance")).where(
        DocumentChunk.id.in_(chunk_ids)
    )
    rows = await session.execute(stmt)
    return {int(cid): float(dist) for cid, dist in rows.all()}


async def list_chunks_by_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    offset: int = 0,
    limit: int = 10,
) -> list[DocumentChunk]:
    """按 chunk_index 顺序取某文档的一段分块（Document Retriever 工具用）。

    和 search_chunks 的区别：
        search_chunks 是"给一个问题，找最像的片段"（按相似度）；
        本函数是"给一份文档，按顺序翻原文"（按位置）——
        模型引用了 [1] 之后想看看上下文时走这条路。

    仍带 organization_id 过滤：document_id 是模型给的参数（不可信），
    少了这一条，理论上构造别人的 document_id 就能读到别的组织的数据。
    """
    stmt = (
        select(DocumentChunk)
        .where(
            DocumentChunk.document_id == document_id,
            DocumentChunk.organization_id == organization_id,
        )
        .order_by(DocumentChunk.chunk_index)
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
