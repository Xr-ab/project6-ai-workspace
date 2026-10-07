"""三条 Demo 脚本的离线静态针：不连容器也能证明它们「有牙齿」。

真跑的断言在容器上才能验，但「脚本里到底断言了什么」今天就能钉住：
一份只 print 结果不 assert 的演示脚本，半年后等于没跑过。
"""
import ast
import importlib
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = BACKEND_ROOT / "demo"
SCRIPTS = ("demo1_chat_rag.py", "demo2_multi_agent.py", "demo3_workflow.py")


@pytest.mark.parametrize("name", SCRIPTS)
def test_each_script_imports_and_exposes_main(name):
    mod = importlib.import_module(f"demo.{name.removesuffix('.py')}")
    assert callable(mod.main)


@pytest.mark.parametrize("name", SCRIPTS)
def test_each_script_actually_asserts(name):
    """每条脚本至少 4 次 require() 调用（AST 数，不是 grep 数——注释里的 require 不算）。"""
    tree = ast.parse((DEMO_DIR / name).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "require"]
    assert len(calls) >= 4, f"{name} 只有 {len(calls)} 条 require，太弱"


@pytest.mark.parametrize("name", SCRIPTS)
def test_each_script_writes_a_receipt(name):
    tree = ast.parse((DEMO_DIR / name).read_text(encoding="utf-8"))
    names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "write_receipt" in names


def test_fixture_markers_are_present_and_ascii():
    md = (BACKEND_ROOT / "tests/fixtures/demo/company_report.md").read_text(encoding="utf-8")
    for marker in ("SECTION-ALPHA-7731", "SECTION-BRAVO-4402", "SECTION-CHARLIE-9015"):
        assert marker in md
    # 纯 ASCII 是 spec §6.3 的刻意要求（非 ASCII 会在 GBK 控制台上二次损坏）。
    # 用显式判定而不是 md.encode("ascii") 靠抛异常——抛异常不是这条针的通过条件。
    assert md.isascii(), "company_report.md 含非 ASCII 字符"
    csv = (BACKEND_ROOT / "tests/fixtures/demo/sales_2026q3.csv").read_text(encoding="utf-8")
    assert csv.count("\n") >= 20
    assert csv.isascii(), "sales_2026q3.csv 含非 ASCII 字符"


def test_demo3_targets_doc_summary_not_sales_analysis():
    """计划开头裁定第 3 条：Demo 3 走 doc_summary（唯一把上传件收进 input_spec 的图）。

    第二行是全源文本子串禁令，注释与 docstring 也算（R47：一个禁令只管代码、
    不管注释，等于告诉后来人「换个注释就能改道」）。所以 demo3 里指代那张图只能用中文，
    不许出现 `sales_analysis` 这十个字符——Step 6 的实现文本按此写。
    """
    src = (DEMO_DIR / "demo3_workflow.py").read_text(encoding="utf-8")
    assert '"doc_summary"' in src or "'doc_summary'" in src
    assert "sales_analysis" not in src


def test_rag_questions_stay_ascii():
    """跨语言提问会让**聊天面**检索空手，而且是以「判红」以外的方式空手：不报错，只是没引用。

    实测（本地 bge-small-zh-v1.5，零钱探针 scratch/_p11c_t7_query_probe/）：
      中文问 + 纯 ASCII 英文报告  top1=0.4403 → 过门 0/3 条
      中文问 + 纯 ASCII 英文 CSV  top1=0.4132 → 过门 0/5 条
    两者都低于 retriever.DEFAULT_MIN_SCORE=0.45，全文腿（plainto 要求 lexeme 全 AND）
    也救不回来 ⇒ retrieve 返空。Task 7 第一次真跑就撞在 Demo 1 上
    （_p11c_t7_demo1/inner-rc.txt=1）——这对 Demo 1 的聊天面至今成立（该面保持默认 gate）。
    2026-10-03 修复波起 Demo 3 的 doc_summary 图**已豁免**该阈值（retrieve 传 min_score=0.0，
    修的是「中文元问句 0 命中 → 整条 run failed」），中文问在那条图上不再空手；本针继续
    按 ASCII 钉两条提问，是因为演示问句本波未改（改文案要重烧真跑），不是技术必需。

    夹具按 spec §6.3 定死纯 ASCII，所以能动的只有提问：两条提问（按上口径）仍钉 ASCII。
    针打在常量上（真值，不是 grep 源码），改文案就红。
    """
    d1 = importlib.import_module("demo.demo1_chat_rag")
    d3 = importlib.import_module("demo.demo3_workflow")
    assert d1.RAG_QUESTION.isascii(), "Demo 1 的 RAG 提问含非 ASCII：检索会空手"
    assert d3.SUMMARY_QUESTION.isascii(), (
        "Demo 3 的 workflow 提问含非 ASCII：本针按演示稳定性钉 ASCII"
        "（该图 2026-10-03 起已豁免阈值，非技术必需，见 docstring）")
    # 还得指着那一节问，不然换成任意英文句也过这道针
    assert d1.MARKER in d1.RAG_QUESTION


def test_demo2_report_fields_match_the_product_schema():
    """Demo 2 断言的「报告七个字段」必须与产品侧 Report 模型一字不差（R55 的针）。

    失效模式（Task 7 第 2 次真跑实测撞到的）：脚本按 `isinstance(report, str)` 断言，
    而产品从 Phase 5 起交回的是结构化字典（`app/ai/graph/nodes/report.py` 的 Report
    → `app/application/task_runner.py:217` 落 meta）。这类「脚本臆断产品形状」的错，
    只有把脚本常量与产品模型**同一进程内**比对才拦得住：产品改字段 → 本针红 →
    演示脚本必须重读一次契约，而不是等下一次真跑（一条真跑要烧 8~13 次补全）。
    """
    from app.ai.graph.nodes.report import Report

    d2 = importlib.import_module("demo.demo2_multi_agent")
    assert d2.REPORT_FIELDS == set(Report.model_fields), (
        f"脚本与产品的报告字段已分叉：脚本多 {sorted(d2.REPORT_FIELDS - set(Report.model_fields))}"
        f" / 产品多 {sorted(set(Report.model_fields) - d2.REPORT_FIELDS)}")
    # 降级支路的形状（report.py:59 只写 executive_summary + content）不许混进七个字段：
    # 「字段齐」这道闸的全部价值就在于它能把降级报告挡在外面。
    assert "content" not in d2.REPORT_FIELDS
    # report_volume 自己不许是空转：只有一行散文时它必须只数那一行，空壳必须数出 0。
    assert d2.report_volume({"executive_summary": "甲" * 50, "risks": ["乙", "丙"]}) == 52
    assert d2.report_volume({"executive_summary": "", "risks": [], "review_warning": None}) == 0


def test_no_script_prints_a_token_or_password():
    for name in SCRIPTS:
        src = (DEMO_DIR / name).read_text(encoding="utf-8")
        for forbidden in ("access_token", "refresh_token", "P6_DEMO_PASSWORD"):
            assert forbidden not in src, f"{name} 里出现了凭据面名字：{forbidden}"
