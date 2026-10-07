"""评测指标口径（Phase 6 Evaluation，docs/08 §4）。

**全仓唯一的指标计算处** —— 纯函数、只吃已展平的行，不 import sqlalchemy、不碰模型、
不调模型。为什么这么切：口径是"产品对质量的定义"，它会被人反复追问（面试、评审、
半年后的自己）。凡是和取数混在一起的口径都没法单测，也就没人敢改。

`docs/06` §2.7 那六个键名逐字保留（是接口契约），`docs/08` §4 其余指标加在旁边。
"""
from __future__ import annotations

import math

# §5.3 分档语义里 3 才是"基本正确"，所以成功阈值取 3（含）。
# 这个数一旦改，Task 8 的基线指标全体漂移 —— 故写死并让单测盯着（test_eval_metrics 第 2 段）。
QUALITY_PASS_SCORE = 3

# docs/08 §2 的类别 → 需要哪些指标才有源。缺源时指标出 null 并登记进 not_applicable，
# 绝不静默省略：省略会被读成"算过了、是 0"。
CATEGORY_REQUIRED = {
    "rag": ("retrieval_quality",),
}

# ---------------- diff 的方向语义（W2：三处登记，一份真相） ----------------

# 方向登记**必须显式成两张表**：修前只有 LOWER_IS_BETTER，diff 项里的
# lower_is_better 是 `k in LOWER_IS_BETTER` —— 于是"没登记方向的键"读起来等于
# "越大越好"，`reviewer_miss_rate`（漏检率，越小越好）就在这一族里被渲成
# "100%→50% 的改善 = ▼变差"（Task 8 实测记进 docs/10 遗留 7，本期收掉）。
#
# 方向语义：这些指标越小越好，diff 的着色与箭头要反过来（前端靠它，别自己猜）
LOWER_IS_BETTER = frozenset({
    "latency_avg_ms", "latency_p50_ms", "latency_p95_ms",
    "total_tokens", "total_prompt_tokens", "total_completion_tokens", "total_cost",
    # W2 补：Reviewer 漏检率 = reviewer 放行但最终不过的占比，越低说明 reviewer 越可信
    "reviewer_miss_rate",
})

# 越大越好的键 —— **按 compute_metrics 实际产出的键对着抄**，别把计数类塞进来。
# 计数类（cases_total / cases_passed / cases_error / reviewed_cases / quality_pass_score）
# 与元信息类（category_scope / not_applicable / cost_available / failure_category_dist）
# 故意两张表都不进：它们的"变大"不是价值判断
# （cases_total 变多是加了用例、reviewed_cases 变多既可能好也可能坏、
#   failure_category_dist "更大"可能只是换了构成），所以它们走
# direction_registered=false → 前端渲"方向未登记"中性灰，绝不给绿/红。
HIGHER_IS_BETTER = frozenset({
    "task_success_rate", "tool_success_rate", "agent_completion_rate",
    "reviewer_error_detection_rate", "final_answer_quality_avg", "retrieval_quality",
})

# diff 项 dir 的全部取值（生产侧的取值域；schemas/evaluation.py 那份是契约声明，
# test_evaluations_api 有一条断言钉两者不许漂移）。
# up/down/same 来自"量出来的数值"；missing_* = 单边缺席；
# W2 新增两支（原来全被 `else: same` 吞掉，于是"没量出来"被渲成"持平"）：
#   unmeasured = 两侧键都在、但不是两侧都量出了有限数值（任一侧为 None）——
#                retrieval_quality 两侧都 null 是"这个指标没测过"，不是"两轮一样好"
#   changed    = 两侧都在、都不是 null、但不是两个可减数值且**值不相等**（cost_available
#                True→False、not_applicable 变长、failure_category_dist 换构成，
#                以及一侧数值一侧列表这种混形）——
#                变了，但方向无意义，所以既不给绿也不给红
#   same       = 两侧都在且值相等（旗标确实没变，这才是真持平）
DIFF_DIRS = ("up", "down", "same", "missing_base", "missing_head",
             "unmeasured", "changed")


def _is_number(v: object) -> bool:
    """真数值判据：int/float、**排除 bool**（bool 是 int 的子类，True 会混进减法），
    并且必须是有限值（NaN/inf 进不了 JSON，但从内存里直接 diff 时不许量出 delta）。"""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def _case_sort_key(no: object) -> tuple[int, str]:
    """case_no 的排序键（W3）：case_results 的键穿过 JSONB 回来是**数字串**，
    按字符串排会让 '10' 排在 '9' 前面 —— 翻转列表与覆盖列表都是给人按编号读的，
    倒置的编号列比不排序更坏。能转 int 的按数值排（0 组，同值再按原串稳定兜底），
    转不了的（case_no 是 NOT NULL 纯数字，理论不存在）退到 1 组不抛 ——
    排序属展示职责，不许因此把整次对比炸掉（判据同 service 那边"不加 try 防御"的
    反面：这里不是取值而是排序，炸与不炸没有正确性差别，那就选不炸）。"""
    try:
        return (0, f"{int(no):012d}")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (1, str(no))


# failure_category_dist 里「未通过但链路侧没有失败分类」那一桶的键名（W1 / 后端 C-1）。
# 为什么必须有这一桶而不是让它消失：dist 读的人拿它数"这批失败了几条、各是什么病"，
# 而 judge 判不过的用例（score < 阈值、链路全绿）在链路侧就是没有分类 ——
# 不补一个桶，sum(dist) 就小于真正的失败数，这个键当量"失败分布"就是在少报。
JUDGE_FAILED_KEY = "judge_failed"


def percentile(values: list[int], pct: float) -> float | None:
    """线性插值分位（§4「分位：p50 / p95」）。空集合返回 None，不返回 0。

    口径写死成线性插值而不是"取第 k 个"：两条真 Run 各 10 条用例时，取整下标会让
    p95 恒等于第 10 个（最大值），基线之间的抖动全被最大值吃掉。
    """
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    pos = (len(vals) - 1) * (pct / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def score_case(*, status: str, score: int | None) -> bool:
    """单条用例是否 passed（§4：成功 = 跑完 且 质量分 ≥ 阈值）。

    status != 'ok' 直接否：链路炸了的执行没有"质量"可言，给它打分等于把故障算成能力。
    score 为 None（judge 没出分）也否 —— 宁可低估不虚报。
    """
    if status != "ok":
        return False
    if score is None:
        return False
    return score >= QUALITY_PASS_SCORE


def _avg_or_none(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def compute_metrics(rows: list[dict], *, category_scope: list[str]) -> dict:
    """把一批用例结果算成 `evaluation_runs.metrics`（docs/08 §4 全量口径）。

    入参 rows 的每个键都用 `.get()` 读且带默认值：坏行不许抛（§7 同一条纪律 —— 一条
    脏数据把整次评测搞炸，比少算一个指标严重得多）。
    """
    total = len(rows)
    passed = sum(1 for r in rows if r.get("passed"))
    tool_total = sum(r.get("tool_total") or 0 for r in rows)
    tool_ok = sum(r.get("tool_ok") or 0 for r in rows)
    latencies = [r.get("latency_ms") for r in rows if r.get("latency_ms") is not None]
    prompt = sum(r.get("prompt_tokens") or 0 for r in rows)
    completion = sum(r.get("completion_tokens") or 0 for r in rows)
    # cost 的来路有两条：DB 列 numeric(12,6) 读回是 Decimal，pricing.compute_cost 算出是
    # float。同一个 sum() 里 Decimal + float 会抛 TypeError，Decimal 也进不了 JSONB ——
    # 读入即归一成 float。本层只加和不判价，归一不动任何口径数值与键名。
    costs = [None if (c := r.get("cost")) is None else float(c) for r in rows]
    # 任一行为 None（未配单价）即整体不可得：混合"有值 + 无值"多半是数据脏，
    # 报出一个偏小的总数比报 None 更危险（会让人以为这轮真这么便宜）。
    cost_available = total > 0 and all(c is not None for c in costs)
    # ---- failure_category_dist 的口径（W1 / 后端 C-1）----
    # 这个键的名字是"失败**类别**分布"，所以它只许累计未通过的用例。
    # 修前的错账：无条件按 r["failure_category"] 计数，而 evaluation_service 会把
    # task_runs.failure_category 覆盖到结果行上 —— 一条**通过**的用例只要链路上有过
    # 一次被打回重试（review_timeout 之类），它就被计进"失败分布"。基线 run
    # 99c7a710 实测：case 6 status=ok / passed=t / score=3 却带 review_timeout，
    # 落库 dist = {internal_error:3, review_timeout:1} = 4 个"失败"，
    # 而同一条 metrics 里 task_success_rate=0.7（真失败 3 个）—— 同一个对象自相矛盾。
    #
    # **不变式（这条才是"dist 有没有说谎"的可检仪器）**：
    #     sum(dist.values()) == cases_total - cases_passed
    # 成立的两半：① 通过的行一条都不进（`and not r.get("passed")`）；
    #             ② 未通过但链路侧没分类的行必须进 JUDGE_FAILED_KEY 桶 ——
    #               judge 判不过的用例链路全绿，在 failure_category 上就是 None，
    #               不补这一桶则 sum 少算、不变式破、"这批评了 3 条"读成"2 条"。
    # test_eval_metrics 第 3/3b 段把这条不变式钉成断言（含构造的"通过却带链路分类"行）。
    dist: dict[str, int] = {}
    for r in rows:
        if r.get("passed"):
            continue
        cat = r.get("failure_category") or JUDGE_FAILED_KEY
        dist[cat] = dist.get(cat, 0) + 1
    agent_clean = sum(1 for r in rows if not (r.get("agent_error_nodes") or 0))
    reviewed = [r for r in rows if r.get("reviewer_verdict")]
    caught = [r for r in reviewed if r.get("reviewer_verdict") != "pass"]
    # 漏检：reviewer 放行（pass）但最终质量不过 —— 这是"信任 reviewer 能不能省人"的唯一依据
    missed = [r for r in reviewed
              if r.get("reviewer_verdict") == "pass" and not r.get("passed")]
    scores = [r.get("score") for r in rows if r.get("score") is not None]

    metrics = {
        # ---- docs/06 §2.7 的六个键，逐字不动 ----
        "task_success_rate": round(passed / total, 4) if total else None,
        "tool_success_rate": round(tool_ok / tool_total, 4) if tool_total else None,
        "latency_avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "total_tokens": prompt + completion,
        "total_cost": round(sum(costs), 6) if cost_available else None,
        "failure_category_dist": dist,
        # ---- docs/08 §4 其余口径 ----
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "total_prompt_tokens": prompt,
        "total_completion_tokens": completion,
        "agent_completion_rate": round(agent_clean / total, 4) if total else None,
        "reviewer_error_detection_rate": (round(len(caught) / len(reviewed), 4)
                                         if reviewed else None),
        "reviewer_miss_rate": round(len(missed) / len(reviewed), 4) if reviewed else None,
        "final_answer_quality_avg": round(_avg_or_none(scores), 4) if scores else None,
        # 本期不覆盖 rag → 恒 None（见 Global Constraint 3）
        "retrieval_quality": _ratio_true(rows, "retrieval_hit") if "rag" in category_scope else None,
        # ---- 计数与元信息：前端与回归对比都要用，也让"这个数是从几条用例来的"可见 ----
        "cases_total": total,
        "cases_passed": passed,
        "cases_error": sum(1 for r in rows if r.get("status") == "error"),
        # 被审样本数：拦截率/漏检率的分母。不给出来的话 0.25 到底是 1/4 还是 3/12 看不出来
        "reviewed_cases": len(reviewed),
        "cost_available": cost_available,
        "quality_pass_score": QUALITY_PASS_SCORE,
        "category_scope": sorted(category_scope),
        "not_applicable": sorted(
            name for cat, names in CATEGORY_REQUIRED.items()
            if cat not in category_scope for name in names
        ),
        # 用例级明细（diff 的左半边）：{case_no: passed}
        "case_results": {r.get("case_no"): bool(r.get("passed")) for r in rows},
    }
    return metrics


def _ratio_true(rows: list[dict], key: str) -> float | None:
    """某布尔字段的命中率（Retrieval Quality 的口径：命中引用的用例数 / RAG 用例数）。"""
    sample = [r for r in rows if r.get(key) is not None]
    if not sample:
        return None
    return round(sum(1 for r in sample if r.get(key)) / len(sample), 4)


# 逐用例翻转不进指标的 delta 表，单独列出来：docs/08 §6 的"哪些用例从 pass→fail"
# （W3 起旁边还挂一个 case_coverage，说明这份 flips 是在多大的可比域上算出来的）
def diff_metrics(base: dict, head: dict) -> dict:
    """两次 Run 的指标 diff（docs/08 §6 回归对比）。任一侧缺键就当 None，不抛。

    取键口径（计划裁定 D14）：遍历 base/head 的**键并集**、sorted 定序 —— 只遍历 head
    会让"基线里有、这轮消失了"的指标从 diff 里整个蒸发，Task 7 的回归表就看不见它。
    单边缺席标 missing_base / missing_head，**不复用 same**："持平"只能来自真实 delta=0，
    把缺席渲成持平等于向人承诺"没有回归"。这与 Global Constraint 2 对 retrieval_quality
    "不许静默省略"是同一条纪律。缺席键的 delta 恒 None（0 是量出来的，None 是没量）。
    两侧都在但不是两个有限数值时，W2 起拆成三支（原来一支 `else: same` 全包，
    等于把"没量出来"与"变了"都报成"持平"）：
        任一侧为 None            → unmeasured（没量出来，持平不成立）
        都非 null 而值不相等      → changed  （变了，方向无意义，不给绿也不给红）
        都非 null 而值相等        → same     （旗标确实没变，这才是真持平）
    即"键在而值为 None"**不再**算 same —— 缺席与未量出是两个诚实程度不同的陈述。

    返回除逐指标 diff 外还有两个**结构性键**（不是指标、没有 dir，渲染端按身份特判）：
        case_flips    两侧都跑过且 pass/fail 变了的那些条（case_no 在 service 归一为 int）
        case_coverage W3：flips 是在多大的可比域上算出来的 —— 只在单边出现的用例
                      在上面那一句 `b is not None and h is not None` 里被跳过，
                      不登记出来就等于对覆盖收缩说"没有翻转"。
    """
    out: dict = {}
    keys = sorted({k for k in base if k != "case_results"}
                  | {k for k in head if k != "case_results"})
    for k in keys:
        b, h = base.get(k), head.get(k)
        in_base, in_head = k in base, k in head
        if in_base and in_head and _is_number(b) and _is_number(h):
            delta = round(h - b, 6)
            direction = "up" if delta > 0 else ("down" if delta < 0 else "same")
        elif not in_base:
            delta, direction = None, "missing_base"   # base 没这指标：新增，不是持平
        elif not in_head:
            delta, direction = None, "missing_head"   # head 没这指标：消失，不是持平
        elif b is None or h is None:
            # W2（后端 I-1）第 ① 支：两侧键都在，但**任一侧是 None**（没量出来）。
            # 修前落进 `else: same` —— 于是 retrieval_quality 两侧都 null 被渲成
            # "持平"，读表的人以为"这个指标两轮一样"，真相是"两轮都没测过它"。
            # None 与"非数值的有值"（False / [] / {}）必须分开：前者是没量，后者是量出来了
            # 但量到的是个旗标/集合，比较它相等不等才有意义。
            delta, direction = None, "unmeasured"
        elif b == h:
            # 第 ③ 支：两侧都在、都非数值、**值相等** —— 旗标确实没变，这是真持平
            delta, direction = None, "same"
        else:
            # 第 ② 支：两侧都在、都不是 null、但不是两个可减数值且**值不相等**
            # （cost_available True→False、not_applicable 变长、failure_category_dist
            # 换构成，以及一侧数值一侧列表的混形）—— 变了，但方向无意义。
            # 修前同样被吞成 same；给 changed 而不是 up/down 是因为 None 侧没有 delta，
            # 任何箭头方向都是在猜。
            delta, direction = None, "changed"
        out[k] = {
            "base": b, "head": h, "delta": delta, "dir": direction,
            "lower_is_better": k in LOWER_IS_BETTER,
            # 方向是否**登记过**：由后端单点给（W2 裁定：前端不再养第二张方向表 ——
            # 两张表必然漂移）。False 时 lower_is_better 的 False 是"没登记"而不是
            # "越大越好"，前端必须渲"方向未登记"中性灰，不许给绿/红。
            "direction_registered": k in LOWER_IS_BETTER | HIGHER_IS_BETTER,
        }
    bcases, hcases = base.get("case_results") or {}, head.get("case_results") or {}
    flips = []
    for no in sorted(set(bcases) | set(hcases), key=_case_sort_key):
        b, h = bcases.get(no), hcases.get(no)
        if b is not None and h is not None and b != h:
            flips.append({"case_no": no,
                          "from": "pass" if b else "fail",
                          "to": "pass" if h else "fail"})
    out["case_flips"] = flips
    # W3（前端 I7 同族的谎，后端侧）：上面那一句 `b is not None and h is not None`
    # 把只在单边出现的用例**直接跳过**，于是 base 10 条 / head 5 条的两次 Run 比出来
    # 是 flips=[] —— 页面照实写"没有翻转"，读的人以为覆盖一致，真相是后 5 条根本没进过场。
    # "没有翻转"与"翻转不可比"必须能区分，所以这里把覆盖差显式量出来交给渲染端。
    # 口径：case_no 集合的交集才是可比域；两侧各自多出来的那些**不是没翻转，是没比**。
    only_base = sorted((set(bcases) - set(hcases)), key=_case_sort_key)
    only_head = sorted((set(hcases) - set(bcases)), key=_case_sort_key)
    out["case_coverage"] = {
        "base_total": len(bcases),
        "head_total": len(hcases),
        "comparable_total": len(set(bcases) & set(hcases)),
        "only_in_base": only_base,     # head 没跑过这些用例
        "only_in_head": only_head,     # base 没跑过这些用例
        "flips_total": len(flips),
    }
    return out
