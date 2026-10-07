"""Data Analyst 节点（Phase 4 示例图第 1 站：查数）。

职责（docs/03-agent-design.md §2.2）：生成查询 → 执行 → 解读，产出数据结论。
Phase 4 直接复用 Phase 3 的 tool_loop（bind_tools + 多轮循环 + registry 唯一关口），
只用 tool_names 过滤出数据类工具 —— 这是 docs/03 §5 的「工具绑定边界」：
每个 Agent 只绑定自己职责内的工具，Data Analyst 看不见 Web Search。

为什么整个节点可以放心被 RetryPolicy 重跑：
    节点内工具全是只读查询（SELECT / READ ONLY 事务），重跑只是多查一次，
    没有副作用累积。这和「落库类节点不配重试」是同一条判断标准的正反两面。

ToolContext 为什么从 config 传、不走 state：
    session / organization_id 是运行时依赖，不是任务数据；塞进 state 会被
    Checkpointer 序列化存快照（session 对象存不住），而且中断恢复后旧 session
    早已关闭。config 每次 invoke 现场给，恢复时自然拿到新鲜的。
"""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.ai.graph.errors import node_guard
from app.ai.graph.state import TaskState
from app.ai.prompts import DATA_ANALYST_PROMPT, TOOL_RULES
from app.ai.tool_loop import run as run_tool_loop
from app.ai.tools.base import ToolContext

# 数据分析师的工具边界：业务语义查询 + 自由只读 SQL（不含 knowledge / research 工具）
DATA_TOOLS = [
    "query_sales", "query_product", "query_customer", "sql_query",
    # Phase 10：外部库存域三把（经 MCP 读 enterprise_data）。不给这三把 = 模型看不见它们
    "query_stock_levels", "query_stock_movements", "suggest_replenishment",
]


@node_guard("data_analyst")
async def data_analyst(state: TaskState, config: RunnableConfig) -> dict:
    ctx: ToolContext = config["configurable"]["tool_context"]
    messages = [
        SystemMessage(content=f"{DATA_ANALYST_PROMPT}\n\n{TOOL_RULES}"),
        HumanMessage(content=state["question"]),
    ]

    data_results: list[dict] = []
    conclusion = ""
    prompt_tokens = completion_tokens = 0

    # role=ctx.role：schema 面按角色收口（8b F1 第二道），与 tool_names 边界求交
    async for chunk in run_tool_loop(
        messages, ctx=ctx, tool_names=DATA_TOOLS, role=ctx.role
    ):
        if chunk.type == "text":
            conclusion += chunk.text  # 不再要求调工具的那一轮，文本就是数据结论
        elif chunk.type == "usage":
            # 每轮各报一次，必须累加（Phase 3 踩过用 = 赋值丢历史轮次的坑）
            prompt_tokens += chunk.prompt_tokens
            completion_tokens += chunk.completion_tokens
        elif chunk.type == "tool_call":
            if ctx.tool_call_sink is not None:  # 交执行层落 tool_calls 表（Phase 6 工具成功率数据源）
                ctx.tool_call_sink.append(chunk.tool_call)
            data_results.append(
                {
                    "tool": chunk.tool_call.call.name,
                    "args": chunk.tool_call.call.args,
                    "ok": chunk.tool_call.result.ok,
                    "rows": chunk.tool_call.result.rows,
                    "duration_ms": chunk.tool_call.result.duration_ms,
                }
            )

    data_results.append({"tool": "conclusion", "text": conclusion})

    # meta 是浅合并 dict，token 是覆盖字段 → 返回算好的新绝对值（旧值 + 本轮增量）
    meta = dict(state.get("meta") or {})
    meta["prompt_tokens"] = meta.get("prompt_tokens", 0) + prompt_tokens
    meta["completion_tokens"] = meta.get("completion_tokens", 0) + completion_tokens

    return {
        "data_results": data_results,  # add 字段：只返回本轮新增，绝不 append 旧 list
        "messages": [f"data_analyst 完成：{len(data_results) - 1} 次工具调用"],
        "meta": meta,
    }
