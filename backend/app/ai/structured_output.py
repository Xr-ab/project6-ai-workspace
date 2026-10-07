"""Structured Output 基础封装（Phase 3 / docs/09-roadmap.md §6）。

要解决的问题：让模型"按固定字段"吐数据，给代码直接消费 ——
Phase 4 的规划节点、Phase 5 的 Supervisor 路由与 Reviewer 结论都要用。

为什么不能只在 prompt 里写"请返回 JSON"：
    那是**请求**，不是**约束**。实测模型会：套 ```json 代码围栏、前后加
    "好的，以下是结果："、字段名自作主张（summary → 摘要）、漏必填字段、
    把数字写成字符串。它在自然语言层面完全合理，但下游 `data["summary"]`
    直接 KeyError —— 而这类错误在测试里常常查不出来（模型今天听话、明天不听话）。
    结构化输出把 schema 交给 **API 协议层**（走 function calling 通道），
    模型在生成时就被限制在 schema 的字段集合里，不靠它自觉。

为什么协议层之外还要有第二层（本文件的校验 + 重试）：
    协议层只保证"是个符合 schema 形状的 JSON"，不保证**业务语义**对
    （枚举取值、数值范围、必填非空）。所以拿到结果仍要过 Pydantic 校验；
    不通过时把**校验错误原文**回喂给模型让它重出 —— 和 tool loop 里
    ToolResult(ok=False) 回填是同一条思路：模型有能力自我修正，
    前提是有人明确告诉它"哪里不对"。

为什么返回 StructuredResult 而不是抛异常：
    调用方要能区分两类失败：
      · 模型没给对格式 → 可以降级（换策略 / 用默认值 / 整段跳过）
      · 代码 bug（schema 定义错、网络断）→ 应该炸出来让人看到
    全部抛异常会把两类混在一起，Phase 4 的图节点就做不了降级分支。
"""
from dataclasses import dataclass
from typing import TypeVar

from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel

from app.ai.llm_service import llm_service

ModelT = TypeVar("ModelT", bound=BaseModel)

# 默认最多 3 次：第 1 次是正常请求，第 2、3 次是纠错重试。
# 实测格式错误基本一次纠错就能改对；给到 3 次还错说明是 schema 本身有问题，
# 继续重试只是烧钱。
DEFAULT_MAX_ATTEMPTS = 3


@dataclass
class StructuredResult:
    """结构化输出结果。成功失败都从这里取，调用方不接异常。"""

    ok: bool
    value: BaseModel | None = None
    error: str | None = None
    attempts: int = 1
    # token 要累计：重试的次数就是模型调用的次数，只记最后一次会少算钱
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _hint(schema: type[BaseModel], error: str) -> str:
    """重试时追加给模型的话。

    只给错误原文，不重复贴 schema —— function calling 每次调用都会把
    schema 一起发过去，再贴一遍是白烧 token。
    """
    return (
        f"你上一次的输出没有通过 {schema.__name__} 的校验，报错是：{error}\n"
        "请重新输出一次，每个字段都必须满足 schema 的约束。"
    )


async def astructured(
    messages: list[BaseMessage],
    schema: type[ModelT],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> StructuredResult:
    """请求模型按 schema 输出，返回校验过的对象。

    返回的是 StructuredResult（不是 ModelT），因为"失败"是常态之一，
    必须显式处理；成功时调用方取 result.value 并断言其类型。
    """
    runnable = llm_service.bind_structured(schema)
    convo: list[BaseMessage] = list(messages)
    prompt_tokens = 0
    completion_tokens = 0
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        resp = await runnable.ainvoke(convo)

        # include_raw=True 时返回 {"raw": AIMessage, "parsed": ..., "parsing_error": ...}
        raw = resp["raw"]
        usage = raw.usage_metadata or {}
        prompt_tokens += usage.get("input_tokens", 0)
        completion_tokens += usage.get("output_tokens", 0)

        parsed = resp["parsed"]
        if parsed is not None:
            return StructuredResult(
                ok=True,
                value=parsed,
                attempts=attempt,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        last_error = str(resp["parsing_error"] or "模型没有按要求输出结构化内容")
        # 重试时**不把模型上一轮的原始消息塞回历史**：
        # function_calling 模式下它是带 tool_calls 的 AI 消息，只有配上
        # tool 角色的回填消息才合法；缺一条会让服务端直接拒绝整个请求。
        # 我们只用一条 HumanMessage 说明哪里错了，重出一条即可。
        convo = [*messages, HumanMessage(_hint(schema, last_error))]

    return StructuredResult(
        ok=False,
        error=last_error,
        attempts=max_attempts,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )