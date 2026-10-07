"""Reviewer 节点（Phase 5 工程版）：独立质量闸门（docs/03-agent-design.md §2.5 / §6）。

对比 scratch/reviewer_min.py 的规则桩，这里只把「判定」一步做实：
    写死的 verdict  ──▶  astructured(Review) 让模型按 schema 吐 {verdict, reasons, retry_targets}

为什么它必须是「独立节点 + 独立 Prompt」（§6）：
    被审的是 business_analyst 的结论。让同一个模型自己审自己，它会顺着自己的
    思路点头（幻觉、护短）。换一套「只当裁判、绝不重写」的指令 + 独立一次调用，
    才可能挑出「结论没数据支撑」这种它自己刚犯的错。

retry_count 在这里 +1（不在 router 里）：
    路由函数只能读 state、返回一个字符串，写不了白板（这是 Phase 4/5 反复踩的点）。
    「审一次算一轮」是 reviewer 这件事的一部分，所以计数归它做。

失败怎么办（拿不到合法 verdict）：
    审不动 ≠ 分析有问题。结构化失败时降级为 pass 放行——收尾链路不能因为
    「裁判没当上」就卡死，但把降级原因写进 meta，Trace 里能看到「这次没真正审」。
    （注：超限强制放行是 router 的职责，见 workflow.route_after_review。）
"""
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.ai.graph.errors import node_guard
from app.ai.graph.state import TaskState
from app.ai.graph.nodes.business_analyst import (
    _render_data_results,
    _render_research_results,
)
from app.ai.prompts import REVIEWER_PROMPT
from app.ai.structured_output import astructured


class Review(BaseModel):
    # verdict 用 Literal 锁死：router 靠它二选一，模型吐个 "maybe" 就会崩路由
    verdict: Literal["pass", "fail"] = Field(description="是否通过审核")
    reasons: list[str] = Field(default_factory=list, description="不通过的具体问题，逐条")
    # retry_targets 供 router 决定回炉到哪一站；同样 Literal 锁死成已注册节点名
    retry_targets: list[Literal["data_analyst", "research", "business_analyst"]] = Field(
        default_factory=list, description="需要重跑的环节"
    )


@node_guard("reviewer")
async def reviewer(state: TaskState, config: RunnableConfig) -> dict:
    analysis = (state.get("analysis") or {}).get("content", "")
    material = _render_data_results(state.get("data_results", []))
    research_material = _render_research_results(state.get("research_results", []))

    messages = [
        SystemMessage(content=REVIEWER_PROMPT),
        HumanMessage(
            content=(
                f"用户原始问题：{state['question']}\n\n"
                f"【待审分析结论】\n{analysis}\n\n"
                f"【数据材料】\n{material or '（无）'}\n\n"
                f"【调研材料】\n{research_material or '（无）'}\n\n"
                f"（本次已是第 {state.get('retry_count', 0) + 1} 次审核。）"
            )
        ),
    ]
    result = await astructured(messages, Review)

    meta = dict(state.get("meta") or {})
    meta["prompt_tokens"] = meta.get("prompt_tokens", 0) + result.prompt_tokens
    meta["completion_tokens"] = meta.get("completion_tokens", 0) + result.completion_tokens

    if result.ok:
        review = result.value.model_dump()
        note = (
            f"reviewer 判 {review['verdict']}"
            + (f"：{review['reasons']}" if review["verdict"] == "fail" else "")
        )
    else:
        # 裁判缺席不拦车：降级放行，但记下降级原因
        review = {"verdict": "pass", "reasons": [], "retry_targets": []}
        meta["errors"] = [*(meta.get("errors") or []), f"reviewer 结构化失败，已降级放行：{result.error}"]
        note = "reviewer 结构化失败，降级为 pass 放行"

    return {
        "review": review,
        "retry_count": state.get("retry_count", 0) + 1,  # 审一次 = 一轮，在这写
        "messages": [note],
        "meta": meta,
    }
