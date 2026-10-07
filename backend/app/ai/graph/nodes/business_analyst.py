"""Business Analyst 节点（Phase 4 示例图第 2 站：分析）。

职责（docs/03-agent-design.md §2.4）：综合数据结论 + 原始问题，产出业务洞察。
「结论必须绑定数据依据」不是靠 prompt 求出来的——把 data_analyst 的结论原文
和每次工具调用记录（工具名/参数/是否成功/行数）都放进 prompt 材料，
模型没得编：材料里没有的数字它引不出来。
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.ai.graph.errors import node_guard
from app.ai.graph.memory import BUSINESS_ANALYST_INCLUDE, render_block
from app.ai.graph.state import TaskState
from app.ai.llm_service import llm_service
from app.ai.prompts import BUSINESS_ANALYST_PROMPT


def _render_data_results(data_results: list[dict]) -> str:
    """把 data_results 渲染成给模型看的材料：工具调用清单 + 结论全文。"""
    lines: list[str] = []
    for r in data_results:
        if r.get("tool") == "conclusion":
            lines.append(f"【数据结论】{r.get('text', '')}")
        else:
            lines.append(
                f"【工具调用】{r.get('tool')} args="
                f"{json.dumps(r.get('args', {}), ensure_ascii=False)} "
                f"ok={r.get('ok')} rows={r.get('rows')} {r.get('duration_ms')}ms"
            )
    return "\n".join(lines)


def _render_research_results(research_results: list[dict]) -> str:
    """把 research_results 渲染成调研材料（§2.4：综合分析要同时读数据 + 调研）。"""
    lines: list[str] = []
    for r in research_results:
        if r.get("tool") == "conclusion":
            lines.append(f"【调研结论】{r.get('text', '')}")
        else:
            lines.append(
                f"【检索调用】{r.get('tool')} args="
                f"{json.dumps(r.get('args', {}), ensure_ascii=False)} "
                f"ok={r.get('ok')} rows={r.get('rows')}"
            )
    return "\n".join(lines)


@node_guard("business_analyst")
async def business_analyst(state: TaskState, config: RunnableConfig) -> dict:
    material = _render_data_results(state.get("data_results", []))
    research_material = _render_research_results(state.get("research_results", []))
    # research 未激活时这段为空，不塞空标题污染材料
    research_block = f"\n\n调研材料：\n{research_material}" if research_material else ""
    messages = [
        SystemMessage(
            content=BUSINESS_ANALYST_PROMPT + render_block(
                state.get("memory_context"), include=BUSINESS_ANALYST_INCLUDE
            )
        ),
        HumanMessage(
            content=f"用户原始问题：{state['question']}\n\n数据分析材料：\n{material}{research_block}"
        ),
    ]
    content, prompt_tokens, completion_tokens = await llm_service.achat_stats(messages)

    meta = dict(state.get("meta") or {})
    meta["prompt_tokens"] = meta.get("prompt_tokens", 0) + prompt_tokens
    meta["completion_tokens"] = meta.get("completion_tokens", 0) + completion_tokens

    return {
        "analysis": {"content": content},
        "messages": [f"business_analyst 完成：分析 {len(content)} 字"],
        "meta": meta,
    }
