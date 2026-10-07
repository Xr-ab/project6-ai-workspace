"""检索（Phase 2 RAG 的出口）：把"用户提问"变成"带来源的片段列表"。

在整条 RAG 链路里的位置：
    入库：文件 →[parser]→ 文本 →[chunker]→ 片段 →[embedding]→ 向量 → 库
    检索：提问 →[Hybrid：向量召回 ∪ 全文召回 → RRF 融合]→ top-k 片段 →[拼 prompt]→ LLM

为什么检索要单独成层，而不是直接写在 chat_service 里：
    Phase 3 的 RAG Tool、Phase 5 的 Agent 节点都要做同一件事。写死在 chat 里
    到时候要复制三份；而且 top_k / 阈值这些参数是会被反复调的，集中一处才好调。

为什么这里显式接收 organization_id，而不是内部自取身份：
    ai 层不该知道"当前用户是谁"——那是 web 层的上下文。显式传参还有个安全收益：
    多租户过滤条件没法被"忘记加"，漏传会直接报参数缺失，而不是静默查到别人的数据。

Hybrid Search（docs/05-database-design.md §6.2）为什么加全文这条腿：
    纯向量召回会把"语义相近"排在前面，但企业知识库里最要命的查询常常是
    **精确字面**：制度编号、错误码、"年假 3 天还是 5 天"里的数字。这些词向量
    可能糊成一片，planning 却一问一个准。向量捞"意思像的"、全文捞"字面有的"，
    RRF 只按**名次**融合（不碰两边量纲不同的 score），谁在各自列表里排得靠前
    谁靠前——省去了给两路分数标定权重的坑。
"""
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embedding_service import embedding_service
from app.data.repositories import document_repo

# 默认返回条数（最终 Top N）。为什么是 5：够覆盖答案所在片段 + 一点上下文，
# 又不至于把 prompt 预算浪费在无关内容上（每个片段约 400 字）。
DEFAULT_TOP_K = 5

# 相似度下限。低于它的片段视为噪声，宁可不给也不要污染 prompt。
#
# 实测（bge-small-zh-v1.5，3 份制度文档）：
#   命中主题时 top-1          0.65 ~ 0.69
#   跨主题的弱相关            0.47 ~ 0.58
#   完全无关（"帮我写段 Python 代码"）  0.36  ← 噪声也能到 0.36
# 这个模型的分值整体偏高，0.35 拦不住噪声，所以取 0.45。
#
# 阈值调高的风险是漏掉答案，调低的风险是让模型看到无关内容后跑偏 ——
# 后者更隐蔽（回答看起来通顺，但依据是错的）。
# 注意：单一固定阈值本身就脆弱，换模型 / 换语料都要重新实测，
# 别照抄别人的数字（这组数是自己跑出来的）。
# 用法注（2026-10-03 修复波）：阈值按调用场景可豁免——单文档总结（doc_summary 图）传
# min_score=0.0，因为检索范围已收窄到用户钦点的那一份、不存在跨文档噪声；全库问答类
# 调用保持本默认值（聊天面跨语言 / 弱重叠问句仍可能空手，属已登记限制，见 README）。
DEFAULT_MIN_SCORE = 0.45

# ── Hybrid 融合参数（docs/05 §6.2：v1 固定，"融合权重与 top_k1/top_k2 可调"留后续）──
# 向量召回宽度：先多捞，把候选池做大，再由 RRF + Top N 收窄。比最终 5 条宽即可。
VECTOR_RECALL = 20
# 全文召回宽度：精确匹配本就少，10 条足够覆盖编号/术语类命中。
TEXT_RECALL = 10
# RRF 平滑常数，业界通用 60。它的作用是压住第 1 名的权重、让名次差更平缓，
# 具体取值几乎不影响结果顺序（60 与 100 在几十条候选里排序一致），照抄即可。
RRF_K = 60


@dataclass
class Hit:
    """一个检索命中片段 —— 也是 citation（引用来源）的最小单位。

    为什么带上 filename / page：Phase 2 出口要求"回答带引用来源"。
    这些信息在入库时就一路保留下来（parser 存页码 → chunker 透传 → metadata 落库），
    检索时顺手取出来，前端才能显示"来自《员工手册》第 2 页"。
    """

    chunk_id: int
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    content: str
    page: int | None
    # 余弦相似度，0~1，越大越相关（对外统一用这个口径）。
    # 注意：这是**片段本身与提问**的相似度，不是 RRF 融合分——
    # 融合分只用来排序，量纲小（~0.03），拿它当"相关度"给模型看会误导。
    score: float


def _fuse(
    vec_rows: list[tuple], txt_rows: list[tuple], *, min_score: float
) -> list[tuple]:
    """RRF 融合两条召回腿，返回按融合分降序的 [(chunk, filename, distance|None)]。

    纯函数（不碰数据库、不碰网络），这样名次、去重、噪声门三段逻辑能单测。
    distance 可能为 None：只从全文腿进来的命中向量侧没召回，就没有距离。

    RRF 打分：一个片段的融合分 = Σ 在每条腿里的 1/(RRF_K + 名次)。
    同一块在两条腿都靠前 → 两个高分相加，自然排到最前；只被一条腿捞到 → 单份分。
    """
    merged: dict[int, dict] = {}
    for rank, (chunk, filename, distance) in enumerate(vec_rows, start=1):
        merged[chunk.id] = {
            "chunk": chunk,
            "filename": filename,
            "distance": distance,
            "rrf": 1.0 / (RRF_K + rank),
            "in_vec": True,
            "in_txt": False,
        }
    for rank, (chunk, filename, _ts_rank) in enumerate(txt_rows, start=1):
        entry = merged.get(chunk.id)
        if entry is None:
            # 全文独有命中：向量没捞到它，所以先没有距离（后面按需回查补上）
            entry = {
                "chunk": chunk,
                "filename": filename,
                "distance": None,
                "rrf": 0.0,
                "in_vec": False,
                "in_txt": True,
            }
            merged[chunk.id] = entry
        else:
            entry["in_txt"] = True
        entry["rrf"] += 1.0 / (RRF_K + rank)

    ordered = sorted(merged.values(), key=lambda e: e["rrf"], reverse=True)

    kept: list[tuple] = []
    for e in ordered:
        # 噪声门只拦"向量勉强捞到、但全文也没命中"的弱相似块。
        # 全文命中是**精确字面**匹配（plainto 要求查询词全部出现），
        # 正是混合检索要补进来的那类召回；对它套向量相似度阈值 = 白加这条腿。
        if e["in_vec"] and not e["in_txt"] and (1.0 - e["distance"]) < min_score:
            continue
        kept.append((e["chunk"], e["filename"], e["distance"]))
    return kept


async def retrieve(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    document_ids: list[uuid.UUID] | None = None,
) -> list[Hit]:
    """提问 → Hybrid 召回 → RRF 融合 → 按相关度排序的命中列表。

    document_ids 就是 roadmap 里说的 Metadata Filter：把检索范围限定到
    指定文档（例如前端勾选"只在员工手册里查"）。不传 = 全组织知识库。
    """
    # query 走 embed_query 而不是 embed_texts：bge 系列要求 query 侧加指令前缀、
    # 入库侧不加。这里用错不会报错，只是检索质量悄悄下降。
    query_vector = await embedding_service.embed_query(query)

    # 两条腿各捞各的，都用同一套 org / ready / document_ids 过滤（见 repo 层）
    vec_rows = await document_repo.search_chunks(
        session,
        organization_id=organization_id,
        query_vector=query_vector,
        top_k=VECTOR_RECALL,
        document_ids=document_ids,
    )
    txt_rows = await document_repo.search_chunks_text(
        session,
        organization_id=organization_id,
        query=query,
        top_k=TEXT_RECALL,
        document_ids=document_ids,
    )

    fused = _fuse(vec_rows, txt_rows, min_score=min_score)[:top_k]

    # 只从全文腿进来的命中没有距离，回查一次真实余弦补上（主键 IN，很便宜）。
    # 补不到（并发下刚被删）就丢弃这块——没有可信分数就不返回，别编一个。
    missing_ids = [chunk.id for chunk, _f, dist in fused if dist is None]
    dist_map: dict[int, float] = {}
    if missing_ids:
        dist_map = await document_repo.cosine_distances(
            session, chunk_ids=missing_ids, query_vector=query_vector
        )

    hits: list[Hit] = []
    for chunk, filename, distance in fused:
        if distance is None:
            distance = dist_map.get(chunk.id)
        if distance is None:
            continue
        hits.append(
            Hit(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                filename=filename,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                page=(chunk.chunk_metadata or {}).get("page"),
                # distance = 1 - 相似度，方向相反。这里统一转成 score（相似度，越大越相关），
                # 对外只有一种口径，免得调用方同时记两套方向相反的数、迟早搞反。
                score=1.0 - distance,
            )
        )
    return hits
