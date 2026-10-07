"""一次性把 ONNX 权重灌进 ${FASTEMBED_CACHE_PATH}（spec §6 R-C/R-O）。

只走 embedding_service 的唯一入口，不自己 new TextEmbedding：入口哪天改了缓存策略，
这里跟着变，不会在镜像里烤出第二条路径来。

失败**不阻断** init-data：文档索引面不是 11a 的出口（spec §7 行 1，RAG 完整演练在 11c），
所以打印原因后 exit 0。R-O 的教训是「预热报成功、卷里啥也没有」——所以 Step 里另有一枚
「卷里真有东西、/tmp 里没东西」的针，不靠这个脚本的退出码自证。
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.embedding_service import embedding_service  # noqa: E402
from app.core.config import settings  # noqa: E402


async def _main() -> None:
    t0 = time.monotonic()
    vectors = await embedding_service.embed_texts(["预热"])
    dim = len(vectors[0])
    cache = os.environ.get("FASTEMBED_CACHE_PATH", "(未设置 → 会落在容器临时层)")
    print(
        f"warm_embedding: model={settings.embedding_model} dim={dim} "
        f"cache={cache} elapsed={time.monotonic() - t0:.1f}s"
    )
    if dim != settings.embedding_dim:
        raise RuntimeError(f"维度 {dim} != settings.embedding_dim {settings.embedding_dim}")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001 — 预热失败按设计不阻断（见 docstring）
        print(f"warm_embedding: 预热未完成（{type(exc).__name__}: {exc}）")
        raise SystemExit(0)
