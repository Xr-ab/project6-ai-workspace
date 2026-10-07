"""报告层（Phase 9b）的离线针：纯逻辑 + 声明面，零 DB 零网络。

分三组，对应这个功能的三个"会悄悄坏掉"的地方：

A. **渲染口径**（`report_service.render_markdown`）：报告是交付物，markdown 是它的对外
   样子。坏法很隐蔽——不是抛错，而是少渲一段、把降级支路印两遍、或把复核警告吞掉。
   这些都不会让任何页面报错，只会让交付物悄悄缺内容。

B. **契约对账**（后端标签表 ↔ 产品 `Report` 模型 ↔ 前端镜像表，以及时间窗词表
   `RangeKey` ↔ 前端 `lib/ranges.ts`）：
   `REPORT_FIELD_LABELS` 是三处同源声明里的中间那一环。产品加字段而不加标签 ⇒
   报告里那一段正文永远不出现（R56 就是这一类：形状对不上，没人红）。
   与 `test_report_render_contract.py` 同一套办法（同进程读产品模型），本文件多钉一条
   **前后端顺序也必须一致**——报告是给人顺着读的，顺序分叉了读起来就是乱的。

C. **接口声明面**（从 `app.openapi()` 取）：分页上下界（`ge=1` 是 8b 那个 500 的教训）、
   以及"没有创建口"这条设计裁定。取 OpenAPI 而不是路由对象：本仓 FastAPI 把
   `include_router` 惰性包成 `_IncludedRouter`（`path` 为 None），走路由对象要自己拼
   prefix，且已有探针这么错过一次（取到空集 → 断言全绿 → 假绿）。
"""
from pathlib import Path

import pytest

from app.application import report_service
from app.data.models import Report as ReportModel
from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TASK_REPORT_TS = BACKEND_ROOT.parent / "frontend/src/lib/taskReport.ts"
RANGES_TS = BACKEND_ROOT.parent / "frontend/src/lib/ranges.ts"


# ============================ A. 渲染口径 ============================

# 一条真跑形状的结构化报告：七个字段齐，键序照 PostgreSQL jsonb 的重排结果
# （`risks` 键最短，排在最前 —— Demo 2 真跑读出来过，见 docs/13）。
STRUCTURED = {
    "risks": ["渠道结构变化的风险未量化"],
    "sources": ["query_sales 2026Q3"],
    "key_findings": ["8 月环比下降 12%", "华东区贡献了降幅的 70%"],
    "data_evidence": ["sales_2026q3.csv 逐月汇总"],
    "root_causes": ["促销活动集中在 7 月"],
    "recommendations": ["9 月补一次区域定向促销"],
    "executive_summary": "本月销售下滑主要由渠道结构与促销节奏错位造成。",
}


def test_structured_report_renders_summary_first_then_labels_in_model_order():
    """摘要置顶、其余按模型字段序分节 —— 顺序不跟 jsonb 键序走。"""
    md = report_service.render_markdown(STRUCTURED)
    assert md is not None
    # 摘要不加「## 执行摘要」标题：它就是开篇第一段（与前端 ReportView 同口径）
    assert md.startswith("本月销售下滑主要由渠道结构与促销节奏错位造成。")
    assert "## 执行摘要" not in md
    # 其余六段按声明序出现，且 risks 不在最前（那是库里的键序，不是读序）
    positions = [md.index(f"## {label}") for _key, label in report_service.FIELD_LABELS if f"## {label}" in md]
    assert positions == sorted(positions)
    assert md.index("## 核心发现") < md.index("## 风险提示")


def test_list_fields_become_markdown_bullets():
    md = report_service.render_markdown(STRUCTURED)
    assert "- 8 月环比下降 12%\n- 华东区贡献了降幅的 70%" in md
    assert "- 9 月补一次区域定向促销" in md


def test_degraded_shape_is_rendered_and_not_printed_twice():
    """降级支路 `{executive_summary, content}` 两值相同 ⇒ 只印一遍。

    印两遍不是崩溃、也没有报错 —— 它是"报告读起来像被复制粘贴了一次"，
    只有针能拦住。
    """
    md = report_service.render_markdown(
        {"executive_summary": "原文一段", "content": "原文一段"}
    )
    assert md == "原文一段\n"
    assert md.count("原文一段") == 1


def test_degraded_content_is_shown_when_it_differs_from_summary():
    """两值写岔了就分开展示：少显示比多显示危险（认不出 ≠ 没有）。"""
    md = report_service.render_markdown({"executive_summary": "结论", "content": "完整原文"})
    assert "结论" in md
    assert "## 报告原文" in md
    assert "完整原文" in md


def test_review_warning_survives_as_its_own_section():
    """复核警告不许被吞：它是"这份报告带病交付"的唯一标记。"""
    md = report_service.render_markdown(
        {**STRUCTURED, "review_warning": ["数字口径未标明", "材料中不存在的数字不得出现"]}
    )
    assert "## 复核警告" in md
    assert md.index("## 复核警告") > md.index("## 来源"), "警告要排在正文之后，不打断阅读"
    assert "- 数字口径未标明" in md


def test_unknown_fields_are_not_dropped():
    """产品加字段而渲染器还没认它 ⇒ 用键名当标题照渲，不是丢掉这一段。"""
    md = report_service.render_markdown({**STRUCTURED, "confidence": 0.82})
    assert "## confidence" in md
    assert "0.82" in md


@pytest.mark.parametrize(
    "value",
    [None, {}, [], "一段散文", 42, {"executive_summary": "", "key_findings": []}],
)
def test_unrecognisable_content_returns_none(value):
    """认不出就 None —— 调用方把 markdown 落成 NULL，前端退回结构化渲染。

    不猜、不硬凑：编一份假 markdown 比留空更坏（留空是可以看出来的，
    编出来的东西看起来是对的）。
    """
    assert report_service.render_markdown(value) is None


# ============================ B. 契约对账 ============================


def test_field_labels_cover_every_report_field_in_model_order():
    """三处同源：产品 `Report` 模型 ↔ 后端标签表 ↔ 前端镜像表（键集与顺序都要一致）。"""
    from app.ai.graph.nodes.report import Report

    model_fields = list(Report.model_fields)
    assert [key for key, _ in report_service.FIELD_LABELS] == model_fields, (
        "后端标签表的键/序与产品 Report 模型分叉："
        f"标签 {[k for k, _ in report_service.FIELD_LABELS]} vs 产品 {model_fields}"
    )


def _ts_block(source: str, marker: str) -> str:
    """取 `marker ... = <容器>` 的容器体（只看 `=` 后第一个非空白字符，同 contracts 针）。"""
    start = source.index(marker)
    eq = source.index("=", start)
    brace = next(i for i, ch in enumerate(source[eq:], start=eq) if not ch.isspace() and ch != "=")
    assert source[brace] in "{["  # noqa: S101 —— 解析器自检，形状变了要当场说
    closer = "}" if source[brace] == "{" else "]"
    return source[brace + 1: source.index(closer, brace)]


def test_frontend_mirror_uses_the_same_keys_and_order():
    """前端镜像表（`lib/taskReport.ts`）必须与后端标签表同键同序。

    读前端源码而不是 import（本仓无 TS 运行时桥，为一条针引 esbuild 不值）——
    这是**面向源码的静态对账**，与 `test_report_render_contract.py` 同一套办法；
    它拦的是"后端加了字段而前端镜像没跟"，拦不住"镜像对了但组件没渲染"（那是前端 vitest 的活）。
    """
    assert TASK_REPORT_TS.exists(), f"前端归一模块不在预期位置：{TASK_REPORT_TS}"
    source = TASK_REPORT_TS.read_text(encoding="utf-8")
    import re

    frontend_order = re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", _ts_block(source, "REPORT_FIELD_ORDER"))
    backend_order = [key for key, _ in report_service.FIELD_LABELS]
    assert frontend_order == backend_order, f"前端展示顺序 {frontend_order} vs 后端 {backend_order}"

    frontend_keys = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", _ts_block(source, "REPORT_FIELD_LABELS"), re.M))
    assert frontend_keys == set(backend_order), "前端标签表键集与后端分叉"


def test_degraded_and_warning_keys_are_not_advertised_as_fields():
    """`content`（降级支路）与 `review_warning`（警告键）不许混进正式字段表。

    「七字段齐」是 Demo 2 用来判定「没走降级支路」的仪器；把它们登记成正式字段，
    等于把降级报告洗成正常报告 —— 那条仪器当场失效。
    """
    keys = {key for key, _ in report_service.FIELD_LABELS}
    assert "content" not in keys
    assert "review_warning" not in keys


def test_frontend_range_tabs_are_exactly_the_backend_range_keys():
    """时间窗档位：前端 `lib/ranges.ts` 的 key 集必须 == 后端 `RangeKey`。

    为什么这一条要在后端钉而不是前端 vitest：前端那边只能对账"自己抄没抄错"
    （`RangeTab.key` 的类型是 `StatsRange`，编译期已经锁住了合法值集合的**引用**），
    但真正的分歧发生在**两边各有一份词表**的时候 —— 后端加一档 `quarter` 而前端没跟，
    前端那一档永远不会出现，用户看到的"近 30 天"其实还是旧的四档，出的数没人看得出不对。
    这一类"加了一边、另一边静默"的分歧只有跨语言静态对账能拦（与上面那条
    `test_frontend_mirror_uses_the_same_keys_and_order` 同一套办法、同一个理由）。

    只钉**键集**不钉**顺序**：tab 的左起顺序是前端文案的自由，后端 `RANGE_KEYS` 的
    元组序没有对外含义；把两者钉在一起会让一次纯排版的改动红在后端。
    """
    import typing

    from app.data.repositories import stats_repo
    from app.schemas.stats import RangeKey

    assert RANGES_TS.exists(), f"前端时间窗档位表不在预期位置：{RANGES_TS}"
    source = RANGES_TS.read_text(encoding="utf-8")
    import re

    frontend_keys = set(re.findall(r"key:\s*'([A-Za-z_][A-Za-z0-9_]*)'", _ts_block(source, "export const RANGE_TABS")))
    backend_keys = set(typing.get_args(RangeKey))
    assert frontend_keys == backend_keys, (
        f"前端时间窗 {sorted(frontend_keys)} vs 后端 RangeKey {sorted(backend_keys)}"
    )
    # 三个声明口是同一份词表的三个视图：词表变了，两处都要跟着变
    assert backend_keys == set(stats_repo.RANGE_KEYS), "RangeKey 与 stats_repo.RANGE_KEYS 分叉"


# ============================ C. 接口声明面 ============================


def _report_paths() -> dict:
    return {p: v for p, v in app.openapi()["paths"].items() if "report" in p}


def test_only_two_report_paths_and_no_create_verb():
    """`/reports` 只有两条路径，且**没有 POST**。

    没有创建口是设计裁定不是漏项（见 api/reports.py 头注）：报告的写入方是执行链路，
    手工 POST 一份没有来源执行的报告 = 凭空造交付物。所以这条针不是在测"实现对不对"，
    是在守"后来人想加 POST 时得先想清楚"。
    """
    paths = _report_paths()
    assert set(paths) == {"/api/v1/reports", "/api/v1/reports/{report_id}"}, f"路径集变了：{sorted(paths)}"
    assert set(paths["/api/v1/reports"]) == {"get"}, "列表口只该有 GET（没有创建口）"
    assert set(paths["/api/v1/reports/{report_id}"]) == {"get", "delete"}, "详情口只该有 GET / DELETE"


def test_report_list_pagination_has_both_bounds():
    """上下界都要有：下界是 8b 那个 500 的教训（`?limit=-1` 进 SQL LIMIT 由 PG 抛错）。"""
    params = _report_paths()["/api/v1/reports"]["get"]["parameters"]
    limit = next(p for p in params if p["name"] == "limit")
    assert limit["schema"]["minimum"] == 1
    assert limit["schema"]["maximum"] == 200
    offset = next(p for p in params if p["name"] == "offset")
    assert offset["schema"]["minimum"] == 0


def test_report_list_range_param_matches_stats_enum_and_defaults_to_all():
    """时间范围过滤：`range` 的合法值必须与 stats **逐字相同**，默认值刻意不同。

    为什么钉"逐字相同"而不是各写一份：时间窗语义（滚动 7/30 天、today=本地日 00:00）
    全仓只许有一份，那份在 `stats_repo.range_start`。报表这边一旦私自加个
    `quarter` 或把 `week` 改成自然周，同一个词在两个页面上就是两个意思 ——
    那种分歧不会报错，只会让人对不上数然后怀疑系统。

    为什么默认 `all` 而 stats 默认 `today`：报告列表是**交付物台账**（"我历史上
    出过哪些报告"），默认当日会让它看上去是空的；stats 是当日运营面，默认 today 才对。
    """
    params = _report_paths()["/api/v1/reports"]["get"]["parameters"]
    report_range = next(p for p in params if p["name"] == "range")
    stats_params = app.openapi()["paths"]["/api/v1/stats/overview"]["get"]["parameters"]
    stats_range = next(p for p in stats_params if p["name"] == "range")
    assert report_range["schema"]["enum"] == stats_range["schema"]["enum"]
    assert report_range["schema"]["default"] == "all"
    assert stats_range["schema"]["default"] == "today"


def test_report_list_and_count_share_the_same_filter_signature():
    """列表与计数的过滤参数必须一字相同 —— `total` 不许比当页多算或少算一段。

    签名对账而不是行为对账：行为要真库（那是 integration 层 `since` 窗那条针的活），
    这条钉的是"加谓词时只加了一处"这个**结构性**错误。分页接口最常见的谎就是
    列表按新条件筛了、计数没筛，而它只在第二页之后才看得出来。
    """
    import inspect

    from app.data.repositories import report_repo

    skip = {"session", "self"}
    list_kwargs = {
        p for p, s in inspect.signature(report_repo.list_reports).parameters.items()
        if s.kind is inspect.Parameter.KEYWORD_ONLY and p not in skip
    }
    count_kwargs = {
        p for p, s in inspect.signature(report_repo.count_reports).parameters.items()
        if s.kind is inspect.Parameter.KEYWORD_ONLY and p not in skip
    }
    assert {"organization_id", "user_id", "report_type", "since"} <= list_kwargs
    assert list_kwargs - {"limit", "offset"} == count_kwargs, (
        f"列表独有 {list_kwargs - count_kwargs - {'limit', 'offset'}} / "
        f"计数独有 {count_kwargs - list_kwargs}"
    )


def test_report_list_response_carries_total_and_detail_carries_both_bodies():
    """两个形状裁定各钉一条（它们是本功能的对外契约，改了就炸前端）。

    列表 `{items,total}`：**有意破例**于 docs/06 §7.1 的"裸数组"口径（要总数才能翻页），
    破例已登记；详情同时给 `content`（结构化真值）与 `markdown`（后端渲染全文）。
    """
    schema = app.openapi()
    list_props = set(
        schema["components"]["schemas"]["ReportListOut"]["properties"]
    )
    assert list_props == {"items", "total"}, f"列表信封形状变了：{sorted(list_props)}"
    detail_props = set(schema["components"]["schemas"]["ReportDetailOut"]["properties"])
    assert {"content", "markdown"} <= detail_props, f"详情缺正文两形之一：{sorted(detail_props)}"
    # 列表项**不许**带正文：拖着 N 份正文分页是最常见的胖响应
    summary_props = set(schema["components"]["schemas"]["ReportOut"]["properties"])
    assert "content" not in summary_props and "markdown" not in summary_props


def test_report_table_declaration_matches_the_service_contract():
    """表声明面（列/索引/外键）—— 离线可查，不需要真库。

    为什么值得单独钉：这一层错了要等迁移真跑才炸（本机没 Docker 时根本发现不了），
    而它不是"实现细节"——`ON DELETE SET NULL` 那两条关系到"删任务会不会带走报告"
    这个产品口径（答案：不会）。
    """
    table = ReportModel.__table__
    assert set(table.columns.keys()) == {
        "id", "organization_id", "user_id", "task_id", "task_run_id", "title",
        "report_type", "content", "markdown", "status", "reviewer_verdict",
        "total_tokens", "cost", "created_at", "updated_at",
    }
    assert {i.name for i in table.indexes} == {
        "ix_reports_organization_id", "ix_reports_org_created",
        "ix_reports_task_run", "ix_reports_task",
    }
    for column in ("task_id", "task_run_id"):
        fk = next(iter(table.c[column].foreign_keys))
        assert fk.ondelete == "SET NULL", f"{column} 的 ondelete 应当是 SET NULL（报告不随执行消失）"
    # 报告类型是机器值三档（中文标签在前端），enforcement 由 service 的映射保证
    assert report_service.REPORT_TYPES == {"analysis", "workflow", "summary"}


# ============================ D. 纯映射 ============================


class _FakeTask:
    """最小 task 替身：只带 service 真正读的三个字段。

    带 `id` 是必需的 —— 未知 task_type 那条支路会 `logger.warning(..., task.id)`，
    少了它 AttributeError 会把"降落到 analysis"这条容错路径变成崩溃（第一版就这么红过一次）。
    """

    def __init__(self, task_type: str, title: str | None = None, question: str = "", id: str = "t-1"):
        self.task_type = task_type
        self.title = title
        self.question = question
        self.id = id


@pytest.mark.parametrize(
    ("task_type", "expected"),
    [
        ("agent_analysis", "analysis"),
        ("workflow", "workflow"),
        ("document_summary", "summary"),
    ],
)
def test_report_type_mapping(task_type, expected):
    assert report_service.report_type_for_task(_FakeTask(task_type)) == expected


def test_unknown_task_type_falls_back_to_analysis():
    """认不出的 task_type 落 analysis 而不是抛错。

    走到写报告这一步时执行**已经成功结束**，为一个分类标签把报告丢掉是本末倒置。
    降落到最宽的那一类 + 日志留痕，比丢掉诚实。
    """
    assert report_service.report_type_for_task(_FakeTask("some_future_graph")) == "analysis"


def test_title_prefers_task_title_then_question_and_truncates():
    assert report_service.title_for_task(_FakeTask("x", title="季度复盘", question="问")) == "季度复盘"
    # 标题为空/空白 ⇒ 退回问题原文（列表页要的是"这份报告在讲什么"，不是编号）
    assert report_service.title_for_task(_FakeTask("x", title="   ", question="本月销售为何下滑？")) == "本月销售为何下滑？"
    long = "甲" * 300
    out = report_service.title_for_task(_FakeTask("x", title=long, question="q"))
    assert len(out) == 200
    assert out.endswith("…")
