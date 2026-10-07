"""RAG 链路（Phase 2）：parser → chunker → retriever。

按 docs/09-roadmap.md §5 的目录约定，这三块同属 RAG 链路，放一个包内：
    parser.py     文件 → list[ParsedPage]（带页码的纯文本）
    chunker.py    文本 → list[Chunk]（带页码的片段）
    retriever.py  问题 → list[Hit]（带来源的检索结果）

为什么不放 app/application/：它们不碰数据库、不碰 HTTP，是纯粹的文本处理与检索算法，
和 embedding_service 一样属于 AI 侧能力；services 层负责编排它们 + 落库。
"""
