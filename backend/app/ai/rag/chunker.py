"""文档切分（Phase 2 RAG）：把长文本切成可检索的片段。

为什么必须切（三个硬约束，缺一不可）：
    1. embedding 有长度上限（bge-small-zh-v1.5 = 512 token）。整篇塞进去，
       超出部分被**静默截断** —— 不报错，但后半篇的语义根本没进向量。
    2. 语义被平均掉：5000 字压成 1 个向量，它表示的是"整篇的平均意思"。
       问"请假流程"时它和"整篇"的相似度并不高，因为整篇里还有报销、考勤、IT 制度。
    3. 检索结果最终要拼进 prompt 喂给模型，返回整篇既超 token 预算又不聚焦。

核心矛盾（决定了参数怎么选）：
    切得越碎 → 检索越准，但单片段越缺上下文；
    切得越大 → 上下文越完整，但检索越模糊。
    所以不是"越小越好"，而是找平衡点。

为什么要有 overlap（重叠）：
    切点必然落在某处，可能正好切断一句话。重叠保证被切断的内容
    至少在某一个 chunk 里是完整的。

为什么优先按分隔符切：
    段落 / 换行 / 句末标点 是自然语言的语义边界。按边界切，每个 chunk 语义自洽；
    固定长度硬切会把"标题"和"正文"、"上句"和"下句"强行拆开。

为什么按页分别切、而不是先拼成全文再切：
    ParsedPage 里的页码是引用来源的依据。先拼全文再切，就再也说不清
    某个 chunk 来自第几页了（Step 5 保留页码的努力会白费）。
"""
from dataclasses import dataclass

from app.ai.rag.parser import ParsedPage

# 默认切分参数。
# 为什么是 400：bge-small-zh-v1.5 上限 512 token，中文里 1 个字大致 1 个 token，
#   留出余量取 400 字符，避免标点 / 英文单词分词后超限被静默截断。
DEFAULT_CHUNK_SIZE = 400

# 重叠约 15%：够覆盖被切断的一句话，又不至于让相邻片段大面积重复
# （重复太多会让检索结果里出现好几条几乎一样的内容，浪费 prompt 预算）。
DEFAULT_OVERLAP = 60

# 候选边界，按"语义强度"从高到低排列。切分时取窗口内**最靠后**的那个，
# 位置越靠后浪费的字符越少，同时天然优先选到更粗的语义边界。
_BOUNDARIES = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ")

# 边界位置至少要超过窗口的一半，否则宁可硬切：
# 防止在段落开头就断掉，产出大量十几字的碎片片段（碎片检索不出东西）。
_MIN_FILL_RATIO = 0.5


@dataclass
class Chunk:
    """一个待入库的片段。index 是文档内序号，page 用于引用来源。"""

    text: str
    index: int
    page: int | None = None


def chunk_pages(
    pages: list[ParsedPage],
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """把解析结果切成片段列表，index 在整篇文档内连续编号。

    调用方拿到的 index 直接对应 document_chunks.chunk_index，
    所以这里必须保证"整篇连续"而不是"每页从 0 开始"。
    """
    if overlap >= size:
        # 重叠大于等于窗口时，下一段的起点会落到本段起点之前，可能原地打转
        raise ValueError(f"overlap({overlap}) 必须小于 size({size})")

    chunks: list[Chunk] = []
    for page in pages:
        for text in _split_text(page.text, size, overlap):
            chunks.append(Chunk(text=text, index=len(chunks), page=page.page))
    return chunks


def _split_text(text: str, size: int, overlap: int) -> list[str]:
    """单页文本切分：贪心向后取 size 个字符，再回退到最近的语义边界。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    parts: list[str] = []
    start = 0
    total = len(text)

    while start < total:
        end = min(start + size, total)
        if end < total:
            boundary = _find_boundary(text, start, end)
            if boundary > start:
                end = boundary

        piece = text[start:end].strip()
        if piece:
            parts.append(piece)

        if end >= total:
            break
        # 回退 overlap 个字符，让下一段和本段有重叠。
        # max(..., start + 1) 兜底：保证起点一定前进，否则会死循环。
        start = max(end - overlap, start + 1)

    return parts


def _find_boundary(text: str, start: int, end: int) -> int:
    """在 text[start:end] 内找最靠后的语义边界，返回该边界之后的位置。

    找不到（或位置太靠前）时返回 start，表示"没有可用边界，交给调用方硬切"。
    """
    window = text[start:end]
    best = -1
    for sep in _BOUNDARIES:
        pos = window.rfind(sep)
        if pos >= 0:
            # 切在分隔符之后：分隔符本身留在上一段，读起来更自然
            best = max(best, pos + len(sep))

    if best < len(window) * _MIN_FILL_RATIO:
        return start
    return start + best
