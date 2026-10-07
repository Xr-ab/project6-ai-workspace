"""Demo 2（spec §6.4）：Multi-Agent 智能分析 + Trace 全可见。

这条是唯一「一次提交、多次模型调用」的主线（Supervisor 编排 → 动态派发的取数/调研站
→ 业务综合 → 复核 → 报告；哪一站被派由 plan 决定，见下方 REQUIRED_NODES 的注释）。
所以它带两条钱门：脚本按 Trace 数带 model 的节点（超 LLM_HARD_STOP 直接判红），
调用者另外把 worker 日志里的 chat/completions 行数抄进工件（Task 7 Step 3）。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from demo.demo_common import (  # noqa: E402
    cost_face, env_required, llm_node_count, make_client, poll, require,
    TERMINAL_STATUSES, within_hard_stop, write_receipt,
)

QUESTION = "本月销售下降的可能原因有哪些？给三条建议，并注明依据的数据。"
# R50 读码改口：必含集合只留**结构上一定跑**的四站——
#   supervisor：START 固定边（app/ai/graph/workflow.py:99）
#   business_analyst：route_next_agent 的兜底终点，计划跑完或为空都到这（:57）
#   reviewer：business_analyst → reviewer 固定边（:112）
#   report：route_after_review 无论放行还是回炉超限都收敛到它（:64,76）
# data_analyst / research 由 Supervisor 的 plan 动态派发（supervisor.py:35 的 Literal
# 只锁「能派谁」，不锁「一定派谁」）——写进必含集合就是假断言。
REQUIRED_NODES = {"supervisor", "business_analyst", "reviewer", "report"}

# R55 读码改口（Task 7 第 2 次真跑撞红撞出来的，工件 backend/scratch/_p11c_t7_demo2/）：
# meta['report'] 从来不是散文，而是**结构化字典**。Phase 5 起 report 站按 docs/03 §2.6
# 用 Structured Output 约束成七个字段（app/ai/graph/nodes/report.py:25-32 的 Report 模型），
# 落进 task_run.meta 的是它的 model_dump（app/application/task_runner.py:217），
# 消费者一直按字典取（app/application/evaluation_service.py:625、scratch/e2e_phase5.py:68-72）。
# 原断言 `isinstance(report, str) and len(report) > 80` 是计划侧一处未读码的臆断，真跑必红。
# 实得 `{'risks': [...], 'sources': [...], …}` 那个反常键序也是读出来的事实：值存在 JSONB 列里，
# PostgreSQL 的 jsonb 按「键短者在前、同长按字典序」重排，`risks`(5) 自然排第一。
# 七个字段齐还是**降级面的探测器**：结构化失败时只写 {executive_summary, content}
# （report.py:59-61），所以「字段齐」= 这条演示主张（Structured Output 报告）真的通了。
REPORT_FIELDS = {"executive_summary", "key_findings", "data_evidence",
                 "root_causes", "risks", "recommendations", "sources"}


def report_volume(report: dict) -> int:
    """结构化报告的正文总量：所有字符串字段 + 所有列表条目的字符数。

    为什么把 >80 这道闸改成数「一个总量」而不是数 `executive_summary`：正文现在分布在七个
    字段里，单挑一个字段卡 80 字是拿模型的手气赌某一条（R51 的教训），七块加起来卡 80
    才是原断言「报告得真写成文」的本意，而且比它更强（空壳字典过不了）。
    """
    total = 0
    for value in report.values():
        if isinstance(value, str):
            total += len(value)
        elif isinstance(value, list):
            total += sum(len(str(item)) for item in value)
    return total


def run(client) -> dict:
    r = client.post("/agents/tasks", {"question": QUESTION, "task_type": "agent_analysis"})
    require(r.status_code == 202, f"提交应 202，实得 {r.status_code}：{r.text[:200]}")
    body = r.json()
    task_id, run_id = body["task_id"], body["task_run_id"]
    require(body["status"] == "queued", f"提交后状态应是 queued，实得 {body['status']}")

    detail = poll(
        lambda: client.get(f"/agents/tasks/{task_id}").json(),
        lambda d: (d.get("latest_run") or {}).get("status") in TERMINAL_STATUSES,
        timeout_s=900.0, interval_s=5.0)
    lr = detail["latest_run"]
    trace = client.get(f"/agents/task-runs/{run_id}/trace").json()
    nodes = trace["nodes"]

    require(llm_node_count(nodes) <= 12,
            f"Multi-Agent 单条链路的模型节点数 {llm_node_count(nodes)} 超过 12，链路与预期不符（不重跑）")
    within_hard_stop(nodes)
    require(lr["status"] == "completed",
            f"终态应 completed，实得 {lr['status']} failure={lr.get('failure_category')}")
    require(llm_node_count(nodes) >= 4, f"至少四个 agent 节点各一次模型调用，实得 {llm_node_count(nodes)}")
    names = {n["name"] for n in nodes}
    require(REQUIRED_NODES <= names, f"必经节点缺失：{sorted(REQUIRED_NODES - names)}")
    tools = [n for n in nodes if n["kind"] == "tool"]
    # R51 读码改口：工具调用几条是模型自由——tool_loop 只在模型自己点工具时才产出记录
    # （app/ai/graph/nodes/data_analyst.py:48-68），GRAPH_TOOLSETS 也只登记「哪两站有工具」
    # （app/ai/graph/toolsets.py:28-33），不保证条数。原断言 ≥2 是拿一次真跑（Task 7
    # 每条主线只许跑一次、且不许改断言凑绿）去赌模型的手气。门降到 ≥1 条成功——
    # 「数据腿至少通了一次」这个说法仍然非平凡；实际条数全量进收据，文档只写测到的数。
    tool_ok = [n for n in tools if n["status"] == "ok"]
    require(len(tools) >= 1, "没有 tool 节点 = Data Analyst 没真调工具")
    require(len(tool_ok) >= 1, f"成功的工具调用为 0（总 {len(tools)} 条），数据腿整条没通")
    ids = {n["span_id"] for n in nodes}
    require(len(ids) == len(nodes), "span_id 重复 = Trace 树不可信")
    require(all(n["parent_span_id"] in ids or n["parent_span_id"] is None for n in nodes),
            "每个节点要么有根要么父节点存在，否则不是树")
    # R49 读码改口：真实形状是**森林**，不是单根树。agent 行的父指针由
    # _persist_agent_runs 落库，而它压根不传 parent_span_id
    # （app/application/task_runner.py:268-292），node_guard 造的 span 也没有这个键
    # （app/ai/graph/errors.py:218-231），models.py:298 注明「NULL = 根 span」，
    # agent_task_service.py:404-406 更直说「归属不在这里判断……孤儿提升是前端
    # buildTraceTree 的事」。所以每个 agent 节点各是一棵树（≥4 个根），只有工具行
    # 挂在所属 agent 下（base.record_tool_call 盖章 → tool_call_repo.py:97 落列）。
    # 「单根」这条原断言在真跑上必红，而 Demo 2 只许跑一次——留着它就是拿交付去赌。
    roots = [n for n in nodes if n["parent_span_id"] is None]
    require(all(n["kind"] == "agent" for n in roots),
            f"根里混进非 agent 节点 = 工具行没盖章：{[n['name'] for n in roots if n['kind'] != 'agent']}")
    require(any(n["name"] == "supervisor" for n in roots),
            "supervisor 必须是可见根（START 固定边），它不在根里 = 图没从编排起步")
    agent_ids = {n["span_id"] for n in nodes if n["kind"] == "agent"}
    require(all(n["parent_span_id"] in agent_ids for n in tools),
            "工具行必须挂在某个 agent 节点下，挂不上 = Trace 的归属断了")
    require(all(n["status"] in ("ok", "error") for n in tools),
            f"工具状态只应是 ok/error（app/application/chat_service.py:194），"
            f"实得 {sorted({n['status'] for n in tools})}")
    report = (lr.get("meta") or {}).get("report")
    require(isinstance(report, dict),
            f"meta['report'] 应是结构化字典（Report.model_dump），实得 {type(report).__name__}")
    absent = REPORT_FIELDS - set(report)
    require(not absent,
            f"结构化报告缺字段 {sorted(absent)}：缺 = 走了 report.py:59 的降级支路，"
            f"「Structured Output 报告」这条演示主张就不成立")
    report_chars = report_volume(report)
    require(report_chars > 80,
            f"报告七个字段齐但正文总量只有 {report_chars} 字（闸仍是原来的 >80，"
            f"只是按整份报告数）：{str(report)[:120]}")

    return {
        "结论": "Multi-Agent 全链跑穿：编排→（派发站）→综合→复核→报告，Trace 成森林且工具真调",
        "机械断言清单": [
            "提交 202 且 status=queued", "终态 completed",
            f"结构必过节点齐（{sorted(REQUIRED_NODES)}）", "≥1 条成功的 tool 节点",
            "span_id 唯一 + 父指针可解析 + 根全为 agent 且 supervisor 在根里",
            "工具行挂在 agent 下且状态是 ok/error",
            "模型节点数 ≤12 且 ≤硬停 40",
            "meta.report 是字典且七个字段齐（= 没走降级支路）且正文总量>80",
        ],
        "钱门读数": cost_face(client),
        "task_id": task_id,
        "run_id": run_id,
        "模型节点数": llm_node_count(nodes),
        "节点名": sorted(names),
        "根节点": sorted(n["name"] for n in roots),
        "成功工具调用数": len(tool_ok),
        "工具调用": [{"name": n["name"], "status": n["status"],
                     "tokens": n.get("total_tokens")} for n in tools],
        "报告字数": report_chars,
        "报告非空字段": sorted(k for k, v in report.items() if v),
    }


def main(argv: list[str]) -> int:
    client, _origin = make_client()
    try:
        payload = run(client)
    finally:
        client.close()
    path = write_receipt(env_required("P6_ART_DIR"), "demo2", payload)
    print(f"DEMO2_PASS receipt={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
