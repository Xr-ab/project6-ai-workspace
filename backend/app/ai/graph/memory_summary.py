"""把滚出窗口的旧轮次交给模型压成一段主线（docs/11 §4.5）—— **本模块有 IO**。

为什么和 `memory.py` 分居两个文件：
    `memory.py` 的立身之本是「零 IO 纯函数」，§8 那一页确定性单测全靠这条才跑得动；
    一次模型调用搬进去，那页测试立刻要么变慢、要么得满地打桩。分工因此是三层：

        判「哪些轮滚出窗口」 → `memory.archived_rounds()`（纯函数）
        压不成时怎么兜底   → `memory.digest_rounds()`（纯函数）
        怎么把这几轮写成一句话 → 本模块（调模型 + 超时 + 失败返回 error）

    调用方（`agent_task_service._build_memory_for`）负责「要不要调」，本模块只负责
    「怎么调、怎么不把追问拖死、失败怎么交差」。

为什么模型调用不放在图里：
    它发生在图启动**之前**（记忆装配阶段），此时没有节点、没有 span 可挂。但记账不能
    因此丢失：返回的 `SummaryResult` 带着 token 与起止时间，由执行层（`task_runner`）
    追加一条 `node="memory_summary"` 的 span 落进 `agent_runs` —— 成本面板统计的正是
    这张表，run 之外的模型调用也必须有自己的行，否则"摘要花了多少钱"永远是一笔糊涂账。

失败姿态：**永不抛**。摘要只是记忆的一种更省空间的写法，模型挂了就退回规则摘要
（`build_memory` 会自己兜），任务不该因为"摘要没写漂亮"而失败。超时同理：追问是
用户等着的同步路径（§4.5），宁可这轮摘要降级，也不能让一次 follow-up 卡在模型上。
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from app.ai.llm_service import llm_service
from app.ai.prompts import MEMORY_SUMMARY_PROMPT

# 8 秒：占位的是"一次短输出调用"的合理上限。DeepSeek 正常 1~3s 返回，超过 8s 基本是
# 网络/服务端异常，继续等只会把一次追问拖成十几秒。常量而不做成 settings 开关：
# 它没有第二种正确取值，做开关只会让人以为"调大了就好了"。
SUMMARY_TIMEOUT_SECONDS = 8.0

# 少于 2 轮不调模型（见 summarize_archived 里的判据）。
SUMMARY_MIN_ROUNDS = 2

# 摘要正文字数下限。真库实测（2026-10-21，glm-4-flash）两次踩中：
#   只压 1 轮时返回「数据。」（3 字）；压 2 轮时返回「者查询条件设置不正确。」（10 字）——
#   后者还是从原料里"抄到半个句子"的形状。这种输出进 prompt 比没有摘要更糟：
#   它占着【更早轮次摘要】的标题，却不承载任何主线信息，模型会拿它当既成事实。
#   所以低于 15 字一律判失败、回退规则摘要（digest 至少会点名"第几轮在问什么"）。
SUMMARY_MIN_CHARS = 15

# 原料的渲染上限：轮流贴全量 conclusion（每轮 120 字）× 8 轮 ≈ 1000 字，正是"够模型
# 看懂主线"的量；再多的轮次靠 `archived_rounds` 的窗口上限（MEMORY_RECALL_ROUNDS）拦。
_ROUND_LINE_CHARS = 120


@dataclass
class SummaryResult:
    """一次摘要调用的结果，成功失败都在这里（调用方不接异常）。

    text 为 None 就是"这轮没有模型摘要"，调用方回退 `digest_rounds`；
    error 非空则说明**尝试过但失败**，要落进 stats 的降级痕（§7 可观测纪律）。
    attempted 区分"没调"与"调了"：原料不足两道门是合法跳过（不花 token、不算失败），
    此时 attempted=False，调用方**不该**把它记进 agent_runs —— 否则成本面板会多出一行
    tokens=0、status=error 的幽灵 span，看起来像一次失败调用（task_runner 的契约：
    span 只在调用真的发生了时才存在）。
    """

    text: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None
    attempted: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.text)


def render_material(rounds: list[dict]) -> str:
    """摘要原料 → 给模型看的多行文本（旧→新）。

    结论也给它看：模型要从"问题 + 结论"里提炼主线，只给问题它写不出"一路怎么收窄"。
    但结论按 `_ROUND_LINE_CHARS` 截断——摘要的产出上限是 200 字，原料再长也变不成
    更准的一句话，只会在输入侧多烧 token。
    """
    lines: list[str] = []
    for r in rounds:
        question = str(r.get("question") or "").strip()
        conclusion = str(r.get("conclusion") or "").strip()[:_ROUND_LINE_CHARS]
        if not question and not conclusion:
            continue
        lines.append(f"第 {r.get('round')} 轮\n- 问题：{question or '（未记录）'}\n- 结论：{conclusion or '（未记录）'}")
    return "\n\n".join(lines)


async def summarize_archived(rounds: list[dict]) -> SummaryResult:
    """把滚出窗口的旧轮次写成一段主线摘要；失败返回带 error 的结果，**绝不抛**。

    两道**不调模型**的门（都不算"失败"，`error` 留空，调用方照常用规则摘要）：
      ① 原料不足 `SUMMARY_MIN_ROUNDS` 轮 —— 单轮没有"主线"可总结，模型只能复读，
         而规则版的「第 N 轮主线：<问题>」信息量更高且不要钱。真库实测压 1 轮时
         模型返回「数据。」（3 字），正是这个门要挡的形状；
      ② 原料全都没问题也没结论 —— 理论上到不了，真到了也别空调一次。
    """
    started_at = datetime.now(timezone.utc)
    t0 = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    rounds = rounds or []
    material = render_material(rounds)
    if len(rounds) < SUMMARY_MIN_ROUNDS or not material:
        return SummaryResult(started_at=started_at, finished_at=datetime.now(timezone.utc))

    messages = [SystemMessage(content=MEMORY_SUMMARY_PROMPT), HumanMessage(content=material)]
    # usage 只有在请求真成功返回时才拿得到；超时/抛异常路径保持 (0, 0)。
    # 判废路径（空文本/质量闸）也必须把 usage 带回去：调用真发生了、token 真花了，
    # 记 0 就是让成本面板少算一笔（task_runner 的契约：error span = 尝试过但降级，
    # token 花了）。真库实测第 7 轮那条判废 span 曾写着 tokens=0，就是这么漏的。
    used_prompt, used_completion = 0, 0
    try:
        # wait_for 而不是给 ChatOpenAI 设 request_timeout：那个是全局模型实例的属性，
        # 改它会把图里所有节点一起改掉；这里要限的只是"这条记忆摘要"。
        text, prompt_tokens, completion_tokens = await asyncio.wait_for(
            llm_service.achat_stats(messages), timeout=SUMMARY_TIMEOUT_SECONDS
        )
        used_prompt, used_completion = prompt_tokens or 0, completion_tokens or 0
    except asyncio.TimeoutError:
        error = f"摘要调用超时（>{SUMMARY_TIMEOUT_SECONDS:.0f}s）"
    except Exception as exc:  # noqa: BLE001 —— 摘要失败不该让追问失败，统一降级
        error = f"摘要调用失败：{type(exc).__name__}: {exc}"
    else:
        cleaned = _clean(text)
        if not cleaned:
            error = "摘要返回空文本"
        elif len(cleaned) < SUMMARY_MIN_CHARS:
            # 质量闸：短到承载不了主线的一句话，比规则摘要更糟（常量注释里记了实测值）
            error = f"摘要过短（{len(cleaned)} < {SUMMARY_MIN_CHARS} 字），判为不可用"
        else:
            return SummaryResult(
                text=cleaned,
                prompt_tokens=used_prompt,
                completion_tokens=used_completion,
                duration_ms=_elapsed(),
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                attempted=True,
            )

    # 能走到这里 = 真的发了请求（超时 / 抛异常 / 空文本 / 质量闸判废），attempted 必须为真
    return SummaryResult(
        prompt_tokens=used_prompt,
        completion_tokens=used_completion,
        duration_ms=_elapsed(),
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        error=error,
        attempted=True,
    )


def _clean(text: str | None) -> str | None:
    """模型输出 → 一行干净摘要。

    模型的三类惯性要在这里削掉：① 爱加「摘要：」「综上，」这类前缀；② 爱换行分点
    （而 `render_block` 里摘要占一行，多行会把后面几个块的格式带歪）；③ 爱把结论原文
    再抄一遍。长度不在这里截——截断口径归 `memory.build_memory`（`clip_text`），
    两边各截一次就会出现"摘要 200 字"和"落库 180 字"的不一致。
    """
    if not text:
        return None
    line = " ".join(str(text).split())        # 换行 / 连续空白一律压成单空格
    for prefix in ("摘要：", "摘要:", "总结：", "总结:", "综上，"):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
            break
    return line.strip(" \"'“”") or None
