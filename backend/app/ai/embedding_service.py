"""Embedding 统一入口（Phase 2 RAG）。

为什么需要这一层（和 LLMService 同样的理由）：
    文件入库要批量 embed、检索要单条 embed query，Phase 3 的 RAG Tool、
    Phase 5 的 Agent 节点也都要用。各处自己 new TextEmbedding 的话，
    将来换模型 / 改维度 / 加缓存就要改 N 处。收敛成单一入口后只改本文件。

和 LLMService 的两个关键差异：
    1. fastembed 是 CPU 同步推理（没有 async 版本）。直接在 async 路由里调用会
       阻塞事件循环，把整个服务的并发拖死 —— 本层用 asyncio.to_thread 丢进线程池，
       对外仍暴露 async 方法。这样调用方不需要知道底层阻不阻塞、也不用自己记得包线程池。
       为什么用标准库 asyncio.to_thread 而不是 starlette 的 run_in_threadpool：
       ai 层不该依赖 web 框架，标准库够用。
    2. query 和入库文本必须走两个方法：bge 系列要求 query 侧加指令前缀、doc 侧不加。
       这是模型层面的要求，不是代码风格问题。
"""
import asyncio

from fastembed import TextEmbedding

from app.core.config import settings

# bge 系列的检索指令前缀：官方要求 query 侧加、入库 chunk 不加。
# 作用是让"问句"和"陈述句 chunk"在向量空间里对齐，检索更稳。
_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class EmbeddingService:
    """文本 → 向量 的唯一入口。"""

    def __init__(self) -> None:
        # 懒加载：TextEmbedding 初始化要加载 ONNX 模型（秒级）。
        # 不在 __init__ 里直接建，是为了让 uvicorn --reload 每次重启、
        # 以及纯 Chat 场景（根本不用 embedding）都不付这个代价。
        self._model: TextEmbedding | None = None

    def _get_model(self) -> TextEmbedding:
        if self._model is None:
            self._model = TextEmbedding(model_name=settings.embedding_model)
        return self._model

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量：把一批 chunk 转成向量（入库用）。

        返回 list[list[float]] 而不是 numpy 数组：
        向量最终要写进 pgvector，asyncpg 只认 Python 原生类型，
        np.float32 会在入库时直接报类型错。tolist() 一次转干净。
        """
        if not texts:
            return []
        model = self._get_model()
        vectors = await asyncio.to_thread(lambda: list(model.embed(texts)))
        return [v.tolist() for v in vectors]

    async def embed_query(self, query: str) -> list[float]:
        """单条：把用户查询转成向量（检索用）。"""
        model = self._get_model()
        text = _QUERY_INSTRUCTION + query
        vector = await asyncio.to_thread(lambda: next(iter(model.embed([text]))))
        return vector.tolist()


# 模块级单例：其他模块 from app.ai.embedding_service import embedding_service
embedding_service = EmbeddingService()
