"""切分边界与重叠（app/ai/rag/chunker.py:34 / 38 / 42 / 46 / 50 / 58 / 79 / 111）。

overlap >= size 会原地打转；边界位置太靠前会产出十几字的碎片（碎片检索不出东西）。
两条都是静默失效，只能靠针。
"""
import pytest

from app.ai.rag.chunker import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    _MIN_FILL_RATIO,
    _find_boundary,
    _split_text,
    chunk_pages,
)
from app.ai.rag.parser import ParsedPage


def test_defaults_are_the_bge_small_zh_budget():
    assert (DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP) == (400, 60)
    assert _MIN_FILL_RATIO == 0.5


def test_overlap_not_below_size_is_rejected_before_any_infinite_loop():
    with pytest.raises(ValueError, match="必须小于"):
        chunk_pages([ParsedPage(text="甲" * 500, page=1)], size=100, overlap=100)
    with pytest.raises(ValueError, match="必须小于"):
        chunk_pages([ParsedPage(text="甲" * 500, page=1)], size=100, overlap=150)


def test_short_text_is_one_chunk_and_blank_pages_produce_nothing():
    assert _split_text("  短文本  ", 400, 60) == ["短文本"]
    assert _split_text("   ", 400, 60) == []
    chunks = chunk_pages([ParsedPage(text="", page=1),
                          ParsedPage(text="有内容", page=2)])
    assert [(c.text, c.index, c.page) for c in chunks] == [("有内容", 0, 2)]


def test_index_is_continuous_across_pages_and_page_number_is_kept():
    pages = [ParsedPage(text="甲" * 500, page=1), ParsedPage(text="乙" * 10, page=None)]
    chunks = chunk_pages(pages, size=200, overlap=20)
    assert [c.index for c in chunks] == list(range(len(chunks)))       # 整篇连续，不是每页从 0
    assert chunks[0].page == 1
    assert chunks[-1].page is None
    assert all(c.page == 1 for c in chunks[:-1])


def test_boundary_is_preferred_when_it_is_deep_enough_in_the_window():
    text = "甲" * 248 + "\n\n" + "乙" * 300
    parts = _split_text(text, 400, 60)
    assert parts[0] == "甲" * 248                    # 切在段落边界之后，不是硬切 400
    assert parts[1].endswith("乙" * 300)
    assert len(parts) == 2


def test_early_boundary_is_refused_and_the_window_is_cut_hard():
    text = "甲\n" + "乙" * 399                       # 边界在位置 1，远低于半窗
    parts = _split_text(text, 400, 60)
    assert len(parts[0]) == 400                      # _MIN_FILL_RATIO 生效 = 宁可硬切
    assert _find_boundary(text, 0, 400) == 0


def test_find_boundary_returns_start_when_no_separator_exists():
    text = "没" * 400
    assert _find_boundary(text, 0, 400) == 0


def test_overlap_actually_repeats_content():
    text = ("甲" * 248 + "\n\n" + "乙" * 300)
    parts = _split_text(text, 400, 60)
    assert parts[0][-30:] in parts[1]                # 被切断的内容至少在一个片段里是完整的
