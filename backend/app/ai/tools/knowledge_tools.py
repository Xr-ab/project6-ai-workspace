"""知识库工具（Phase 3）：让模型自己决定"要不要查知识库、查什么"。

和 Phase 2 的关系：
    Phase 2 的知识库是**开关式**的 —— 前端勾上 use_knowledge，后端先检索再拼 prompt，
    模型全程不知道有"检索"这回事，它只是收到了一段资料而已。
    这里的工具是**模型自主式**的：检索由模型发起、参数由模型给、
    查得不满意还能换个说法再查一次。同一个 retriever 能力，两种用法。

为什么两个都要有：
    开关式适合"用户明确知道要查库"的场景（少一次模型往返，更快更便宜）；
    自主式适合用户不知道该怎么查、或需要多轮查的场景。
"""
import uuid

from pydantic import BaseModel, Field

from app.ai.rag.retriever import Hit, retrieve
from app.ai.tools.base import Tool, ToolContext, ToolResult
from app.ai.tools.registry import register
from app.data.repositories import document_repo

# 单个片段回填给模型的字符上限。
# 知识库的 chunk 本身约 400 字，正常不会触发；这里防的是"某个 chunk 特别大"
# （比如 CSV 整表被切成长块）把 prompt 预算吃掉。
CHUNK_TEXT_LIMIT = 600

# 一次调用最多回填给模型的总字符数。超了就丢后面的命中并标 truncated ——
# 与其让模型收到 20 条却看不出重点，不如给前几条最相关的。
TOTAL_TEXT_BUDGET = 3000


class RagSearchArgs(BaseModel):
    """rag_search 参数。

    Field 的 description 会原样进 JSON Schema 给模型看，
    所以写的是"给模型的说明"而不是"给人看的注释"。
    """

    query: str = Field(
        description=(
            "检索用的查询语句。用自然语言描述要找什么信息，"
            "例如'请假需要提前几天申请'，不要只给关键词堆砌。"
        )
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="返回多少条片段，默认 5。问题宽泛时可以调大，精确查某一件事时用默认值即可。",
    )


def _hits_to_data(hits: list[Hit]) -> tuple[list[dict], bool]:
    """把命中列表转成回填给模型的数据，并按总预算截断。

    返回 (数据, 是否截断过)。
    """
    data: list[dict] = []
    used = 0
    for hit in hits:
        text = hit.content[:CHUNK_TEXT_LIMIT]
        if used + len(text) > TOTAL_TEXT_BUDGET:
            return data, True
        used += len(text)
        data.append(
            {
                "document_id": str(hit.document_id),
                "filename": hit.filename,
                "page": hit.page,
                "score": round(hit.score, 3),
                "text": text,
            }
        )
    return data, False


async def rag_search(args: RagSearchArgs, ctx: ToolContext) -> ToolResult:
    """企业知识库的语义检索。

    注意这里**没有** min_score 参数：阈值是防噪声的工程参数，
    该由我们定、不该让模型调（模型没有依据判断"0.4 和 0.5 哪个合适"，
    它只会把阈值当成一个可以试的旋钮，然后收到一堆无关内容还以为是资料少）。
    """
    # 包一层 SAVEPOINT：检索用的是请求共享的 ctx.session，一旦底层查询报错，
    # PostgreSQL 会把整个事务标成 aborted，同请求后面的写库（assistant 消息、
    # tool_calls、agent_runs）会全被 "current transaction is aborted" 带崩。
    # begin_nested 出错时只回滚到这个保存点，外层事务保住。
    async with ctx.session.begin_nested():
        hits = await retrieve(
            ctx.session,
            organization_id=ctx.organization_id,
            query=args.query,
            top_k=args.top_k,
        )
    # 一条都没命中不算失败：知识库里确实没这份资料，是正常业务结果。
    # 返回 ok=False 会让模型以为"工具坏了，重试一下"，然后反复调用。
    data, truncated = _hits_to_data(hits)
    return ToolResult(ok=True, data=data, rows=len(data), truncated=truncated)


class DocumentRetrieverArgs(BaseModel):
    document_id: str = Field(description="文档 id（UUID），通常来自 rag_search 返回结果里的 document_id")
    offset: int = Field(default=0, ge=0, description="从第几个片段开始取，默认 0")
    limit: int = Field(default=10, ge=1, le=30, description="取多少个连续片段，默认 10")


async def document_retriever(
    args: DocumentRetrieverArgs, ctx: ToolContext
) -> ToolResult:
    """按文档 id 顺序取原文片段（翻上下文用）。

    为什么 document_id 是 str 而不是 uuid.UUID：
        参数是模型填的，它可能给出一个不是 UUID 的字符串。
        若在这里声明成 uuid.UUID，Pydantic 校验失败会返回一大段
        校验错误，模型看不懂"哪个参数该长什么样"。声明成 str、
        由本函数给出人话错误，模型才知道该怎么改。
    """
    try:
        document_id = uuid.UUID(args.document_id)
    except ValueError:
        return ToolResult(
            ok=False,
            error=f"document_id 不是合法的 UUID：{args.document_id}。",
        )

    # SAVEPOINT 同上：查询炸了不能把共享事务拖进 aborted 状态
    async with ctx.session.begin_nested():
        chunks = await document_repo.list_chunks_by_document(
            ctx.session,
            document_id=document_id,
            organization_id=ctx.organization_id,
            offset=args.offset,
            limit=args.limit,
        )
    if not chunks:
        return ToolResult(
            ok=False,
            error=(
                f"文档 {args.document_id} 不存在、或不属于当前组织、"
                f"或 offset={args.offset} 已超出该文档的片段范围。"
            ),
        )

    data = [
        {
            "chunk_index": c.chunk_index,
            "page": (c.chunk_metadata or {}).get("page"),
            "text": c.content[:CHUNK_TEXT_LIMIT],
        }
        for c in chunks
    ]
    return ToolResult(ok=True, data=data, rows=len(data))


register(
    Tool(
        name="rag_search",
        description=(
            "在企业内部知识库（已上传的制度、手册、报告等文档）里做语义检索。\n"
            "何时用：问题涉及公司内部规定、流程、制度、内部资料。\n"
            "何时不用：闲聊、通用知识问答、需要算数或查数据库表 —— 那些用别的工具。"
        ),
        args_schema=RagSearchArgs,
        executor=rag_search,
        tool_type="knowledge",
    )
)

register(
    Tool(
        name="document_retriever",
        description=(
            "按文档 id 顺序取回原文片段，用于查看某份文档的上下文。\n"
            "何时用：rag_search 返回的片段看着像答案但不完整，需要看看前后文；"
            "或用户明确要求'看看某份文档第几段'。\n"
            "何时不用：只是想找相关信息 —— 直接用 rag_search，它按相似度排序。"
        ),
        args_schema=DocumentRetrieverArgs,
        executor=document_retriever,
        tool_type="knowledge",
    )
)