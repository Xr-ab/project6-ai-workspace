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
MEMORY_CONCLUSION_CHARS = 120  # 每条 conclusion 的字符上限：analysis 正文上千字，必须归一
MEMORY_FACTS_MAX = 8           # 查数摘要条数上限（只含最近一轮）
MEMORY_RESEARCH_MAX = 3        # 外部检索条目更少：复用价值低于业务数字（§4.1）
MEMORY_CHARS_MAX = 3000        # 整个 memory_context 序列化后的字符硬上限
_MEMORY_FIELD_CHARS = 200      # 单条结论文本（数据/调研）的截断长度

_MEMORY_ARGS_CHARS = 80        # facts 条目里工具参数的截断长度

# ---- 各节点注入哪几条线（docs/11 §5 表；常量放这里，节点与测试共用一份）----
# 谁需要什么，判据是「它做的事用不用得上」，不是「它属于哪一层」：
#   supervisor 只拆任务 → 历轮结论 + 已查过什么（避免重复派活）；不给报告摘要，拆任务用不上
#   business_analyst 出分析 → 上面两条 + 外部背景（分析要同时看内/外材料）
#   report 写最终报告 → 上一轮报告（产增量报告）+ 历轮结论；不给明细，它不判断查没查过
# data_analyst / research 不注入：它们是执行查数的，"该不该再查"由 supervisor 判断；
# reviewer 不注入：每轮独立重判，喂记忆等于给它立场，回环就失去意义（§4.1）。
SUPERVISOR_INCLUDE = ("history", "facts")
BUSINESS_ANALYST_INCLUDE = ("history", "facts", "research_briefs")
REPORT_INCLUDE = ("report_brief", "history")


def empty_stats() -> dict:
    """无记忆开局也要有账可查（§7：降级必须留痕，否则"记忆怎么又没了"无从排查）。"""
    return {
        "injected": False,
        "history_rounds": 0,
        "facts": 0,
        "research": 0,
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


def _usable_runs(prev_runs: list[dict]) -> tuple[list[dict], list[dict], list[int | None]]:
    """一次遍历出两样东西：进结论线的轮次、最近一个可用快照、坏掉的轮号。

    结论线要的是「有结论的轮」，明细线要的是「最近一轮」——两者不一定是同一轮：
    某轮只查了数没出分析时，明细仍可用，但它不进 history（§7：空结论只会占预算、
    还会给模型"上一轮什么也没得出"的误导）。
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


def _apply_budget(mc: dict) -> bool:
    """超预算时按固定顺序裁（§4.3）：越靠近本轮的内容越不可替换。

    1) 整轮整轮丢最旧的 history（保留最近几轮的完整叙事比每轮留半截有用）
    2) 丢最旧的 facts 条目，再丢 research_briefs（明细线内部也是旧的先丢）
    3) 最后才对最近一轮的 conclusion 折半截短
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
    while _size(mc) > MEMORY_CHARS_MAX and mc["history"]:
        last = mc["history"][-1]
        text = last["conclusion"]
        if len(text) <= 20:
            break
        last["conclusion"] = text[: max(20, len(text) // 2)]
        truncated = True
    mc["truncated"] = truncated
    return truncated


def build_memory(prev_runs: list[dict], *, round_no: int) -> dict:
    """组装本轮记忆（§4.2 / §6）。

    prev_runs：最近 N 条 run 的**普通 dict**（旧→新），每项
        `{"run_no": int, "status": str, "state": dict | None}`。
        不收 ORM 对象：收 ORM 就没法零依赖单测，也会把 sqlalchemy 拽进 AI 层。
    round_no：本轮是第几轮，来自 `task_repo.next_run_no()`（**绝不是 retry_count**）。

    返回 `{"context": memory_context | None, "stats": {...}}`：
        context 注入 `TaskState.memory_context`；stats 落 `task_runs.meta.memory`。
    """
    rounds, detail_src, degraded = _usable_runs(prev_runs)
    latest = detail_src[-1] if detail_src else None
    mc: dict | None = None
    if rounds or latest:
        mc = {
            "round": round_no,
            "history": rounds[-MEMORY_ROUNDS_MAX:],
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
        if not (mc["history"] or mc["facts"] or mc["research_briefs"] or mc["report_brief"]):
            mc = None   # 什么都没继承到就别宣称"有记忆"，否则 stats.injected 骗人

    stats = empty_stats()
    if mc:
        stats.update({
            "injected": True,
            "history_rounds": len(mc["history"]),
            "facts": len(mc["facts"]),
            "research": len(mc["research_briefs"]),
            "truncated": bool(mc["truncated"]),
        })
    stats["degraded_rounds"] = degraded
    return {"context": mc, "stats": stats}


# ---- prompt 文本渲染 ----

_BLOCK_LABELS = {
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
    """
    if not memory_context:
        return ""
    lines: list[str] = []
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
