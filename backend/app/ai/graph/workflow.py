"""任务图组装（Phase 5 Multi-Agent）：把 6 个节点接成「动态调度 + 质量回环」的完整图。

对比 Phase 4 的骨架（查数→分析→报告，走哪条路写死）：
    Phase 4：START → data_analyst → business_analyst → report → END   （线性，谁跑固定）
    Phase 5：加了 supervisor（出计划）、research（可选分支）、reviewer（质量闸门 + 回炉），
             并把「固定边」换成「条件边」——跑不跑 research、要不要打回重跑，都由 state 里的数据现场决定。

这就是 docs/03 §1「Workflow-first + Supervisor」的落地：
    顺序仍由图控制（不是自由 Swarm），但「走哪条分支」由模型产出的 plan 驱动 → 可控 + 动态。

────────────────────────────────────────────────────────────────
本文件有两个「路由函数」，它们是整张图唯一的智能调度点。记住共同铁律：
    路由函数【只读 state、返回一个字符串】，绝不写 state（写是节点的活）。
    返回的字符串必须是 path_map 里登记过的目标，否则 LangGraph 报 unknown node。
────────────────────────────────────────────────────────────────
"""

from langgraph.graph import END, START, StateGraph

from app.ai.graph.checkpointer import get_checkpointer
from app.ai.graph.errors import apply_retry
from app.ai.graph.nodes import (
    business_analyst,
    data_analyst,
    report,
    research,
    reviewer,
    supervisor,
)
from app.ai.graph.state import TaskState

# 审核轮数上限 2（docs/03 §2.5「如 2 轮」）。retry_count 由 reviewer 每次审完 +1，
# 所以"2"封顶的是**审核总次数**：第 1 次 fail → 回炉 1 轮 → 第 2 次审完到顶，
# 再 fail 只能强制放行、报告带警告——不是能回炉 2 轮。
# 限轮存在的唯一理由：防止 reviewer 永远挑刺 → 图在「分析↔审核」之间死转。
MAX_REVIEW_ROUNDS = 2


# ── 路由函数①：动态派发（supervisor 出计划后，决定下一个跑谁）──────────────
# supervisor / data_analyst / research 三个节点的出口共用它。
# 它读 plan 里的 task_breakdown，按顺序找「第一个还没跑的 agent」派过去；
# 都跑完（或 plan 为空/降级）→ 进 business_analyst 做综合。
#
# 「跑过了没」怎么判断？——看对应的结果列表非空。data_analyst / research 跑完
# 必定 append 至少一条 conclusion（见各自节点末尾），所以列表非空 = 这个 agent 跑过。
# 用现成的 add 字段当标记，省得为「进度」再往 state 塞一个计数字段。
def route_next_agent(state: TaskState) -> str:
    plan = state.get("plan") or {}
    ran = {
        "data_analyst": bool(state.get("data_results")),
        "research": bool(state.get("research_results")),
    }
    for task in plan.get("task_breakdown", []):
        agent = task["agent"]
        if not ran.get(agent, False):
            return agent  # 计划里这个 agent 还没跑 → 派它
    return "business_analyst"  # 计划内该跑的都跑了 → 综合


# ── 路由函数②：审核后道岔（reviewer 判完，决定放行还是回炉）──────────────────
def route_after_review(state: TaskState) -> str:
    review = state.get("review") or {}
    if review.get("verdict") == "pass":
        return "report"  # 通过 → 出报告
    # 没过：还在轮数内才回炉，否则降级放行（带警告，见 report 读 review）
    if state.get("retry_count", 0) < MAX_REVIEW_ROUNDS:
        targets = review.get("retry_targets") or []
        # 回炉到哪，听 reviewer 的 retry_targets（缺数据回 data_analyst、缺背景回
        # research、分析本身不行就重做 business_analyst）。回炉节点跑完后，它的出口
        # 条件边会把图带回派发/综合流程，最终再次经过 reviewer。
        if "data_analyst" in targets:
            return "data_analyst"
        if "research" in targets:
            return "research"
        return "business_analyst"
    return "report"  # 超限：不再回炉，带警告进报告


# build_task_graph 的 checkpointer 缺省哨兵：不传 → 取进程单例（Phase 5/6 原行为逐字不变）；
# 显式传 False → 编出不带 checkpointer 的实例（Task 10 子图挂父图用；None 不算——
# langgraph 会把 None 解释成「继承父图 saver」，子命名空间照样双写）。
_UNSET = object()


def build_task_graph(*, interrupt_before: list[str] | None = None,
                     checkpointer=_UNSET):
    """编译 Multi-Agent 图。interrupt_before 用于人工审批 / 中断恢复场景。"""
    g = StateGraph(TaskState)
    # apply_retry：按 RETRYABLE_NODES 自动挂 RetryPolicy。supervisor/research/reviewer
    # 早在 Phase 4 就填进了那张表——当时预留的名字，现在全都用上了。
    apply_retry(g.add_node, "supervisor", supervisor)
    apply_retry(g.add_node, "data_analyst", data_analyst)
    apply_retry(g.add_node, "research", research)
    apply_retry(g.add_node, "business_analyst", business_analyst)
    apply_retry(g.add_node, "reviewer", reviewer)
    g.add_node("report", report)  # 收尾节点不配重试，理由见 nodes/report.py 模块注释

    # ── 边：注意「条件边」才是 Phase 5 新增的东西 ──
    g.add_edge(START, "supervisor")  # 第一步固定进 supervisor（它必跑，出计划）

    # supervisor / data_analyst / research 出口都接派发路由：跑谁、跑完去哪，看 plan + 结果列表
    dispatch_map = {
        "data_analyst": "data_analyst",
        "research": "research",
        "business_analyst": "business_analyst",
    }
    g.add_conditional_edges("supervisor", route_next_agent, dispatch_map)
    g.add_conditional_edges("data_analyst", route_next_agent, dispatch_map)
    g.add_conditional_edges("research", route_next_agent, dispatch_map)

    # business_analyst 跑完固定进 reviewer（审的是它的分析结论）
    g.add_edge("business_analyst", "reviewer")

    # reviewer 出口道岔：放行 / 回炉任意一站
    g.add_conditional_edges("reviewer", route_after_review, {
        "report": "report",
        "data_analyst": "data_analyst",
        "research": "research",
        "business_analyst": "business_analyst",
    })

    g.add_edge("report", END)

    # get_checkpointer()：同步取进程内单例（lifespan 保证 ensure_setup 先于首次建图，
    # 未 setup 直接抛 RuntimeError——不许静默回退 InMemorySaver）
    if checkpointer is _UNSET:
        checkpointer = get_checkpointer()
    kwargs: dict = {"checkpointer": checkpointer}
    if interrupt_before:
        kwargs["interrupt_before"] = interrupt_before
    return g.compile(**kwargs)
