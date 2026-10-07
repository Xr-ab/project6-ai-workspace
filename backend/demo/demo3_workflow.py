"""Demo 3（spec §6.4，裁定见计划开头第 3 条）：Workflow 把「上传→RAG→审批→交付」串成一条声明式图。

选 doc_summary 而不是 W1 那张销售分析图的理由：只有它的 input_spec 真收上传件
（`{document_id, question}`），选另一个的话「上传销售数据」这一步就是纯装饰。
（那张图的英文标识符在本文件里一次都不许出现，注释也不行——Step 2 的图选择针
是全源文本子串禁令，见其 R47 注。）
CSV 在这里是**被检索的语料**，不是 sql_query 的数据源（库存数据在 MCP 那边，Phase 10 的面）。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from demo.demo_common import (  # noqa: E402
    cost_face, env_required, make_client, poll, require, TERMINAL_STATUSES,
    write_receipt,
)

FIXTURE = BACKEND_ROOT / "tests/fixtures/demo/sales_2026q3.csv"
GRAPH_KEY = "doc_summary"
WAITING = "waiting_approval"          # 实现里的字面量（workflow_service.py:57），不是 awaiting_*

# 提问保持英文（ASCII）：原先是硬约束且本条实测过——中文问 + 纯 ASCII 英文 CSV 语料的
# 余弦只有 0.4132（本地 bge-small-zh-v1.5，scratch/_p11c_t7_query_probe/），低于
# retriever.DEFAULT_MIN_SCORE=0.45 ⇒ 检索空 ⇒ meta.result.sources 空 ⇒ 本条判红；
# Task 7 在 Demo 1 撞红后先用零钱探针量了这里，才没把它带到真跑上。
# 2026-10-03 修复波后本图已豁免该阈值（doc_summary.retrieve 传 min_score=0.0），中文问
# 不再被误杀；提问保持 ASCII 属演示稳定性选择（本波未改问句＝不重烧真跑），
# 针口径见 test_demo_scripts_static.py。
SUMMARY_QUESTION = "Summarize sales by region and channel over the quarter."


def pick_workflow(client) -> dict:
    rows = client.get("/workflows").json()
    require(isinstance(rows, list) and rows, "工作流目录必须是非空裸数组")
    hit = [w for w in rows if w.get("graph_key") == GRAPH_KEY]
    require(len(hit) == 1, f"按 graph_key={GRAPH_KEY} 应正好选中一行，实得 {len(hit)} 行")
    require(hit[0]["is_active"], f"{GRAPH_KEY} 处于非激活，触发必然 404")
    return hit[0]


def run(client) -> dict:
    wf = pick_workflow(client)

    with FIXTURE.open("rb") as fh:
        up = client.post_form("/documents", data={},
                              files={"file": (FIXTURE.name, fh, "text/csv")})
    require(up.status_code == 202, f"上传应 202，实得 {up.status_code}")
    doc = up.json()
    indexed = poll(lambda: client.get(f"/documents/{doc['id']}").json(),
                   lambda d: d["status"] in ("ready", "failed"),
                   timeout_s=300.0, interval_s=3.0)
    require(indexed["status"] == "ready", f"CSV 索引没到 ready：{indexed['status']}")

    trig = client.post(f"/workflows/{wf['id']}/trigger",
                       {"inputs": {"document_id": doc["id"],
                                   "question": SUMMARY_QUESTION}})
    require(trig.status_code == 202, f"触发应 202，实得 {trig.status_code}：{trig.text[:200]}")
    task_id = trig.json()["task_id"]

    # 关键一停：approval 是 interrupt_before 的断点，任务必须真的**停在那里**而不是冲过去
    stopped = poll(lambda: client.get(f"/agents/tasks/{task_id}").json(),
                   lambda d: (d.get("latest_run") or {}).get("status") in TERMINAL_STATUSES
                             or d.get("status") == WAITING,
                   timeout_s=600.0, interval_s=4.0)
    require(stopped["status"] == WAITING,
            f"应停在 {WAITING}，实得 {stopped['status']}（图没停 = 审批环节是假的）")

    approvals = client.get(f"/agents/tasks/{task_id}/approvals").json()
    pending = [a for a in approvals if a["status"] == "pending"]
    require(len(pending) == 1, f"应有一条待审批，实得 {len(pending)} 条 / 共 {len(approvals)} 条")

    decided = client.post(f"/agents/tasks/{task_id}/approvals/{pending[0]['id']}",
                          {"decision": "approved", "comment": "Demo 3 机械批准"})
    require(decided.status_code == 200, f"决策应 200，实得 {decided.status_code}")
    require(decided.json()["status"] == "approved", f"审批状态应 approved，实得 {decided.json()['status']}")

    done = poll(lambda: client.get(f"/agents/tasks/{task_id}").json(),
                lambda d: (d.get("latest_run") or {}).get("status") in TERMINAL_STATUSES,
                timeout_s=600.0, interval_s=4.0)
    lr = done["latest_run"]
    require(lr["status"] == "completed",
            f"批准之后应 completed，实得 {lr['status']} failure={lr.get('failure_category')}")
    result = (lr.get("meta") or {}).get("result") or {}
    require(isinstance(result.get("summary"), str) and len(result["summary"]) > 40,
            f"meta.result.summary 应是成文结论，实得 {str(result.get('summary'))[:80]}")
    require(isinstance(result.get("sources"), list) and result["sources"],
            "meta.result.sources 必须非空——交付物没有来源就是没做完")

    return {
        "结论": "Workflow 一条链跑穿：上传→索引→图执行停在审批→批准→completed→交付带来源",
        "机械断言清单": [
            "目录按 graph_key 正好选中一行且 is_active", "CSV 索引到 ready",
            "触发 202", f"真停在 {WAITING}", "待审批恰好一条",
            "决策 approved 且 200", "终态 completed",
            "meta.result.summary 长度>40 且 sources 非空",
        ],
        "钱门读数": cost_face(client),
        "图提问": SUMMARY_QUESTION,
        "workflow_id": wf["id"],
        "graph_key": wf["graph_key"],
        "document_id": doc["id"],
        "task_id": task_id,
        "approval_id": pending[0]["id"],
        "来源条数": len(result.get("sources") or []),
        "结论字数": len(result.get("summary") or ""),
    }


def main(argv: list[str]) -> int:
    client, _origin = make_client()
    try:
        payload = run(client)
    finally:
        client.close()
    path = write_receipt(env_required("P6_ART_DIR"), "demo3", payload)
    print(f"DEMO3_PASS receipt={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
