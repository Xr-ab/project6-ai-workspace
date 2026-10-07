"""Demo 1（spec §6.4）：上传企业报告 → 就绪 → RAG 问答 → 引用来源。

一条钱的注意：这条主线花 **2 次**远程 chat 补全——RAG 问答一次，关掉 RAG 的对照请求一次。
`use_knowledge` 只闸检索那一半（`app/api/chat.py:245` 的 `if req.use_knowledge:`），模型流
照跑不误（`app/api/chat.py:114-117`：关工具就是 `llm_service.astream`），所以「RAG 关了就
不碰模型」这个说法是错的，那条对照请求同样出网。上传索引走本地 ONNX embedding，
不花钱但花时间。

钱计数的日志面（R53，实测得出，docs/13 要按这个口径写）：本条的补全记在 **app.log**，
不在 worker.log。chat 是 API 进程里直接流的（`app/api/chat.py` 的 `_sse_generator`），
不经 arq 队列；只有 Demo 2/3 那种后台任务的补全才落在 worker.log。
第一次跑撞红时两侧读数是 worker.log `chat/completions`=0、app.log=1，正是这个差别。
从日志记到的次数以实测为准：模型重试会把数顶上去，那不是失败，按测到的数记账。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from demo.demo_common import (  # noqa: E402
    citation_document_ids, cost_face, env_required, first_frame_is_citations,
    make_client, poll, require, text_blob, write_receipt,
)

FIXTURE = BACKEND_ROOT / "tests/fixtures/demo/company_report.md"
MARKER = "SECTION-ALPHA-7731"
READY_STATUSES = ("ready", "failed")

# 提问必须是英文（ASCII），这不是口味问题而是测出来的：夹具按 spec §6.3 定死纯 ASCII
# （GBK 控制台上非 ASCII 会二次损坏），而中文提问对英文语料的余弦只有 0.4403
# （本地 bge-small-zh-v1.5 实测，见 scratch/_p11c_t7_query_probe/），低于
# retriever.DEFAULT_MIN_SCORE=0.45，且全文腿（plainto 是 lexeme 全 AND）救不了
# ⇒ 检索返回空 ⇒ 首帧没有 citations，本条判红。Task 7 第一次真跑就撞在这上面。
# test_demo_scripts_static.py 有一道针把这个约束钉在常量上。
RAG_QUESTION = f"What does {MARKER} tell us about online revenue growth?"


def upload_and_wait(client, path: Path) -> dict:
    with path.open("rb") as fh:
        r = client.post_form("/documents", data={},
                             files={"file": (path.name, fh, "text/markdown")})
    require(r.status_code == 202, f"上传应 202，实得 {r.status_code}：{r.text[:200]}")
    doc = r.json()
    final = poll(lambda: client.get(f"/documents/{doc['id']}").json(),
                 lambda d: d["status"] in READY_STATUSES,
                 timeout_s=300.0, interval_s=3.0)
    require(final["status"] == "ready",
            f"索引没到 ready：status={final['status']} error={final.get('error_message')}")
    require(final["chunk_count"] > 0, "chunk_count=0 的 ready 是假就绪，检索一定空")
    return final


def run(client) -> dict:
    doc = upload_and_wait(client, FIXTURE)

    conv = client.post("/conversations", {"title": "Demo 1 会话"})
    require(conv.status_code == 201, f"建会话应 201，实得 {conv.status_code}")
    conv_id = conv.json()["id"]

    frames = client.stream_chat({
        "conversation_id": conv_id,
        "message": RAG_QUESTION,
        "use_knowledge": True,
        "use_tools": False,
    })
    require(bool(frames) and frames[-1] == {"done": True}, "SSE 必须以 [DONE] 收尾")
    require(first_frame_is_citations(frames), "首帧必须是非空 citations——这是 RAG 真命中的机械证据")
    require(citation_document_ids(frames) == {doc["id"]},
            f"引用来源必须正好是这次上传的文档，实得 {sorted(citation_document_ids(frames))}")
    blob = text_blob(frames)
    require(len(blob) > 40, f"回答正文只有 {len(blob)} 字，流式链路大概没真跑")
    text_frames = [f for f in frames if "text" in f]
    require(len(text_frames) > 1, "正文应当是多帧流式，而不是单帧一次给完")

    ctrl = client.stream_chat({
        "conversation_id": conv_id, "message": "用一句话说：今天适合做什么？",
        "use_knowledge": False, "use_tools": False,
    })
    require(not citation_document_ids(ctrl), "关掉 RAG 还出引用 = 引用来源不可信")

    first = frames[0]
    return {
        "结论": "上传→索引→RAG 检索→流式回答→引用来源 全链跑穿",
        "机械断言清单": [
            "上传 202", "索引到 ready 且 chunk_count>0", "建会话 201",
            "首帧是非空 citations", f"引用 document_id 集合 == {{{doc['id']}}}",
            "正文多帧且长度>40", "对照请求零 citations",
        ],
        "钱门读数": cost_face(client),
        "RAG 提问": RAG_QUESTION,
        "document_id": doc["id"],
        "chunk_count": doc["chunk_count"],
        "conversation_id": conv_id,
        "citation_shape": {k: first["citations"][0].get(k) for k in
                           ("index", "document_id", "filename", "page", "chunk_index")},
        "citation_count": len(first["citations"]),
        "回答字数": len(blob),
    }


def main(argv: list[str]) -> int:
    client, _origin = make_client()
    try:
        payload = run(client)
    finally:
        client.close()
    path = write_receipt(env_required("P6_ART_DIR"), "demo1", payload)
    print(f"DEMO1_PASS receipt={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
