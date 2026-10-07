"""Research 节点（Phase 5）：调检索工具补外部背景 / 知识库证据。

职责（docs/03-agent-design.md §2.3）：只在 Supervisor 判定「需要外部背景」时被激活，
用 web_search / rag_search / document_retriever 拿证据，产出「调研结论 + 来源」，
不做最终业务判断（那是 business_analyst 的活）。

和 data_analyst 是同构的：都复用 Phase 3 的 tool_loop，区别只有两处——
  ① 工具边界：这里只绑研究类工具，数据分析师的 SQL/业务查询它看不见（§5 工具隔离）；
  ② 结果落进 research_results（data_analyst 落进 data_results），
     下游 business_analyst 会把两份材料一起读。
"""
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.ai.graph.errors import node_guard
from app.ai.graph.state import TaskState
from app.ai.prompts import RESEARCH_PROMPT, TOOL_RULES
from app.ai.tool_loop import run as run_tool_loop
from app.ai.tools.base import ToolContext

# 研究员的工具边界：联网搜索 + 知识库检索（不含 SQL / 业务查询工具）
RESEARCH_TOOLS = ["web_search", "rag_search", "document_retriever"]


@node_guard("research")
async def research(state: TaskState, config: RunnableConfig) -> dict:
    ctx: ToolContext = config["configurable"]["tool_context"]
    messages = [
        SystemMessage(content=f"{RESEARCH_PROMPT}\n\n{TOOL_RULES}"),
        HumanMessage(content=state["question"]),
    ]

    research_results: list[dict] = []
    conclusion = ""
    prompt_tokens = completion_tokens = 0

    # role=ctx.role：schema 面按角色收口（8b F1 第二道），与 tool_names 边界求交
    async for chunk in run_tool_loop(
        messages, ctx=ctx, tool_names=RESEARCH_TOOLS, role=ctx.role
    ):
        if chunk.type == "text":
            conclusion += chunk.text
        elif chunk.type == "usage":
            prompt_tokens += chunk.prompt_tokens
            completion_tokens += chunk.completion_tokens
        elif chunk.type == "tool_call":
            if ctx.tool_call_sink is not None:  # 交执行层落 tool_calls 表（Phase 6 工具成功率数据源）
                ctx.tool_call_sink.append(chunk.tool_call)
            research_results.append(
                {
                    "tool": chunk.tool_call.call.name,
                    "args": chunk.tool_call.call.args,
                    "ok": chunk.tool_call.result.ok,
                    "rows": chunk.tool_call.result.rows,
                    "duration_ms": chunk.tool_call.result.duration_ms,
                }
            )

    research_results.append({"tool": "conclusion", "text": conclusion})

    meta = dict(state.get("meta") or {})
    meta["prompt_tokens"] = meta.get("prompt_tokens", 0) + prompt_tokens
    meta["completion_tokens"] = meta.get("completion_tokens", 0) + completion_tokens

    return {
        "research_results": research_results,  # add 字段：只返回本轮新增
        "messages": [f"research 完成：{len(research_results) - 1} 次检索"],
        "meta": meta,
    }
