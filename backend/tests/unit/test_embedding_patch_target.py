"""假 embedder 必须打在**消费者模块的名字**上（spec §6 点名的最容易写错的一处）。

embedding_service 是模块级单例（app/ai/embedding_service.py:64），
retriever.py:27 与 document_service.py:28 都 `from ... import embedding_service`
⇒ 消费者的模块属性是一份独立绑定。打在原模块上，消费者手里的对象不会变——
桩就白打了，真 ONNX 模型会被拉起来（那是 hf-mirror 下载面，11b 明令不触发）。
"""
import app.ai.embedding_service as embedder_module
import app.ai.rag.retriever as retriever
import app.application.document_service as document_service
from app.ai.embedding_service import EmbeddingService


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def embed_query(self, query: str) -> list[float]:
        self.calls.append(("query", query))
        return [0.0] * 512

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(("texts", tuple(texts)))
        return [[0.0] * 512 for _ in texts]


def test_real_singleton_is_lazy_and_untouched_by_import():
    """import 面不许已经把 ONNX 模型建起来（否则 CI 会去下载模型）。"""
    assert isinstance(embedder_module.embedding_service, EmbeddingService)
    assert embedder_module.embedding_service._model is None


def test_both_consumers_hold_the_same_singleton_object():
    assert retriever.embedding_service is document_service.embedding_service
    assert retriever.embedding_service is embedder_module.embedding_service


async def test_patching_the_consumer_name_changes_what_the_consumer_calls(monkeypatch):
    fake = _FakeEmbedder()
    monkeypatch.setattr(document_service, "embedding_service", fake)
    await document_service.embedding_service.embed_texts(["甲"])
    assert fake.calls == [("texts", ("甲",))]


async def test_patching_the_origin_module_leaves_the_consumer_untouched(monkeypatch):
    """这枚针钉的是「打错地方」的形状：原模块属性换了，消费者手里还是老对象。"""
    fake = _FakeEmbedder()
    monkeypatch.setattr(embedder_module, "embedding_service", fake)
    assert document_service.embedding_service is not fake
    assert retriever.embedding_service is not fake
    assert fake.calls == []                                    # 真 embedder 零调用的反面
