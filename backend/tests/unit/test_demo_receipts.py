"""Demo 工具箱的判定层用例：零网络、零容器、零模型调用。

这里测的是「脚本凭什么说 Demo 过了」。spec §6.2 把脚本层与演练层分开，
脚本层的全部可信度都落在这几个函数上，所以它们必须能在没有栈的机器上被证。
"""
import json

import httpx
import pytest


# ---- classify ----


def test_classify_pass_only_when_completed_with_a_result():
    from demo.demo_common import classify

    assert classify("completed", {"summary": "x"}) == "pass"


def test_classify_catches_the_empty_result_shape():
    """completed 但 result 为空 = 假通过（doc_summary 的 deliver 返回 None 时 task 会变 rejected，
    但「completed + 空 result」是装配层能造出来的形状，必须单独判红）。"""
    from demo.demo_common import classify

    assert classify("completed", None) == "empty_result"


def test_classify_names_the_wrong_terminal_status():
    from demo.demo_common import classify

    assert classify("rejected", {"summary": "x"}) == "wrong_terminal:rejected"
    assert classify("failed", None) == "wrong_terminal:failed"


def test_require_raises_with_the_thing_that_was_expected():
    from demo.demo_common import DemoAssertion, require

    with pytest.raises(DemoAssertion) as ei:
        require(False, "citations 帧非空")
    assert "citations 帧非空" in str(ei.value)


# ---- SSE ----


def test_parse_sse_decodes_data_frames_and_the_done_sentinel():
    from demo.demo_common import parse_sse

    raw = 'data: {"citations": []}\n\ndata: {"text": "你好"}\n\ndata: {"text": "，世界"}\n\ndata: [DONE]\n\n'
    frames = parse_sse(raw)
    assert frames[0] == {"citations": []}
    assert frames[1]["text"] == "你好"
    assert frames[-1] == {"done": True}


def test_parse_sse_ignores_comments_and_blank_lines():
    from demo.demo_common import parse_sse

    frames = parse_sse(": ping\n\n\ndata: {\"text\": \"a\"}\n")
    assert frames == [{"text": "a"}]


def test_first_frame_is_citations_checks_shape_not_just_presence():
    from demo.demo_common import first_frame_is_citations

    assert first_frame_is_citations([{"citations": [{"index": 1}], "text": ""}]) is True
    assert first_frame_is_citations([{"citations": [], "text": ""}]) is False  # 空引用 = 没命中
    assert first_frame_is_citations([{"text": "hi"}]) is False


def test_text_blob_and_citation_document_ids():
    from demo.demo_common import citation_document_ids, parse_sse, text_blob

    raw = ('data: {"citations": [{"index": 1, "document_id": "d1", "filename": "a.md", '
           '"page": 2, "chunk_index": 3, "score": 0.8}]}\n\n'
           'data: {"text": "结论"}\n\ndata: {"text": "如下"}\n\n')
    frames = parse_sse(raw)
    assert text_blob(frames) == "结论如下"
    assert citation_document_ids(frames) == {"d1"}


# ---- Trace 侧的模型调用计数（钱门的数据来源） ----


def test_llm_node_count_counts_nodes_with_a_model():
    from demo.demo_common import llm_node_count

    nodes = [
        {"kind": "agent", "name": "supervisor", "model": "glm-4-flash"},
        {"kind": "tool", "name": "query_sales", "model": None},
        {"kind": "agent", "name": "reviewer", "model": "glm-4-flash"},
    ]
    assert llm_node_count(nodes) == 2


def test_within_hard_stop_boundary_is_40_nodes_not_41():
    """硬停的算子是 `n <= limit`：40 个带 model 节点放行、41 个判红。

    口径注（I1 裁定）：数的是 agent 节点数，不是 `chat/completions` 次数——11a 那次真跑
    `agent_runs=9` 对 13 次补全，所以这道闸是代理下界，Task 7 每跑完一条另抄 worker 实测。
    """
    from demo.demo_common import DemoAssertion, within_hard_stop

    within_hard_stop([{"model": "glm-4-flash"}] * 40)      # 边界内不抛
    with pytest.raises(DemoAssertion) as ei:
        within_hard_stop([{"model": "glm-4-flash"}] * 41)
    assert "超过硬停 40" in str(ei.value)


# ---- 轮询 ----


def test_poll_returns_as_soon_as_until_is_satisfied():
    from demo.demo_common import poll

    seen = []

    def fetch():
        seen.append(1)
        return {"status": "completed"} if len(seen) >= 3 else {"status": "running"}

    ticks = iter([0.0, 1.0, 2.0, 3.0])
    out = poll(fetch, lambda o: o["status"] == "completed",
               timeout_s=30.0, interval_s=1.0, clock=lambda: next(ticks),
               sleeper=lambda _s: None)
    assert out["status"] == "completed"
    assert len(seen) == 3


def test_poll_raises_demo_timeout_carrying_the_last_reading():
    from demo.demo_common import DemoTimeout, poll

    ticks = iter([0.0, 10.0, 20.0, 40.0])

    with pytest.raises(DemoTimeout) as ei:
        poll(lambda: {"status": "running", "progress": 33},
             lambda o: o["status"] == "completed",
             timeout_s=30.0, interval_s=1.0, clock=lambda: next(ticks),
             sleeper=lambda _s: None)
    assert ei.value.last["progress"] == 33   # 超时也要留下最后一次读数，不许只报「超时」


# ---- 回执与环境 ----


def test_write_receipt_is_utf8_json_and_returns_the_path(tmp_path):
    from demo.demo_common import write_receipt

    p = write_receipt(str(tmp_path), "demo1", {"结论": "中文原文", "n": 1})
    assert p.exists()
    raw = p.read_text(encoding="utf-8")
    back = json.loads(raw)                 # 不写 encoding 在 GBK 主机上会红
    assert back["结论"] == "中文原文"
    assert '"结论"' in raw and "\\u4e2d" not in raw   # 原文可读；ensure_ascii=True 时这行必须红


def test_env_required_names_the_missing_variable(monkeypatch):
    from demo.demo_common import env_required

    monkeypatch.delenv("P6_ORIGIN", raising=False)
    with pytest.raises(KeyError) as ei:
        env_required("P6_ORIGIN")
    assert "P6_ORIGIN" in str(ei.value)


def test_api_client_prefixes_api_v1_and_attaches_bearer():
    """用 MockTransport 证装配，不打网络：路径前缀与 Authorization 头是三条脚本共同的地基。"""
    from demo.demo_common import ApiClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    c = ApiClient("http://127.0.0.1:8081", "tok", transport=httpx.MockTransport(handler))
    r = c.get("/agents/tasks", params={"limit": 1})
    assert r.status_code == 200
    assert seen["url"] == "http://127.0.0.1:8081/api/v1/agents/tasks?limit=1"
    assert seen["auth"] == "Bearer tok"


def test_terminal_statuses_are_the_three_the_code_actually_writes():
    """字面量对齐实现：waiting_approval 不是终态，rejected 是（workflow_service.py:467）。"""
    from demo.demo_common import TERMINAL_STATUSES

    assert TERMINAL_STATUSES == frozenset({"completed", "failed", "rejected"})
    assert "waiting_approval" not in TERMINAL_STATUSES


# ---- 两条碰线的客户端方法（Demo 1 的 SSE 与 Demo 3 的上传都走它们） ----


def _sse_body() -> str:
    """帧原文抄自 app/api/chat.py（每帧 `data: <json>\\n\\n`，收尾 `data: [DONE]\\n\\n`）。"""
    return (
        'data: {"citations": [{"index": 1, "document_id": "d1", "filename": "a.md", '
        '"page": 2, "chunk_index": 3, "score": 0.8}]}\n\n'
        'data: {"text": "你好"}\n\n'
        'data: {"text": "，世界"}\n\n'
        'data: [DONE]\n\n'
    )


def test_stream_chat_returns_the_frame_sequence_and_reads_to_the_done_sentinel():
    from demo.demo_common import ApiClient, citation_document_ids, first_frame_is_citations

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        return httpx.Response(200, text=_sse_body())

    c = ApiClient("http://127.0.0.1:8081", "tok", transport=httpx.MockTransport(handler))
    frames = c.stream_chat({"conversation_id": "11111111-1111-1111-1111-111111111111",
                            "message": "库存低于安全线吗", "use_knowledge": True, "use_tools": False})
    assert (seen["method"], seen["path"]) == ("POST", "/api/v1/chat/stream")
    assert frames[-1] == {"done": True}
    assert first_frame_is_citations(frames) is True
    assert citation_document_ids(frames) == {"d1"}


def test_stream_chat_judges_a_non_200_red_instead_of_returning_empty_frames():
    from demo.demo_common import ApiClient, DemoAssertion

    c = ApiClient("http://127.0.0.1:8081", "tok",
                  transport=httpx.MockTransport(lambda _r: httpx.Response(500, text="boom")))
    with pytest.raises(DemoAssertion) as ei:
        c.stream_chat({"conversation_id": "11111111-1111-1111-1111-111111111111",
                       "message": "x", "use_knowledge": True, "use_tools": False})
    assert "应 200" in str(ei.value)


def test_post_form_sends_multipart_with_a_part_named_file():
    """服务端契约是 `file: UploadFile = File(...)`（app/api/documents.py:67）⇒ part 名必须是 file。"""
    from demo.demo_common import ApiClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["ctype"] = request.headers.get("Content-Type", "")
        seen["body"] = request.read().decode("utf-8", "replace")
        return httpx.Response(202, json={"id": "doc-1", "status": "queued"})

    c = ApiClient("http://127.0.0.1:8081", "tok", transport=httpx.MockTransport(handler))
    r = c.post_form("/documents", data={},
                    files={"file": ("company_report.md", "# 报告".encode("utf-8"), "text/markdown")})
    assert r.status_code == 202
    assert seen["path"] == "/api/v1/documents"
    assert seen["ctype"].startswith("multipart/form-data")
    assert 'name="file"; filename="company_report.md"' in seen["body"]


def test_cost_face_reads_only_the_three_money_fields():
    """钱门读数走 HTTP，不碰库、不碰 .env。**只取三个键**：
    把整个 cards 塞进回执会连带 recent_tasks/recent_conversations 一起出去，
    那是把用户数据抄进工件。"""
    from demo.demo_common import ApiClient, cost_face

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "range": "all", "scope": "org",
            "cards": {"task_total": 1, "success_rate": 1.0, "success_basis": {},
                      "total_tokens": 24661, "prompt_tokens": 20000,
                      "completion_tokens": 4661, "total_cost": 0.0,
                      "currency": "CNY", "pricing_configured": True},
            "recent_tasks": [{"id": "should-not-escape"}],
            "recent_conversations": [{"id": "should-not-escape"}],
        })

    client = ApiClient("http://x", "tok", transport=httpx.MockTransport(handler))
    face = cost_face(client)
    assert seen["url"].endswith("/api/v1/stats/overview?range=all")
    assert face == {"total_cost": 0.0, "pricing_configured": True, "total_tokens": 24661}
    assert "should-not-escape" not in str(face)
