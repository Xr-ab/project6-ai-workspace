"""引用来源（Phase 2 RAG）：把检索结果变成"模型看得懂的上下文"和"前端能显示的引用"。

为什么和 retriever 分成两个模块：
    retriever 回答"找到了什么"，citation 回答"怎么呈现"。两者变化的理由不同 ——
    换检索策略（改 top_k / 混合检索）不动这里，改引用格式（加高亮、加相似度条）
    不动检索。另外 Phase 3 的 RAG Tool 会复用 retriever，但不需要这套 prompt 组装。

为什么引用编号要在拼上下文的时候就定下来：
    模型回答里写的 [1] 必须和前端展示的第 1 条来源严格对应。
    这个编号就是"上下文里的资料序号"，两边共用同一个来源（本函数的循环下标），
    才不会出现"模型说 [1]、前端显示第 3 条"这种对不上的情况。
"""
import uuid
from dataclasses import dataclass

from app.ai.rag.retriever import Hit


@dataclass
class Citation:
    """一条引用来源（返回给前端、并随消息落库）。"""

    index: int  # 资料编号，从 1 开始，对应模型回答里的 [n]
    document_id: uuid.UUID
    filename: str
    page: int | None
    chunk_index: int
    score: float

    def to_payload(self) -> dict:
        """转成可直接 json.dumps 的 dict。

        document_id 必须转 str：UUID 对象 json.dumps 不认识会直接抛 TypeError，
        而这个 dict 是要塞进 SSE 帧推给前端的。
        """
        return {
            "index": self.index,
            "document_id": str(self.document_id),
            "filename": self.filename,
            "page": self.page,
            "chunk_index": self.chunk_index,
            "score": self.score,
        }


def build_context(hits: list[Hit]) -> tuple[str, list[Citation]]:
    """命中片段 → (给模型看的资料块文本, 给前端的引用列表)。

    返回两个东西，是因为它们是同一份数据的两种投影：
    模型需要"带编号的正文"，前端需要"编号 + 来源 + 分数"。
    分成两次遍历各自组装，比先拼文本再回头解析编号可靠得多。

    没有任何命中时返回 ("", [])，调用方据此降级成普通对话
    （而不是把空资料块发给模型 —— 那会让模型对着空资料编答案）。
    """
    if not hits:
        return "", []

    blocks: list[str] = []
    citations: list[Citation] = []
    for i, hit in enumerate(hits, start=1):
        # 页码可能为 None（txt / csv 这类没有页概念的格式），有才显示
        location = f"（第 {hit.page} 页）" if hit.page is not None else ""
        blocks.append(f"【资料 {i}】来源：{hit.filename}{location}\n{hit.content}")
        citations.append(
            Citation(
                index=i,
                document_id=hit.document_id,
                filename=hit.filename,
                page=hit.page,
                chunk_index=hit.chunk_index,
                # 分数保留 4 位小数：它只是给前端做"相关度"提示，
                # 原始浮点尾数没有意义，落库还占地方
                score=round(hit.score, 4),
            )
        )

    # 资料块之间空一行：模型对"块边界"的识别主要靠空行，
    # 挤在一起会把两份资料的正文连成一段，引用编号也就跟着串了
    return "\n\n".join(blocks), citations
