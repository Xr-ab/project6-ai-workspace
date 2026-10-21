"""短期记忆（Phase 6 / docs/11-memory-design.md）：把历轮终态快照压成 memory_context。

**零 IO 的纯函数模块**：不 import ORM、不查库、不调模型。
    取几轮快照是仓储（task_repo）的事，怎么压缩是这里的事 —— 分开之后
    §4 那一整页判断（白名单 / 分层 / 预算 / 裁剪顺序）全部可以用
    scratch/test_memory_context.py 喂构造快照测完，不必连数据库、不必烧 token。

三条纪律落点（docs/11 §1）：
    显式白名单 → _brief_* / _report_brief / _usable_runs 是唯一出口，只从 state 里点状取
              question / analysis / data_results / research_results / report 五个键，
              plan / review / retry_count / messages / meta 物理上进不来（不另设"禁用键"常量：
              那玩意味是黑名单，加新字段时就漏了）；
    有窗口有预算 → MEMORY_* 常量 + _apply_budget 的固定裁剪顺序；
    可观测 → build_memory 返回 (context, stats)，stats 落 task_runs.meta.memory。

分层判据见 docs/11 §4.4：结论线（history）跨轮累积，明细线（facts / research_briefs /
report_brief）只取最近一轮 —— 结论并存是推理轨迹，数字并存是口径矛盾。

摘要压缩见 docs/11 §4.5：滚出窗口的轮次**不丢弃**，压成一行主线摘要（`summary` /
`summary_rounds` 两个键）。压法两档：模型摘要（调用方在 `agent_task_service` 里调，
本模块只收结果文本）+ 规则兜底 `digest_rounds`（模型失败/未调用时用）。本模块仍然零 IO
——「要不要调模型」和「怎么调」都不在这里。
"""
import json
import logging

from app.ai.prompts import MEMORY_FOOTER, MEMORY_HEADER

# 本模块仍是"零 IO 纯函数"：logger 只用于 §7 的降级留痕（写日志不算 IO 依赖，也不影响返回值）。
# 注意 Python 的 lastResort handler 会把没配 handler 的 WARNING 直接打到 stderr，
# 所以单测要一份干净输出得显式 logging.disable(logging.WARNING) —— 见 scratch/test_memory_context.py。
logger = logging.getLogger(__name__)

# ---- 预算（docs/11 §4.3，改动必须同步改那节）----
MEMORY_ROUNDS_MAX = 4          # history 最多带几轮（本轮之外的历史轮数）
MEMORY_RECALL_ROUNDS = 12      # 最多回看多少轮原始快照（§4.5）：窗口外的压成摘要，再外面的彻底不留
MEMORY_CONCLUSION_CHARS = 120  # 每条 conclusion 的字符上限：analysis 正文上千字，必须归一
MEMORY_FACTS_MAX = 8           # 查数摘要条数上限（只含最近一轮）
MEMORY_RESEARCH_MAX = 3        # 外部检索条目更少：复用价值低于业务数字（§4.1）
MEMORY_SUMMARY_CHARS = 200     # 摘要文本上限（§4.5）：它是"旧闻"，不许和最近一轮抢预算
MEMORY_CHARS_MAX = 3000        # 整个 memory_context 序列化后的字符硬上限
_MEMORY_FIELD_CHARS = 200      # 单条结论文本（数据/调研）的截断长度
_MEMORY_SUMMARY_Q_CHARS = 24   # 摘要里每个问题的截断长度：问题原文常带客套前缀，取主干就够
_MEMORY_SUMMARY_HEAD_CHARS = 12  # 摘要前缀「更早 N 轮略，」的预算预留（见 digest_rounds）

_MEMORY_ARGS_CHARS = 80        # facts 条目里工具参数的截断长度

# ---- 各节点注入哪几条线（docs/11 §5 表；常量放这里，节点与测试共用一份）----
# 谁需要什么，判据是「它做的事用不用得上」，不是「它属于哪一层」：
#   supervisor 只拆任务 → 历轮结论 + 已查过什么（避免重复派活）；不给报告摘要，拆任务用不上
#   business_analyst 出分析 → 上面两条 + 外部背景（分析要同时看内/外材料）
#   report 写最终报告 → 上一轮报告（产增量报告）+ 历轮结论；不给明细，它不判断查没查过
# data_analyst / research 不注入：它们是执行查数的，"该不该再查"由 supervisor 判断；
# reviewer 不注入：每轮独立重判，喂记忆等于给它立场，回环就失去意义（§4.1）。
# summary（§4.5 的更早轮次摘要）三个节点都给：它只回答"这个任务最初在问什么、一路怎么
# 收窄下来"，是三者共同的背景，不像 facts 那样分"用不用得上"。
# 元组内的顺序不代表渲染顺序（render_block 里是固定的 if 序列），只作声明。
SUPERVISOR_INCLUDE = ("summary", "history", "facts")
BUSINESS_ANALYST_INCLUDE = ("summary", "history", "facts", "research_briefs")
REPORT_INCLUDE = ("summary", "report_brief", "history")


def empty_stats() -> dict:
    """无记忆开局也要有账可查（§7：降级必须留痕，否则"记忆怎么又没了"无从排查）。"""
    return {
        "injected": False,
        "history_rounds": 0,
        "facts": 0,
        "research": 0,
        "archived_rounds": 0,   # 滚出窗口、被压进摘要的轮数（§4.5）
        "summary_chars": 0,     # 摘要实际字符数（0 = 本轮没有更早的轮次可压）
        "summary_source": None,  # "model" | "digest" | None：这轮摘要谁写的（§4.5 降级留痕）
        "truncated": False,
        "degraded_rounds": [],
    }


def _size(obj) -> int:
    """字符数近似而不是精确 token：算准要引 tokenizer 依赖，这里只要一条硬上限护栏（§4.3）。"""
    return len(json.dumps(obj, ensure_ascii=False, default=str))


def clip_text(value, limit: int) -> str:
    """任意快照字段 → 截断后的纯文本。结构不符当空处理，绝不让一轮坏数据炸整条链。

    公开名（不带下划线）：Task 5 的记忆视图端点也复用它截结论，
    截断口径只有一份，不会「记忆里的 120 字」和「页面上的 120 字」不是一回事。
    """
    if isinstance(value, str):
        return value.strip()[:limit]
    if isinstance(value, dict):
        return str(value.get("content") or "").strip()[:limit]
    return ""


def _brief_calls(items: list, *, limit: int) -> list[str]:
    """data_results / research_results → 人话摘要（不搬原始行，§4.1）。

    节点实现决定了 `{"tool": "conclusion", ...}` 恒在列表末尾（见 data_analyst.py
    循环后那次 append），所以 `[-limit:]` 截断天然保住了结论行、丢的是最旧的调用行。
    """
    out: list[str] = []
    for r in items or []:
        if not isinstance(r, dict):
            continue
        if r.get("tool") == "conclusion":
            text = clip_text(r.get("text"), _MEMORY_FIELD_CHARS)
            if text:
                out.append(f"数据结论：{text}")
            continue
        # rows 的形状不能赌：真快照里它是**行数**（ToolResult.rows: int | None，
        # registry 统一填 len(...)），而老数据的 JSONB 完全可能是原始行列表。
        # 直接 len(rows) 会在 ok=True & rows=4 时抛 TypeError —— 追问端点当场 500，
        # 这正是 §7「绝不让一轮坏数据炸整条链」要挡的事。
        rows = r.get("rows")
        if not r.get("ok"):
            status = "失败"
        elif isinstance(rows, int):
            status = f"{rows} 行"
        elif isinstance(rows, list):
            status = f"{len(rows)} 行"
        else:
            status = "已返回"   # 不报行数的工具（如 calculator），只说明调用发生过
        args = json.dumps(r.get("args") or {}, ensure_ascii=False, default=str)[:_MEMORY_ARGS_CHARS]
        out.append(f"{r.get('tool')}({args}) → {status}")
    return out[-limit:]


def _brief_research(items: list, *, conclusion_label: str) -> list[str]:
    """research_results 同 _brief_calls，只是结论行换个标签（模型要分得清内/外材料）。"""
    briefs = _brief_calls(items, limit=MEMORY_RESEARCH_MAX)
    return [b.replace("数据结论：", conclusion_label, 1) if b.startswith("数据结论：") else b
            for b in briefs]


def _report_brief(report: dict | None) -> dict | None:
    """报告只保三项（§4.1）：摘要 + 发现 + 建议。data_evidence/sources 是旧数字的重灾区，不带。"""
    if not isinstance(report, dict):
        return None
    brief = {
        "executive_summary": clip_text(report.get("executive_summary"), _MEMORY_FIELD_CHARS),
        "key_findings": [clip_text(x, 120) for x in (report.get("key_findings") or [])[:3]],
        "recommendations": [clip_text(x, 120) for x in (report.get("recommendations") or [])[:3]],
    }
    return brief if (brief["executive_summary"] or brief["key_findings"]) else None


def _usable_runs(
    prev_runs: list[dict], *, warn: bool = True
) -> tuple[list[dict], list[dict], list[int | None]]:
    """一次遍历出两样东西：进结论线的轮次、最近一个可用快照、坏掉的轮号。

    结论线要的是「有结论的轮」，明细线要的是「最近一轮」——两者不一定是同一轮：
    某轮只查了数没出分析时，明细仍可用，但它不进 history（§7：空结论只会占预算、
    还会给模型"上一轮什么也没得出"的误导）。

    warn=False 只给 `archived_rounds()` 用：摘要原料要扫一遍、正式装配还要扫一遍，
    坏快照只该留一条 warning（两遍都打就成了噪声，而没有第二条信息）。
    """
    rounds: list[dict] = []
    detail_src: list[dict] = []
    degraded: list[int | None] = []
    for run in prev_runs or []:
        # 仓储已只取 completed；这里再挡一次是第二道闸：failed 快照照写（§7），
        # 半截结论传下去会一路污染后续所有轮次，宁可无记忆。
        if run.get("status") != "completed":
            continue
        state = run.get("state")
        if not isinstance(state, dict):
            # §7 要求降级留痕两处都有：轮号进 stats（落库，供事后查），warning 进日志（当场看）。
            # 只留一份的话，"记忆怎么又没了"要么翻不到、要么得解 JSONB 才知道。
            degraded.append(run.get("run_no"))
            if warn:
                logger.warning(
                    "第 %s 轮快照不可用（state 类型 %s），本轮记忆跳过它",
                    run.get("run_no"), type(state).__name__,
                )
            continue
        detail_src.append(state)
        conclusion = clip_text(state.get("analysis"), MEMORY_CONCLUSION_CHARS)
        if not conclusion:
            continue
        rounds.append({
            "round": run.get("run_no"),
            "question": clip_text(state.get("question"), MEMORY_CONCLUSION_CHARS),
            "conclusion": conclusion,
        })
    return rounds, detail_src, degraded


def _split_window(rounds: list[dict]) -> tuple[list[dict], list[dict]]:
    """结论线切成两段：滚出窗口的（旧，进摘要）+ 窗口内的（近，原样保留）。

    窗口口径只有这一处：`build_memory` 拿它决定 `history` 与摘要的原料，别处再判一次
    就会长出"摘要按 A 口径、history 按 B 口径"的两套窗口。
    """
    if len(rounds) <= MEMORY_ROUNDS_MAX:
        return [], rounds
    return rounds[:-MEMORY_ROUNDS_MAX], rounds[-MEMORY_ROUNDS_MAX:]


def archived_rounds(prev_runs: list[dict]) -> list[dict]:
    """滚出窗口、等着被摘要的那几轮（纯函数，入参与 `build_memory` 同形）。

    公开它的唯一理由是**分工**：调用方要先把这几轮交给模型写摘要（`memory_summary`），
    再把结果文本回填给 `build_memory(archived_summary=…)`。如果让调用方自己判「哪几轮
    算窗口外」，同一件事就会有两份判据，改窗口时必漏一处。
    """
    rounds, _, _ = _usable_runs(prev_runs, warn=False)
    return _split_window(rounds)[0]


def digest_rounds(archived: list[dict]) -> str:
    """滚出窗口的旧轮次 → 一行主线摘要（§4.5 摘要压缩的规则实现，纯函数、可单测）。

    只留**问题链**、不留结论，两个原因：
      · 结论每条就 120 字，四轮压不进 200 字里，勉强压只剩半截判断（比没有更危险）；
      · 摘要要回答的是"这个任务最初在问什么、一路怎么收窄下来的"，那正是问题链的形状。
        结论由窗口内的 history 负责，分工不重叠。

    装不下时**从新往旧装**：离窗口最近的那几轮先留（它们和本轮问题最相关），
    更旧的点名"略过 N 轮"——丢内容可以，丢得无声不行。
    """
    return _pack_summary(archived)[0]


def _pack_summary(archived: list[dict]) -> tuple[str, list[int]]:
    """摘要的实体：返回 (文本, **真正写进文本的**轮号)。

    为什么要把轮号一起交出去：记忆视图靠它算 `reused_by_next`，而"滚出窗口"不等于
    "还留在摘要里"——被"更早 N 轮略"吃掉的轮次并没有被下一轮继承，按 archived 全量
    标记会让页面把丢掉的轮次说成复用过的。
    """
    items = [
        (r.get("round"), (r.get("question") or "").strip()[:_MEMORY_SUMMARY_Q_CHARS])
        for r in (archived or [])
    ]
    items = [(rn, q) for rn, q in items if q]        # 没记问题的轮次不进摘要（同 §7 空结论不收）
    if not items:
        return "", []
    # 前缀「更早 N 轮略，」也要占预算：不预留就会写出 204 字的摘要去撞 200 字上限
    # （第 8 轮以后 dropped 是两位数，预留 12 字还留了余量）。
    budget = MEMORY_SUMMARY_CHARS - _MEMORY_SUMMARY_HEAD_CHARS
    kept: list[tuple[int, str]] = []
    used = 0
    for rn, q in reversed(items):
        if used + len(q) + 3 > budget:                # +3 是 " → " 分隔符
            break
        kept.append((rn, q))
        used += len(q) + 3
    kept.reverse()
    dropped = len(items) - len(kept)
    span = (
        f"第 {kept[0][0]}-{kept[-1][0]} 轮"
        if len(kept) > 1 and kept[0][0] != kept[-1][0]
        else f"第 {kept[0][0]} 轮"
    )
    head = f"更早 {dropped} 轮略，" if dropped else ""
    text = f"{head}{span}主线：{' → '.join(q for _, q in kept)}"
    return text[:MEMORY_SUMMARY_CHARS], [rn for rn, _ in kept]


def _apply_budget(mc: dict) -> bool:
    """超预算时按固定顺序裁（§4.3）：越靠近本轮的内容越不可替换。

    1) 整轮整轮丢最旧的 history（保留最近几轮的完整叙事比每轮留半截有用）
    2) 丢最旧的 facts 条目，再丢 research_briefs（明细线内部也是旧的先丢）
    3) 丢 summary（§4.5）——它排在明细线之后：全场只有它是"这轮丢了就再也补不回来"的
       （明细都在最近一轮快照里，下一轮还能重算），而且它只有 200 字上限、最省预算
    4) 最后才对最近一轮的 conclusion 折半截短
    只保留最后一条 history 的最低限度：追问链的"我在接着问什么"不能整条消失。
    """
    truncated = False
    while _size(mc) > MEMORY_CHARS_MAX and len(mc["history"]) > 1:
        mc["history"].pop(0)
        truncated = True
    while _size(mc) > MEMORY_CHARS_MAX and mc["facts"]:
        mc["facts"].pop(0)
        truncated = True
    while _size(mc) > MEMORY_CHARS_MAX and mc["research_briefs"]:
        mc["research_briefs"].pop(0)
        truncated = True
    while _size(mc) > MEMORY_CHARS_MAX and mc.get("summary"):
        # 摘要与 summary_rounds 必须同生同死：只清文本会让记忆视图把"已被裁掉的轮次"
        # 仍然标成 reused_by_next，页面说谎比少显示更糟。
        mc["summary"] = None
        mc["summary_rounds"] = []
        truncated = True
    while _size(mc) > MEMORY_CHARS_MAX and mc["history"]:
        last = mc["history"][-1]
        text = last["conclusion"]
        if len(text) <= 20:
            break
        last["conclusion"] = text[: max(20, len(text) // 2)]
        truncated = True
    mc["truncated"] = truncated
    return truncated


def build_memory(
    prev_runs: list[dict], *, round_no: int, archived_summary: str | None = None
) -> dict:
    """组装本轮记忆（§4.2 / §4.5 / §6）。

    prev_runs：最近 `MEMORY_RECALL_ROUNDS` 条 run 的**普通 dict**（旧→新），每项
        `{"run_no": int, "status": str, "state": dict | None}`。
        不收 ORM 对象：收 ORM 就没法零依赖单测，也会把 sqlalchemy 拽进 AI 层。
        **要比 MEMORY_ROUNDS_MAX 多取**：窗口外那几轮是摘要的原料，取 4 条就只能
        永远丢掉"这任务最初在问什么"（§4.5）。
    round_no：本轮是第几轮，来自 `task_repo.next_run_no()`（**绝不是 retry_count**）。
    archived_summary：模型写好的摘要文本（§4.5 的第一档）。传 None / 空串就走规则兜底
        `digest_rounds`——**不传不是错**，它就是"这轮没调模型"的正常形状：单测、记忆视图、
        模型失败三条路径都走它。调用方若传了但窗口外没有轮次，这份文本会被忽略（没有原料
        就谈不上摘要，收下它只会让摘要声称覆盖了不存在的轮次）。

    返回 `{"context": memory_context | None, "stats": {...}}`：
        context 注入 `TaskState.memory_context`；stats 落 `task_runs.meta.memory`。
    """
    rounds, detail_src, degraded = _usable_runs(prev_runs)
    archived, window = _split_window(rounds)
    latest = detail_src[-1] if detail_src else None
    mc: dict | None = None
    source: str | None = None
    if window or latest:
        model_text = clip_text(archived_summary, MEMORY_SUMMARY_CHARS) if (archived_summary and archived) else ""
        if model_text:
            # 模型摘要只回文本、不回轮号，所以按 archived 全量记：它讲的是"更早那几轮的
            # 主线"，不是其中某个子集（规则版才会因为装不下而只覆盖一部分）。
            summary, summary_rounds, source = model_text, [r["round"] for r in archived], "model"
        else:
            summary, summary_rounds = _pack_summary(archived)
            source = "digest" if summary else None
        mc = {
            "round": round_no,
            # 摘要压缩（§4.5）：滚出窗口的轮次压成一行，history 仍是最近几轮的完整叙事
            "summary": summary or None,
            "summary_rounds": summary_rounds,
            "history": window,
            # 明细线：只认最近一个可用快照
            "facts": _brief_calls(latest.get("data_results"), limit=MEMORY_FACTS_MAX) if latest else [],
            "research_briefs": (
                _brief_research(latest.get("research_results"), conclusion_label="调研结论：") if latest else []
            ),
            "report_brief": _report_brief(latest.get("report")) if latest else None,
            "truncated": False,
        }
        if _apply_budget(mc):
            mc["truncated"] = True
        if not mc["summary"]:
            # 预算把摘要整条裁掉了：`summary_source` 必须跟着归零，否则 meta 里写着
            # "这轮摘要是模型写的"、正文却是空的 —— 事后排查会去追一个不存在的段落。
            source = None
        if not (mc["history"] or mc["facts"] or mc["research_briefs"]
                or mc["report_brief"] or mc["summary"]):
            mc = None   # 什么都没继承到就别宣称"有记忆"，否则 stats.injected 骗人

    stats = empty_stats()
    if mc:
        stats.update({
            "injected": True,
            "history_rounds": len(mc["history"]),
            "facts": len(mc["facts"]),
            "research": len(mc["research_briefs"]),
            "archived_rounds": len(mc["summary_rounds"]),
            "summary_chars": len(mc["summary"] or ""),
            "summary_source": source,
            "truncated": bool(mc["truncated"]),
        })
    stats["degraded_rounds"] = degraded
    return {"context": mc, "stats": stats}



# ---- prompt 文本渲染 ----

_BLOCK_LABELS = {
    "summary": "【更早轮次摘要】",
    "history": "【历轮结论】",
    "facts": "【上一轮已查过的数据】",
    "research_briefs": "【上一轮已检索的外部背景】",
    "report_brief": "【上一轮报告摘要】",
}


def render_block(memory_context: dict | None, *, include: tuple[str, ...]) -> str:
    """memory_context → 追加到 SystemMessage 尾部的一段文本；**无记忆时返回 ""**。

    返回空串是硬要求（§5）：第一轮必须和现状逐字节同构，否则本 Phase 之外的
    评测基线、Prompt 对比全都被"追加了一段新文字"污染。
    各节点传不同的 include（§5 表），这里只负责"怎么说"，不负责"说什么该给谁"。

    块序固定为 摘要 → 历轮结论 → 明细：摘要里的轮次**比 history 更早**，
    顺序反过来模型会以为摘要是最新的。
    """
    if not memory_context:
        return ""
    lines: list[str] = []
    if "summary" in include and memory_context.get("summary"):
        lines.append(_BLOCK_LABELS["summary"])
        lines.append(f"- {memory_context['summary']}")
    if "history" in include and memory_context.get("history"):
        lines.append(_BLOCK_LABELS["history"])
        for h in memory_context["history"]:
            lines.append(f"- 第 {h['round']} 轮 · {h['question']}：{h['conclusion']}")
    if "facts" in include and memory_context.get("facts"):
        lines.append(_BLOCK_LABELS["facts"])
        lines.extend(f"- {f}" for f in memory_context["facts"])
    if "research_briefs" in include and memory_context.get("research_briefs"):
        lines.append(_BLOCK_LABELS["research_briefs"])
        lines.extend(f"- {r}" for r in memory_context["research_briefs"])
    if "report_brief" in include and memory_context.get("report_brief"):
        brief = memory_context["report_brief"]
        lines.append(_BLOCK_LABELS["report_brief"])
        if brief.get("executive_summary"):
            lines.append(f"- 摘要：{brief['executive_summary']}")
        for f in brief.get("key_findings") or []:
            lines.append(f"- 发现：{f}")
        for rec in brief.get("recommendations") or []:
            lines.append(f"- 建议：{rec}")
    # 只剩标签没有内容 = 空标题污染材料（同 business_analyst 里 research_block 的判据）
    if not any(not ln.startswith("【") for ln in lines):
        return ""
    return f"\n\n{MEMORY_HEADER}\n" + "\n".join(lines) + f"\n\n{MEMORY_FOOTER}"
