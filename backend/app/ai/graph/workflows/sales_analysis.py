"""sales_analysis（W1 销售数据分析报告）：Phase 7 第二张图——reviewer 回环整站复用。

节点链（brief 钦定形状）：
    analyze → draft(=business_analyst) → reviewer ⇄(route_after_review 条件边)
    → deliver(=report，即「draft 定稿」) → approval(静态断点) → final_report / reject_end

适配层怎么这么薄（「只复用不复制」硬约束的落点）：
    复用的三个 callable（business_analyst / reviewer / report）读写的是 TaskState 的
    analysis / review / retry_count / data_results / report 位，而 Workflow 契约字段是
    WorkflowState 的 workflow_input / result / approval_decision——两组键不冲突，
    meta 的 reducer 又是同一份对象（WorkflowState 本来就 import 自 state.merge_dict），
    所以 TypedDict 多继承直接合流出 SalesWorkflowState，节点原样挂图、零改动。
    真正的适配只剩 analyze（头）与 final_report（尾）两个业务节点。
    WorkflowState.draft 在本图闲置：草稿演进位由 TaskState.analysis（business_analyst
    的写位）承担、定稿由 report 位承担——「draft 定稿」的语义走这对键，不再复写第二份。

analyze 是 data_analyst 的「确定性替身」：W1 的取数口径固定（query_sales 按 sales_scope
聚合），不需要模型决定调哪个工具 → 不走 tool_loop、不配 RetryPolicy（registry.execute
对工具内异常已有兜底捕获，炸点收敛成人话 TaskNodeError）。身份照旧从 config 注入的
ToolContext 取，绝不从 state 取；工具调用记录进 ctx.tool_call_sink（Task 6 落
tool_calls 表的同一数据源，Trace 页工具层不缺账）。

reviewer 回环语义（Phase 5 逐字沿用，路由函数零改动）：
    MAX_REVIEW_ROUNDS=2 封顶的是审核总次数；超限强制放行由 route_after_review 判。
    本图没有 research 站，回炉 targets 里 data_analyst/research 都收敛到 analyze
    重发查询（只读、可重跑，同 Phase 5「回炉到哪」的降级就近原则）。
    review_timeout 标记由 final_report 写：router 写不了 state，末端才见得到终审结果——
    能走到末端而 verdict 仍非 pass，只可能是超限降级路径（route_after_review 的几何
    保证），标 meta["review_timeout"]，任务终态照旧 completed（errors.py:42
    「降级不置 failed，执行层特判」的设计内路径）。report 节点自己会把 reviewer
    理由挂成 report.review_warning（「带警告的最终报告」，docs/03 §2.5，也是现成的）。

审批末端（Task 4/6 裁定逐字沿用）：编译期 interrupt_before=["approval"]，停在审批
节点之前；决策经 aupdate_state({"approval_decision":…})（不带 as_node）写回；
route_after_approval 凡 ≠ "approved" 一律 rejected（宁拒不放），reject_end 显式
result=None，Task 6 据此把 task.status 落 'rejected'（不是 failed）。
"""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.ai.graph.errors import RETRYABLE_NODES, TaskNodeError, apply_retry, node_guard
from app.ai.graph.nodes import business_analyst, report, reviewer
from app.ai.graph.state import TaskState
from app.ai.graph.workflow import route_after_review
from app.ai.graph.workflows.base import (
    APPROVAL_NODE,
    WorkflowState,
    approval_node,
    route_after_approval,
)
from app.ai.tools import ToolCallRequest, execute as tool_execute
from app.ai.tools.base import record_tool_call


class SalesWorkflowState(WorkflowState, TaskState):
    """两条既有白板的合流：Workflow 契约键 + Phase 5 回环三件套的读写位。

    多继承合法的前提是同名键类型逐字节一致——本仓只有 meta 一个同名键，两侧都是
    Annotated[dict, merge_dict]（WorkflowState.meta 直接复用 state.merge_dict），
    scratch/test_p7_graphs.py 的探针段已实测合流后 reducer 不丢。
    """


# analyze 落 data_results 的调用行形状 = data_analyst 同款五键
# （business_analyst._render_data_results 按 tool/args/ok/rows/duration_ms 渲染材料，
#  形状对齐它，复用节点看到的材料才是完整的）
@node_guard("analyze")
async def analyze(state: SalesWorkflowState, config: RunnableConfig) -> dict:
    """输入物化：workflow_input{question, sales_scope} → question 落位 + query_sales 真查数。

    零 LLM：query_sales 走本地库（READ ONLY 事务 + 组织过滤都在工具里），
    所以本套件（stub-LLM）里可以真调用，钱纪律不破。
    question 缺失/空白即判任务失败——不给坏输入去烧后面回环的机会（ingest 同款守卫）。
    sales_scope 原样交给 registry.execute 校验（Literal 挡非法参数，返回 ok=False 的
    ToolResult 而非抛异常——registry 是唯一关口的老纪律）。
    """
    ctx = config["configurable"]["tool_context"]
    params = state.get("workflow_input") or {}
    question = str(params.get("question") or "").strip()
    if not question:
        raise TaskNodeError("tool_failure", "workflow_input.question 不能为空", "analyze")
    scope = params.get("sales_scope")
    if scope is not None and not isinstance(scope, dict):
        raise TaskNodeError("tool_failure", f"sales_scope 须为 dict 或 null：{scope!r}", "analyze")
    raw_args = dict(scope or {})

    result = await tool_execute("query_sales", raw_args, ctx)
    if ctx.tool_call_sink is not None:  # 与 Phase 5 同一落库通道（AI 层不直接写表）
        # 经 record_tool_call 构造：本节点是 analyze（guard 包过 → ctx 带当前 span 身份），
        # 归属戳必须在这里盖上，末尾 _persist_tool_calls 批量落库时已经认不出来源了。
        ctx.tool_call_sink.append(
            record_tool_call(
                ctx,
                ToolCallRequest(id="analyze-query-sales", name="query_sales", args=raw_args),
                result,
            )
        )
    if not result.ok:
        raise TaskNodeError("tool_failure", f"query_sales 失败：{result.error}", "analyze")

    data = result.data or {}
    total = data.get("total") or {}
    groups = data.get("groups") or []
    period = data.get("period") or {}
    per_group = "；".join(
        f"{g.get('name')}：销售额 {g.get('amount')}，毛利率 {g.get('margin_pct')}%，占比 {g.get('share_pct')}%"
        for g in groups
    )
    conclusion = (
        f"query_sales(group_by={data.get('group_by')}, args={raw_args})，"
        f"数据区间 {period.get('from')} ~ {period.get('to')}。"
        f"合计：销售额 {total.get('amount')}，成本 {total.get('cost')}，"
        f"毛利 {total.get('profit')}，整体毛利率 {total.get('margin_pct')}%，"
        f"共 {len(groups)} 组。分组明细：{per_group}"
    )

    meta = dict(state.get("meta") or {})
    meta["sales"] = {"group_by": data.get("group_by"), "args": raw_args,
                     "rows": result.rows, "total": total}
    return {
        "question": question,  # 复用节点（business_analyst/reviewer/report）的读位
        "data_results": [  # add 字段：只回本轮新增（data_analyst 同款纪律）
            {"tool": "query_sales", "args": raw_args, "ok": True,
             "rows": result.rows, "duration_ms": result.duration_ms},
            {"tool": "conclusion", "text": conclusion},
        ],
        "messages": [f"analyze 完成：query_sales 返回 {result.rows} 组"],
        "meta": meta,
    }


@node_guard("final_report")
async def final_report(state: SalesWorkflowState, config: RunnableConfig) -> dict:
    """审批放行后的成品装配：定稿报告 + 分析原文 + 数据来源 → result（Task 6 落 meta.result）。

    review_timeout 标记的判据是几何的：route_after_review 只在 verdict==pass 或
    retry_count≥MAX_REVIEW_ROUNDS 时放行进 deliver，所以走到这里仍非 pass 必然是
    超限降级——meta 打标（completed 不是 failed，errors.py:42 设计内路径），
    报告侧的 review_warning 由 report 节点自己挂（现成件，不重复实现）。
    """
    review = state.get("review") or {}
    meta = dict(state.get("meta") or {})
    if review.get("verdict") != "pass":
        meta["review_timeout"] = True
    return {
        "result": {
            "report": state.get("report") or {},
            "analysis": (state.get("analysis") or {}).get("content", ""),
            "data_sources": [
                r for r in (state.get("data_results") or []) if r.get("tool") != "conclusion"
            ],
        },
        "meta": meta,
    }


@node_guard("reject_end")
async def reject_end(state: SalesWorkflowState, config: RunnableConfig) -> dict:
    """拒绝支线终点：显式 result=None（doc_summary 同款，Task 6 判 rejected 的锚）。

    同步备忘：三图同款（doc_summary / sales_analysis / business_qa 各复制一份，
    spec §2-5「每图独立定义」裁定），改一处看三处。
    """
    return {"result": None}


def build(*, checkpointer) -> CompiledStateGraph:
    """编译 sales_analysis 图。checkpointer 由注册表统一注入（Task 4 单例）。"""
    g = StateGraph(SalesWorkflowState)
    # analyze：纯 DB 读、无 LLM，不配 RetryPolicy（重跑等价多查一次，收益不抵噪音）。
    g.add_node("analyze", analyze)
    # draft 站 = business_analyst 现成 callable 原样挂。apply_retry 按注册名查表，
    # 这里节点名是 "draft"，显式转挂表里同一份 policy——重试语义与 Phase 5 逐字节同源。
    g.add_node("draft", business_analyst, retry_policy=RETRYABLE_NODES["business_analyst"])
    # reviewer 名与 RETRYABLE_NODES 对得上，apply_retry 直接挂（超限轮数的计数器在它内部）。
    apply_retry(g.add_node, "reviewer", reviewer)
    # deliver = report：收尾节点不配重试（Phase 5 同款判断：重跑副作用大于收益）。
    g.add_node("deliver", report)
    g.add_node(APPROVAL_NODE, approval_node)
    g.add_node("final_report", final_report)
    g.add_node("reject_end", reject_end)

    g.add_edge(START, "analyze")
    g.add_edge("analyze", "draft")
    g.add_edge("draft", "reviewer")
    # reviewer 出口逐字复用 route_after_review：pass / 超限 → "report"（本图的 deliver）；
    # 回炉 "business_analyst"→draft 重做分析；"data_analyst"/"research" 本图没有对应站，
    # 就近收敛到 analyze 重发查询（path_map 必须穷举 router 的全部返回串，缺一个键
    # 就是运行期 unknown target 崩图——语义降级不崩链，宁缺环不缺键）。
    g.add_conditional_edges("reviewer", route_after_review, {
        "report": "deliver",
        "data_analyst": "analyze",
        "research": "analyze",
        "business_analyst": "draft",
    })
    g.add_edge("deliver", APPROVAL_NODE)
    # path_map 键 = route_after_approval 的返回串（"deliver"/"rejected"）；
    # "deliver" 通道指向本图的末端装配站 final_report（名字是 base.py 的路由值，不是节点名）。
    g.add_conditional_edges(APPROVAL_NODE, route_after_approval, {
        "deliver": "final_report",
        "rejected": "reject_end",
    })
    g.add_edge("final_report", END)
    g.add_edge("reject_end", END)

    return g.compile(checkpointer=checkpointer, interrupt_before=[APPROVAL_NODE])
