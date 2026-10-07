"""引用编号与上下文装配（app/ai/rag/citation.py:20 / 46）。

模型回答里的 [1] 必须与前端显示的第 1 条同源；没有命中时要 ("", []) 而不是空资料块，
否则模型会对着空资料编答案。
"""
import json
import uuid

from app.ai.rag.citation import Citation, build_context
from app.ai.rag.retriever import Hit


def _hit(i: int, *, page: int | None = 3, score: float = 0.87654321) -> Hit:
    return Hit(chunk_id=i, document_id=uuid.UUID(int=i), filename=f"手册{i}.pdf",
               chunk_index=i, content=f"正文{i}", page=page, score=score)


def test_no_hits_degrade_to_empty_pair_not_an_empty_material_block():
    assert build_context([]) == ("", [])


def test_numbering_starts_at_one_and_matches_the_block_order():
    text, citations = build_context([_hit(1), _hit(2), _hit(3)])
    assert [c.index for c in citations] == [1, 2, 3]
    assert text.count("【资料 ") == 3 and "【资料 1】" in text and "【资料 3】" in text
    assert text.index("【资料 1】") < text.index("【资料 2】") < text.index("【资料 3】")


def test_blocks_are_separated_by_a_blank_line():
    text, _ = build_context([_hit(1), _hit(2)])
    assert "\n\n" in text
    assert len(text.split("\n\n【资料 2】")) == 2


def test_missing_page_omits_the_location_clause():
    text, citations = build_context([_hit(1, page=None), _hit(2, page=7)])
    assert "（第" not in text.split("【资料 1】")[1].split("【资料 2】")[0]
    assert "（第 7 页）" in text
    assert [c.page for c in citations] == [None, 7]


def test_score_is_rounded_to_four_decimals():
    _, citations = build_context([_hit(1)])
    assert citations[0].score == round(0.87654321, 4)


def test_payload_is_json_serialisable_with_document_id_as_str():
    _, citations = build_context([_hit(1)])
    payload = citations[0].to_payload()
    assert isinstance(payload["document_id"], str)
    json.dumps(payload)                                   # 不抛就是 SSE 帧能用
    assert payload["filename"] == "手册1.pdf" and payload["chunk_index"] == 1


def test_citation_is_a_plain_dataclass_with_the_six_contract_fields():
    c = Citation(index=1, document_id=uuid.UUID(int=1), filename="f",
                 page=None, chunk_index=2, score=0.5)
    assert c.to_payload()["page"] is None
    # citation.py:23-28 定义六字段、:36-43 发射六个键——键集恰等，多一枚少一枚都会红
    assert set(c.to_payload()) == {"index", "document_id", "filename", "page", "chunk_index", "score"}
