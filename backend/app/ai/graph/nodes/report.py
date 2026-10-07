"""Report 节点（Phase 5）：把分析结论产出成结构化最终报告。

对比 Phase 4：那时它只把 analysis 写成一段散文；现在按 docs/03 §2.6 用
Structured Output 约束成固定字段（摘要/发现/依据/原因/风险/建议/来源），
前端和 Evaluation 才能按字段取用，而不是从散文里猜。

不配 RetryPolicy（RETRYABLE_NODES 里没有它）——理由同 Phase 4：
    报告失败通常是模型/配置类永久问题，重跑收益不划算；失败直接走降级，
    而不是抛异常崩掉整个任务（末端节点必须保证有产出）。

降级策略：astructured 拿不到合法结构时，退回把分析原文塞进 content 字段，
    报告「糙但存在」，任务仍算跑完 —— 收尾节点宁可降级不可崩。
"""
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.ai.graph.errors import node_guard
from app.ai.graph.memory import REPORT_INCLUDE, render_block
from app.ai.graph.state import TaskState
from app.ai.prompts import REPORT_PROMPT
from app.ai.structured_output import astructured


class Report(BaseModel):
    executive_summary: str = Field(description="一段话讲清结论")
    key_findings: list[str] = Field(default_factory=list, description="核心发现，逐条")
    data_evidence: list[str] = Field(default_factory=list, description="支撑结论的数据依据")
    root_causes: list[str] = Field(default_factory=list, description="原因分析")
    risks: list[str] = Field(default_factory=list, description="风险提示")
    recommendations: list[str] = Field(default_factory=list, description="可执行建议")
    sources: list[str] = Field(default_factory=list, description="数据/调研来源")


@node_guard("report")
async def report(state: TaskState, config: RunnableConfig) -> dict:
    analysis = (state.get("analysis") or {}).get("content", "")
    messages = [
        SystemMessage(
            content=REPORT_PROMPT + render_block(
                state.get("memory_context"), include=REPORT_INCLUDE
            )
        ),
        HumanMessage(
            content=f"用户原始问题：{state['question']}\n\n业务分析结论：\n{analysis}"
        ),
    ]
    result = await astructured(messages, Report)

    meta = dict(state.get("meta") or {})
    meta["prompt_tokens"] = meta.get("prompt_tokens", 0) + result.prompt_tokens
    meta["completion_tokens"] = meta.get("completion_tokens", 0) + result.completion_tokens

    if result.ok:
        payload = result.value.model_dump()
        note = f"report 完成：{len(payload['key_findings'])} 条发现"
    else:
        # 降级：至少把分析原文交出去，别让整条链白跑
        payload = {"executive_summary": analysis, "content": analysis}
        meta["errors"] = [*(meta.get("errors") or []), f"report 结构化失败，已降级为原文：{result.error}"]
        note = "report 降级为分析原文"

    # 审核未通过却被放行（retry 超限，route_after_review 降级进这里）时，
    # 把 reviewer 的理由如实挂到报告上——「带警告的最终报告」（docs/03 §2.5）。
    # 在这里标而不是在 router 标：router 写不了 state，report 才是最终打包站。
    review = state.get("review") or {}
    if review.get("verdict") != "pass" and review.get("reasons"):
        payload["review_warning"] = review["reasons"]
        note += f"（带警告：审核未通过 {len(review['reasons'])} 条）"

    return {
        "report": payload,
        "messages": [note],
        "meta": meta,
    }
