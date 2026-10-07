"""LLM-as-judge（docs/08 §5.3）：给一条用例的实际输出打分。

与 reviewer 的分工（两者都是"判质量"，别混）：
    reviewer 在**生产链路内**，判"这次交付要不要回炉"，影响任务结果；
    judge 在**评测链路外**，判"这条用例过没过"，只影响指标数字。
    共用一个节点等于让评测能改写生产 —— 评测必须是纯观察者。

失败返回 None 而不是抛：一条用例的 judge 调用失败不该让整批 15 分钟的评测白跑。
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.ai.structured_output import astructured
from app.ai.prompts import JUDGE_ACTUAL_HEADER, JUDGE_HEADER, JUDGE_RULES


class JudgeVerdict(BaseModel):
    """§5.3 的强制形状：分档 + 是否通过 + 理由，三样缺一不可。

    字段名取 `passed` 而非文档写的 `pass`（pass 是 Python 关键字，pydantic 字段
    不能叫它）—— 只改字段名不改语义，这处偏差是裁定 D8。
    """

    score: int = Field(ge=0, le=5)
    passed: bool
    reasons: list[str] = Field(default_factory=list)


def build_judge_messages(*, question: str, key_points: list[str], actual_output: str,
                         must_include: list[str], must_not_include: list[str]) -> list:
    """把判分依据拼成消息。单独一个纯函数：提示词形状是可单测的，不藏在调用点里。"""
    parts = [
        f"【用户问题】\n{question}",
        "【期望要点】\n" + ("\n".join(f"- {p}" for p in key_points) or "（未提供）"),
    ]
    if must_include:
        parts.append("【必须出现】\n" + "\n".join(f"- {m}" for m in must_include))
    if must_not_include:
        parts.append("【不得出现】\n" + "\n".join(f"- {m}" for m in must_not_include))
    parts.append(JUDGE_ACTUAL_HEADER + "\n" + (actual_output or "（空输出）"))
    return [SystemMessage(content=JUDGE_HEADER + "\n\n" + JUDGE_RULES),
            HumanMessage(content="\n\n".join(parts))]


async def judge_case(*, question: str, key_points: list[str], actual_output: str,
                     must_include: list[str] | None = None,
                     must_not_include: list[str] | None = None,
                     ) -> tuple[JudgeVerdict | None, int, int]:
    """返回 (判分或 None, prompt_tokens, completion_tokens)。

    judge 自己的 token 也要记账并计入成本：一次评测 10 条用例 = 10 次额外模型调用，
    不算进来的话报表上的 total_cost 会系统性低估评测的真实开销（这正是要给老板看的数）。
    —— 但只记到这里返回的两个数 + case_snapshot["judge"]，**绝不并入指标层的
    total_tokens/total_cost**（判分开销 ≠ 被测产品开销，见 evaluation_service 模块头）。
    """
    msgs = build_judge_messages(
        question=question, key_points=key_points, actual_output=actual_output,
        must_include=must_include or [], must_not_include=must_not_include or [])
    res = await astructured(msgs, JudgeVerdict)
    if not res.ok or res.value is None:
        return None, res.prompt_tokens, res.completion_tokens
    return res.value, res.prompt_tokens, res.completion_tokens
