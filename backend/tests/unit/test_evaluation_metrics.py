"""评测指标口径（app/application/evaluation_metrics.py，锚点 15 / 19 / 31 / 45 / 61 / 65 / 72 / 92 / 110 / 128 / 217 / 227）。

这里是「产品对质量的定义」，会被反复追问，也是 W1/W2/W3 三笔错账的现场：
dist 只算未通过、方向表两张且互斥、缺席 ≠ 持平 ≠ 没量出来。三条各有针。
"""
from decimal import Decimal

from app.application.evaluation_metrics import (
    CATEGORY_REQUIRED,
    DIFF_DIRS,
    HIGHER_IS_BETTER,
    JUDGE_FAILED_KEY,
    LOWER_IS_BETTER,
    QUALITY_PASS_SCORE,
    compute_metrics,
    diff_metrics,
    percentile,
    score_case,
)


def _row(case_no: int, **kw) -> dict:
    base = {"case_no": case_no, "status": "ok", "passed": True, "score": 4,
            "tool_total": 2, "tool_ok": 2, "latency_ms": 100,
            "prompt_tokens": 10, "completion_tokens": 5, "cost": 1.0,
            "reviewer_verdict": "pass"}
    base.update(kw)
    return base


def test_percentile_is_linear_interpolation_not_floor_index():
    assert percentile([], 95) is None                    # 空集合返回 None 不返回 0
    assert percentile([7], 50) == 7.0
    assert percentile([0, 10], 95) == 9.5                # 取整下标会让它等于 10
    assert percentile([5, 1, 3], 50) == 3.0
    assert percentile([1, None, 2], 50) == 1.5           # None 被滤掉


def test_score_case_requires_ok_and_a_real_score_at_or_above_threshold():
    assert QUALITY_PASS_SCORE == 3
    assert score_case(status="ok", score=3) is True
    assert score_case(status="ok", score=2) is False
    assert score_case(status="error", score=5) is False   # 故障不算能力
    assert score_case(status="ok", score=None) is False   # 宁可低估不虚报


def test_failure_dist_invariant_sum_equals_real_failures():
    rows = [
        _row(1),                                                    # 通过、无分类
        _row(2, passed=False, score=1, failure_category=None),       # judge 判不过
        _row(3, passed=False, score=None, failure_category="review_timeout"),
        _row(4, passed=True, failure_category="review_timeout"),     # 老错账的构造行
    ]
    m = compute_metrics(rows, category_scope=["general"])
    dist = m["failure_category_dist"]
    assert JUDGE_FAILED_KEY in dist
    assert sum(dist.values()) == m["cases_total"] - m["cases_passed"] == 2
    assert dist["review_timeout"] == 1          # 通过那条没被计进去


def test_cost_is_all_or_nothing_and_decimal_is_normalized_on_read():
    rows = [_row(1, cost=Decimal("1.5")), _row(2, cost=2.5)]
    m = compute_metrics(rows, category_scope=["general"])
    assert m["cost_available"] is True
    assert m["total_cost"] == 4.0
    mixed = [_row(1, cost=Decimal("1.5")), _row(2, cost=None)]
    m2 = compute_metrics(mixed, category_scope=["general"])
    assert m2["cost_available"] is False and m2["total_cost"] is None


def test_reviewer_rates_share_one_denominator_and_are_exposed():
    rows = [_row(1), _row(2, reviewer_verdict="reject", passed=False, score=1),
            _row(3, reviewer_verdict="pass", passed=False, score=1),
            _row(4, reviewer_verdict=None, passed=False, score=1)]
    m = compute_metrics(rows, category_scope=["general"])
    assert m["reviewed_cases"] == 3
    assert m["reviewer_error_detection_rate"] == round(1 / 3, 4)
    assert m["reviewer_miss_rate"] == round(1 / 3, 4)
    assert m["task_success_rate"] == 0.25


def test_rag_metric_and_not_applicable_follow_category_scope():
    rows = [_row(1, retrieval_hit=True), _row(2, retrieval_hit=False)]
    m = compute_metrics(rows, category_scope=["rag"])
    assert m["retrieval_quality"] == 0.5
    assert m["not_applicable"] == []
    m2 = compute_metrics(rows, category_scope=["general"])
    assert m2["retrieval_quality"] is None
    assert m2["not_applicable"] == sorted(n for ns in CATEGORY_REQUIRED.values() for n in ns)


def test_direction_tables_are_disjoint_and_counting_keys_are_unregistered():
    assert "reviewer_miss_rate" in LOWER_IS_BETTER          # W2 补的那一族
    assert "total_cost" in LOWER_IS_BETTER and "task_success_rate" in HIGHER_IS_BETTER
    assert not (LOWER_IS_BETTER & HIGHER_IS_BETTER)
    for unregistered in ("cases_total", "reviewed_cases", "quality_pass_score",
                         "cost_available"):
        assert unregistered not in LOWER_IS_BETTER | HIGHER_IS_BETTER


def test_diff_dirs_missing_is_never_reported_as_flat():
    base = {"latency_avg_ms": 100.0, "total_cost": None, "cost_available": True,
            "category_scope": ["a"], "vanished": 1}
    head = {"latency_avg_ms": 80.0, "total_cost": None, "cost_available": False,
            "category_scope": ["a"], "brand_new": 9}
    d = diff_metrics(base, head)
    assert d["latency_avg_ms"]["dir"] == "down" and d["latency_avg_ms"]["delta"] == -20.0
    assert d["latency_avg_ms"]["lower_is_better"] is True
    assert d["latency_avg_ms"]["direction_registered"] is True
    assert d["total_cost"]["dir"] == "unmeasured" and d["total_cost"]["delta"] is None
    assert d["cost_available"]["dir"] == "changed"
    assert d["category_scope"]["dir"] == "same"
    assert d["vanished"]["dir"] == "missing_head"
    assert d["brand_new"]["dir"] == "missing_base"
    assert all(v["dir"] in DIFF_DIRS for k, v in d.items() if k != "case_flips" and k != "case_coverage")
    assert d["brand_new"]["direction_registered"] is False


def test_case_flips_are_sorted_numerically_and_coverage_is_explicit():
    # 计划的构造自相矛盾：它同时要求 only_in_base == ["9"]（9 只在基线侧 ⇒ 不可比，
    # evaluation_metrics.py:300）和 flips 里有 "9"（翻转要求两侧都有值，evaluation_metrics.py:290
    # 那句 `b is not None and h is not None` 会跳过单边用例）。head 改成不含 1、9/10 都判不过：
    # 覆盖收缩（1 只在基线侧）与数值排序（'9' 必须在 '10' 之前）两半才都真被钉住。
    base = {"case_results": {"1": True, "9": True, "10": True}}
    head = {"case_results": {"9": False, "10": False}}
    d = diff_metrics(base, head)
    assert [f["case_no"] for f in d["case_flips"]] == ["9", "10"]     # '9' 不许排到 '10' 之后
    assert d["case_flips"][0] == {"case_no": "9", "from": "pass", "to": "fail"}
    cov = d["case_coverage"]
    assert (cov["base_total"], cov["head_total"], cov["comparable_total"]) == (3, 2, 2)
    assert cov["only_in_base"] == ["1"] and cov["only_in_head"] == []
    assert cov["flips_total"] == 2
    assert "case_results" not in d                                   # 明细不进指标 diff
