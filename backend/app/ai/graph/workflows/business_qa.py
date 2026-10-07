"""business_qa（W2 业务问题问数）：Phase 5 Supervisor 整图作子图 + 审批末端。

节点链（brief 钦定形状）：
    to_team → agent_team(=build_task_graph 编译产物) → from_team
    → approval(静态断点) → deliver / reject_end

这是 spec §7「整图作子图」的实现形，也是「Workflow 与 Agent 配合而非传统编排」
（docs/01:139）的机制落点：子图内部 supervisor 派发 / 工具循环 / reviewer 回炉
全部 Phase 5 原样，父图只在头尾各加一个映射站、尾部接审批闸。

state 合流走任务 9 实证过的多继承法：BusinessQaWorkflowState(WorkflowState, TaskState)，
meta 两侧同为 Annotated[dict, merge_dict]，子图（TaskState 形状）的终态经父图
channel reducer 合流回来——plan/analysis/review/report/data_results 都在父图白板可见
（scratch/test_p7_graphs.py A 组逐位断言）。

两条子图纪律（docs/10:145 旧坑的规避）：
    ① 断点只放父图 approval——子图编译不带 interrupt_before，循环节点带断点会
      每次进入都停；审批也不需要子图内部恢复。
    ② 子图 checkpointer=False（不是 None）——langgraph 1.2.12 实测 None 的语义是
      「继承父图 saver」，照样往 checkpoints 表写 agent_team:<taskid> 子命名空间行
      （双写没省掉）；False 才是真关。子图终态已合入父图 channel、随父图
      checkpoint 落库即可（test A 组实证本 thread 只剩根命名空间行）。
    config（thread_id/tool_context/spans）经 LangGraph 原样透传进子图：内部节点
    的 node_guard 写同一 spans 列表、工具调用写同一 ctx.tool_call_sink，Trace 不缺账。

from_team 的空产物守卫是审批闸的前置：子图 report 拿不出 executive_summary 就不
进 approval（fail with failure_category，Task 6 落 failed），审批人永远不会面对
一份空草稿。deliver 的 result 用 summary 形（{summary: str, sources: []}）——
前端 ResultBlock 第一支命中（异形 result 会被整块 JSON 摊开，难看）。
"""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.ai.graph.errors import TaskNodeError, node_guard
from app.ai.graph.state import TaskState
from app.ai.graph.workflow import build_task_graph
from app.ai.graph.workflows.base import (
    APPROVAL_NODE,
    WorkflowState,
    approval_node,
    route_after_approval,
)


class BusinessQaWorkflowState(WorkflowState, TaskState):
    """Workflow 契约键 + Phase 5 子图读写位的合流（sales_analysis 同款多继承先例）。"""


@node_guard("to_team")
async def to_team(state: BusinessQaWorkflowState, config: RunnableConfig) -> dict:
    """WorkflowState → TaskState 的头部映射：workflow_input.question 落子图的读位。

    question 缺失/空白即判任务失败（analyze/ingest 同款守卫）——坏输入不烧子图
    整条多 Agent 链的机时。
    """
    params = state.get("workflow_input") or {}
    question = str(params.get("question") or "").strip()
    if not question:
        raise TaskNodeError("tool_failure", "workflow_input.question 不能为空", "to_team")
    return {"question": question}


@node_guard("from_team")
async def from_team(state: BusinessQaWorkflowState, config: RunnableConfig) -> dict:
    """TaskState → WorkflowState 的尾部映射：report 定稿铺进 draft，空产物拦在审批前。

    review_timeout 判据与 sales final_report 同几何：能走到这里而终审非 pass，
    只可能是子图内超限强制放行路径——meta 打标，任务终态照旧 completed。
    """
    rep = state.get("report") or {}
    summary = str(rep.get("executive_summary") or "").strip()
    if not summary:
        raise TaskNodeError(
            "output_validation", "子图未产出有效报告（report 为空），不带空产物进审批", "from_team"
        )
    findings = [str(f) for f in (rep.get("key_findings") or []) if str(f).strip()]
    draft = summary + ("\n" + "\n".join(f"- {f}" for f in findings) if findings else "")
    meta = dict(state.get("meta") or {})
    if (state.get("review") or {}).get("verdict") != "pass":
        meta["review_timeout"] = True
    return {"draft": draft, "meta": meta}


@node_guard("deliver")
async def deliver(state: BusinessQaWorkflowState, config: RunnableConfig) -> dict:
    """审批放行后的成品装配：summary 形 result（前端 ResultBlock 第一支命中）。"""
    rep = state.get("report") or {}
    return {
        "result": {
            "summary": state.get("draft") or "",
            "sources": rep.get("sources") or [],
        }
    }


@node_guard("reject_end")
async def reject_end(state: BusinessQaWorkflowState, config: RunnableConfig) -> dict:
    """拒绝支线终点：显式 result=None（doc_summary/sales 同款，Task 6 判 rejected 的锚）。

    同步备忘：三图同款（doc_summary / sales_analysis / business_qa 各复制一份，
    spec §2-5「每图独立定义」裁定），改一处看三处。
    """
    return {"result": None}


def build(*, checkpointer) -> CompiledStateGraph:
    """编译 business_qa 图。checkpointer 由注册表统一注入（只给父图用）。"""
    g = StateGraph(BusinessQaWorkflowState)
    g.add_node("to_team", to_team)
    # 整图作子图：已编译的 Phase 5 Multi-Agent 图直接当节点挂上（spec §7 钦定形），
    # 内部重试/派发/reviewer 回环零改动，父图不重复包。checkpointer=False 而非 None
    # （None 在 1.2.12 会继承父图 saver 双写子命名空间，裁定依据见模块头纪律②）。
    g.add_node("agent_team", build_task_graph(checkpointer=False))
    g.add_node("from_team", from_team)
    g.add_node(APPROVAL_NODE, approval_node)
    g.add_node("deliver", deliver)
    g.add_node("reject_end", reject_end)

    g.add_edge(START, "to_team")
    g.add_edge("to_team", "agent_team")
    g.add_edge("agent_team", "from_team")
    g.add_edge("from_team", APPROVAL_NODE)
    # path_map 键 = route_after_approval 的返回串；本图 deliver 就是末端装配站（同名节点）
    g.add_conditional_edges(APPROVAL_NODE, route_after_approval, {
        "deliver": "deliver",
        "rejected": "reject_end",
    })
    g.add_edge("deliver", END)
    g.add_edge("reject_end", END)

    # 审批断点只放父图（docs/10:145：子图循环节点带断点每次进入都停）
    return g.compile(checkpointer=checkpointer, interrupt_before=[APPROVAL_NODE])
