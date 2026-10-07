"""图→工具全集静态表（Phase 8b T5，R-8b-2 F1 角色预检的判据源）。

干什么：trigger / agent submit 那一刻在 API 进程内回答「这张图会碰哪些注册工具、
当前角色有没有一个够不着的」——缺一个就整单拒（403 AUTH_403002），
而不是放行后在执行中段吃 in-band 403002 静默降质（8a 终评 F1 点的正是这个洞）。

为什么是静态表而不是编译图遍历（绑定裁定，勿再翻案）：
    ① 编译要活着的 checkpointer（build_workflow_graph 在未 ensure_setup 的进程里
       直接 RuntimeError）——预检发生在触发那一刻的 API 请求里，不该为一次权限
       判断拉起图编译的全部依赖；
    ② 遍历节点取工具名要真执行各图工厂，且 analyze 这类节点的工具名藏在
       registry.execute("query_sales") 的调用参数里，不是节点属性——AST/动态扫描
       两条路都贵且骗人；
    ③ 静态表是唯一「不骗钱」的形状：一眼可审、零依赖、纯函数。
代价与对策：表和图可能漂移。更新义务与 WORKFLOW_GRAPHS 同款——**改图工具集必改
这张表**（先例：app/ai/graph/workflows/__init__.py 头注的 EXPECTED_KEYS 纪律）。
scratch/test_p8b_worker.py ⑰ 用「GRAPH_TOOLSETS 键集 = WORKFLOW_GRAPHS ∪
{agent_analysis}」把键漂移钉死；值的漂移靠每条目 file:line 实测留痕（下）。

graph_key 的键域 = WORKFLOW_GRAPHS（三张 workflow 图）+ "agent_analysis"
（Phase 5 agent 整图，/agents 提交面与 business_qa 子图共用，不属 WORKFLOW_GRAPHS）。
"""
from app.ai.tools import registry  # import 即完成全部工具注册（tools/__init__ 副作用）
from app.ai.tools.permissions import tool_allowed

# 实读定表（2026-09-27，T5；每处 file:line 为当日 HEAD 实测）：
#
# agent_analysis（app/ai/graph/workflow.py build_task_graph 挂的工具有且只有两站：
#   data_analyst 与 research——supervisor/reviewer/business_analyst/report 零工具，
#   grep tool_names|run_tool_loop|registry.execute 全 nodes/*.py 实证）
#   = DATA_TOOLS（data_analyst.py:28-32: query_sales/query_product/query_customer/sql_query
#     + Phase 10 外部库存域三把 query_stock_levels/query_stock_movements/suggest_replenishment）
#   ∪ RESEARCH_TOOLS（research.py:22: web_search/rag_search/document_retriever）
# business_qa（business_qa.py:116 把 Phase 5 整图当子图挂上）→ 与 agent_analysis 同集。
# sales_analysis（sales_analysis.py:89 analyze 节点直调 registry.execute("query_sales")；
#   draft=business_analyst / reviewer / deliver=report 都不走 tool_loop（同 grep 实证））
#   → 只有 {query_sales}。⚠️ 实测修正（brief Step 1.2 的猜想不成立）：该图零 admin-only
#   工具，member 触发它没有 F1 可拒——trigger 403 针因此落在 business_qa 上（留痕见报告）。
# doc_summary（ingest=repos 直调、retrieve=app.ai.rag.retriever 直调、summarize=
#   llm_service 直调——全图零「注册工具」；rag 能力走函数不走 registry.execute）→ 空集。
GRAPH_TOOLSETS: dict[str, set[str]] = {
    "agent_analysis": {
        "query_sales", "query_product", "query_customer", "sql_query",
        "web_search", "rag_search", "document_retriever",
        # Phase 10：外部库存域（改图工具集必改这张表——本文件头注的纪律）
        "query_stock_levels", "query_stock_movements", "suggest_replenishment",
    },
    "business_qa": {
        "query_sales", "query_product", "query_customer", "sql_query",
        "web_search", "rag_search", "document_retriever",
        # Phase 10：外部库存域（改图工具集必改这张表——本文件头注的纪律）
        "query_stock_levels", "query_stock_movements", "suggest_replenishment",
    },
    "sales_analysis": {"query_sales"},
    "doc_summary": set(),
}


def missing_tools(graph_key: str, role: str) -> set[str]:
    """该角色触发/提交 graph_key 之前就已经确定拿不到的工具名集合；空集 = 放行。

    未知 graph_key → **KeyError（响亮，不 fail-open）**：表漂移是缺陷，
    返回空集等于给未编目的图发免检通行证——「改图必改表」的纪律只有配「漏表必炸」
    才成立（与 build_workflow_graph 的 KeyError 先例同款：查表先于兜底）。
    表里有名但注册表查无此具 → 计入缺（permission 无从判定，默认拒绝，
    与 permissions.py 头注「新增工具不进表 = member 不可用」同一族纪律）。
    """
    try:
        tools = GRAPH_TOOLSETS[graph_key]
    except KeyError:
        raise KeyError(
            f"GRAPH_TOOLSETS 未登记图 '{graph_key}'（现登记 {sorted(GRAPH_TOOLSETS)}）"
            "——改图必改表"
        ) from None
    out: set[str] = set()
    for name in tools:
        tool = registry.get_tool(name)
        if tool is None or not tool_allowed(role, tool.permission):
            out.add(name)
    return out
