"""工具包（Phase 3）：抽象 + 注册表 + 各领域工具实现。

按 docs/09-roadmap.md §6 的目录约定：
    base.py             Tool / ToolContext / ToolResult / ToolCallRecord
    registry.py         注册、枚举、分发（唯一的执行关口）
    data_tools.py       sql_query / csv_reader / excel_reader / python_calculator / data_statistics
    knowledge_tools.py  rag_search / document_retriever
    research_tools.py   web_search
    business_tools.py   query_customer / query_product / query_sales
    external_tools.py   query_stock_levels / query_stock_movements / suggest_replenishment
                        （Phase 10：经 MCP 读外部库存域，tool_type="external"）

⚠️ 为什么本文件必须**显式 import** 各工具模块：
    注册靠的是"模块被导入时执行 register(...)"这条副作用。
    只 import registry 而不 import 各工具模块 → 注册表是空的 →
    模型看不到任何工具、也不会调用任何工具，而且**不报错**，
    表现成"这个模型不太会用工具"，极难排查。
    （对比：Alembic 的 env.py 必须 import app.data.models，同一类坑。）
"""
from app.ai.tools.base import Tool, ToolCallRecord, ToolCallRequest, ToolContext, ToolResult
from app.ai.tools.registry import execute, get_tool, list_tools, register, to_openai_schema

# 各领域工具模块：import 即注册（副作用），不要删掉这些看似"没被用到"的 import
from app.ai.tools import (  # noqa: F401
    business_tools,
    data_tools,
    external_tools,  # Phase 10：MCP 外部库存域（漏这行 = 注册表里根本没这三把，且不报错）
    knowledge_tools,
    research_tools,
)

__all__ = [
    "Tool",
    "ToolCallRecord",
    "ToolCallRequest",
    "ToolContext",
    "ToolResult",
    "execute",
    "get_tool",
    "list_tools",
    "register",
    "to_openai_schema",
]