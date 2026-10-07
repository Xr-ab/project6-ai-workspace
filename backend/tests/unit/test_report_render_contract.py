"""报告呈现面的契约针：前端标签表的键集 == 产品 `Report` 模型的字段集。

失效模式（R56 的根因，值得单独一条针）：前端报告区**按自己臆想的形状取**产品数据，
产品写字典、页面判字符串，于是整整两段（Phase 9a → 11c）报告正文没有任何呈现区，
而没有任何针红过——因为归一住在组件里，本仓 vitest 边界是 `environment: 'node'`、零 DOM
（`frontend/vitest.config.ts:6`），组件不给测。

现在的分工：归一抽成纯函数 `frontend/src/lib/taskReport.ts`（前端 vitest 有针），
而「字段名这件事两边是否一致」由**本针**守——它同进程读产品模型与前端的标签表，
与 `test_demo_scripts_static.py::test_demo2_report_fields_match_the_product_schema`
同一套办法（同一类错、同一种拦法：产品改字段 → 针红 → 呈现面必须重读契约，
而不是等用户点开页面发现少了一段）。

为什么读前端源码而不是 import 它：本仓后端跑 pytest、前端跑 vitest，没有 TS 运行时桥
（引 esbuild/ts-node 只为一条针不值）。所以这里做的是**面向源码的静态对账**：
从 `taskReport.ts` 里解析出 `REPORT_FIELD_LABELS` 的键集与 `REPORT_FIELD_ORDER` 的有序列表。
它拦得住「产品加了字段而前端没加标签」与「两张表自己漂移」，这正是要拦的两件事；
拦不住的是"标签表写对了但组件没渲染它"——那一条由前端 vitest 的用例守。
"""
import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TASK_REPORT_TS = BACKEND_ROOT.parent / "frontend/src/lib/taskReport.ts"


def _read_source() -> str:
    assert TASK_REPORT_TS.exists(), f"前端归一模块不在预期位置：{TASK_REPORT_TS}"
    return TASK_REPORT_TS.read_text(encoding="utf-8")


def _block(source: str, marker: str) -> str:
    """取 `marker ... = <容器>` 的容器体（不含最外层括号）。

    只看 `=` 之后第一个非空白字符是 `{` 还是 `[` —— 不许在前 200 字里"找第一个花括号"：
    `REPORT_FIELD_ORDER` 的**类型标注**是 `readonly string[]`，而它后面紧跟的标签表里
    就有 `{`，按"找第一个花括号"取块会把顺序表读成空（本针第一版正是这么错的，
    第一跑就红——这类解析针必须自己先被真值验证过）。
    """
    start = source.index(marker)
    eq = source.index("=", start)
    brace = next(
        index for index, char in enumerate(source[eq:], start=eq) if not char.isspace() and char != "="
    )
    assert source[brace] in "{["  # noqa: S101 —— 解析器自检：形状变了要当场说，不要静默返回空
    closer = "}" if source[brace] == "{" else "]"
    end = source.index(closer, brace)
    return source[brace + 1:end]


def _label_keys() -> set[str]:
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", _block(_read_source(), "REPORT_FIELD_LABELS"), re.M))


def _order_keys() -> list[str]:
    return re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", _block(_read_source(), "REPORT_FIELD_ORDER"))


def test_label_table_covers_every_report_field():
    """产品每加一个字段，前端必须同批给一个中文标签（否则那一整段正文没有呈现面）。"""
    from app.ai.graph.nodes.report import Report

    model_fields = set(Report.model_fields)
    labels = _label_keys()
    missing = model_fields - labels
    extra = labels - model_fields
    assert not missing, f"产品有而前端没有标签的报告字段：{sorted(missing)}"
    assert not extra, f"前端有而产品没有的报告字段（产品已删/改名？）：{sorted(extra)}"


def test_display_order_is_exactly_the_model_field_order():
    """展示顺序必须与 `Report` 的字段声明序一字不差 —— 报告是给人顺着读的。

    为什么钉顺序而不是"能显示就行"：落库列是 JSONB，PostgreSQL 会按
    「键短者在前、同长按字典序」重排（Demo 2 真跑读出来的反常键序就是 `risks` 最前，
    见 `docs/13` §4），所以**对象自身的键序在公司里根本不是一个可靠的东西**——
    顺序只能由这份常量定，它错了整篇报告就是乱序的。
    """
    from app.ai.graph.nodes.report import Report

    assert _order_keys() == list(Report.model_fields), (
        "展示顺序与产品字段声明序分叉："
        f"前端 {_order_keys()} vs 产品 {list(Report.model_fields)}"
    )


def test_degraded_shape_keys_are_not_advertised_as_fields():
    """降级支路的 `content`（`report.py:59`）不许混进标签表。

    「七个字段齐」是 Demo 2 用来判定「没走降级支路」的仪器（`demo2_multi_agent.py:42`）；
    把 `content` 也登记成正式字段，等于把降级报告洗成正常报告。
    """
    labels = _label_keys()
    assert "content" not in labels
    assert "review_warning" not in labels, "review_warning 走 warnings 分支，不是正文一段"
