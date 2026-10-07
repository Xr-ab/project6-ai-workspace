"""Workflow 图注册表（Phase 7）：graph_key → 图工厂的唯一查表入口。

编目在库（workflows 表，Task 3 种 3 行），定义在代码——本字典是唯一事实源，
graph_key 对不上这里就构建不出来（「不做可视化 Builder」的落点）。

更新义务：三键已齐（Task 5 doc_summary、Task 9 sales_analysis、Task 10 business_qa）；
scratch/test_p7_graphs.py 的 EXPECTED_KEYS 常量须同步改。

生命周期裁定（Task 4 遗留口径）：build_workflow_graph 每次现编译、不缓存——
checkpointer 单例可能被 shutdown 复位（旧编译图绑死池的坑 agent_task_service 已用
shutdown hook 处理过一次），「谁持有编译图缓存、何时失效」归 Task 6 的
workflow_service 统一裁定，基座层不留第二份模块级缓存就不需要第二个失效钩子。
"""
from collections.abc import Callable

from langgraph.graph.state import CompiledStateGraph

from app.ai.graph.checkpointer import get_checkpointer
from app.ai.graph.workflows import business_qa, doc_summary, sales_analysis

WORKFLOW_GRAPHS: dict[str, Callable[..., CompiledStateGraph]] = {
    "doc_summary": doc_summary.build,
    "sales_analysis": sales_analysis.build,  # Task 9（W1 销售数据分析）
    "business_qa": business_qa.build,  # Task 10（W2 问数，Phase 5 整图作子图）
}


def build_workflow_graph(graph_key: str) -> CompiledStateGraph:
    """按 graph_key 编译一张 Workflow 图。查不到 → KeyError（编目失配是缺陷，不兜底）。

    checkpointer 统一注入 Task 4 的进程单例：未 ensure_setup 时 get_checkpointer()
    抛 RuntimeError——不许静默回退 InMemorySaver（审批状态会假安全）。
    查表先于取单例：KeyError 不会被未 setup 的 RuntimeError 遮蔽。
    """
    factory = WORKFLOW_GRAPHS[graph_key]
    return factory(checkpointer=get_checkpointer())
