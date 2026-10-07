"""短期记忆纯函数面（app/ai/graph/memory.py，锚点 30-38 / 46-48 / 68 / 81 / 199）。

这层为什么值得单测：docs/11 §4 那一整页判断（白名单 / 分层 / 预算 / 裁剪顺序）
错了不会有任何运行时报错替你说话——记忆超限是静默截断，白名单漏了是静默污染。
"""
import logging

from app.ai.graph.memory import (
    BUSINESS_ANALYST_INCLUDE,
    MEMORY_CHARS_MAX,
    MEMORY_CONCLUSION_CHARS,
    MEMORY_FACTS_MAX,
    MEMORY_RESEARCH_MAX,
    MEMORY_ROUNDS_MAX,
    REPORT_INCLUDE,
    SUPERVISOR_INCLUDE,
    _MEMORY_ARGS_CHARS,
    _MEMORY_FIELD_CHARS,
    _size,
    build_memory,
    clip_text,
    render_block,
)


def _run(run_no: int, *, status: str = "completed", **state_keys) -> dict:
    state = {"question": f"第{run_no}轮问", "analysis": f"第{run_no}轮结论", **state_keys}
    return {"run_no": run_no, "status": status, "state": state}


def test_clip_text_shapes_str_dict_and_everything_else():
    assert clip_text("  abc  ", 10) == "abc"          # strip 之后再截
    assert clip_text("abcdefg", 3) == "abc"
    assert clip_text({"content": "hello"}, 3) == "hel"
    assert clip_text({"nope": 1}, 5) == ""            # 结构不符当空处理
    assert clip_text(None, 5) == ""
    assert clip_text(123, 5) == ""                    # 绝不让一轮坏数据炸整条链


def test_history_line_is_cross_round_and_capped_at_memory_rounds_max():
    runs = [_run(i) for i in range(1, 7)]
    mc = build_memory(runs, round_no=7)["context"]
    assert mc is not None
    assert len(mc["history"]) == MEMORY_ROUNDS_MAX
    assert [h["round"] for h in mc["history"]] == [3, 4, 5, 6]   # 丢的是最旧的
    assert all(len(h["conclusion"]) <= MEMORY_CONCLUSION_CHARS for h in mc["history"])


def test_detail_line_only_from_the_latest_usable_snapshot():
    runs = [
        _run(1, data_results=[{"tool": "旧工具", "ok": True, "rows": 1, "args": {}}]),
        _run(2, data_results=[{"tool": "新工具", "ok": True, "rows": 2, "args": {}}]),
    ]
    mc = build_memory(runs, round_no=3)["context"]
    assert not any("旧工具" in f for f in mc["facts"])
    assert any("新工具" in f for f in mc["facts"])


def test_brief_rows_accepts_int_list_and_missing_shapes():
    runs = [_run(1, data_results=[
        {"tool": "query_sales", "ok": True, "rows": 4, "args": {"d": 1}},
        {"tool": "query_product", "ok": True, "rows": [1, 2], "args": {}},
        {"tool": "calculator", "ok": True, "rows": None, "args": {}},
        {"tool": "web_search", "ok": False, "rows": None, "args": {}},
    ])]
    facts = build_memory(runs, round_no=2)["context"]["facts"]
    assert facts[0] == 'query_sales({"d": 1}) → 4 行'
    assert 'query_product({}) → 2 行' in facts
    assert 'calculator({}) → 已返回' in facts
    assert 'web_search({}) → 失败' in facts
    # 钉的是行形状（memory.py:110 恒发射 f"{tool}({args}) → {status}"）——
    # 上面四条字面量已经把每行内容写死了，再用「行长下限」只会跟着字面量恒真、
    # 不 independently 失败；换成「每行都含箭头分隔符」才在验形状而不是验长度巧合。
    assert all(" → " in f for f in facts)


def test_facts_are_capped_and_conclusion_row_survives():
    items = [{"tool": f"t{i}", "ok": True, "rows": 1, "args": {}} for i in range(MEMORY_FACTS_MAX + 3)]
    items.append({"tool": "conclusion", "text": "结" * (_MEMORY_FIELD_CHARS + 500)})
    facts = build_memory([_run(1, data_results=items)], round_no=2)["context"]["facts"]
    assert len(facts) == MEMORY_FACTS_MAX
    assert facts[-1].startswith("数据结论：结")            # 结论行恒在末尾
    assert len(facts[-1]) - len("数据结论：") == _MEMORY_FIELD_CHARS


def test_failed_and_broken_snapshots_are_not_promoted_to_history():
    runs = [
        {"run_no": 1, "status": "failed", "state": {"analysis": "半截结论"}},
        {"run_no": 2, "status": "completed", "state": None},
        {"run_no": 3, "status": "completed", "state": {"analysis": "可用结论"}},
    ]
    logging.disable(logging.WARNING)          # lastResort 会把 WARNING 打到 stderr
    try:
        out = build_memory(runs, round_no=4)
    finally:
        logging.disable(logging.NOTSET)
    assert [h["round"] for h in out["context"]["history"]] == [3]
    assert out["stats"]["degraded_rounds"] == [2]     # 降级必须留痕（§7）
    assert out["stats"]["history_rounds"] == 1


def test_no_conclusion_still_keeps_the_detail_line():
    mc = build_memory([_run(1, analysis="", data_results=[
        {"tool": "query_sales", "ok": True, "rows": 3, "args": {}}])], round_no=2)["context"]
    assert mc["history"] == []
    assert mc["facts"]                      # 明细可用但不进结论线


def test_budget_trims_in_the_fixed_order_and_never_exceeds_the_cap():
    # 计划给的构造（5 轮 × 120 字结论 + 8 条 80 字参数的查数行）实测只有 1565 字符，离
    # MEMORY_CHARS_MAX 差一半，_apply_budget 一次都不进 → 只断言「≤上限 / truncated」会恒假。
    # 补齐 §4.3 的四条线各灌到自己上限才真超预算：未裁 size=3434（history 4 轮 rounds
    # [2,3,4,5]、facts 8、research 3、末轮 conclusion 120 字），裁后 size=2857（t2-pin-probe.out）。
    # 下面每条 stage 断言各钉 _apply_budget（memory.py:179-194）的一个 while 位置——见括注的
    # 「哪种换序会红」。诚实边界：它钉不住 facts 与 research 两个 while 之间的先后（本 fixture
    # 里二者都没被裁到，互换输出逐字节不变），也不裁 report_brief（没有循环裁它 → 无法作顺序针）。
    runs = [_run(i, question="问" * MEMORY_CONCLUSION_CHARS,
                 analysis="长" * MEMORY_CONCLUSION_CHARS,
                 data_results=[{"tool": f"t{k}", "ok": True, "rows": 1,
                                "args": {"x": "字" * _MEMORY_ARGS_CHARS}}
                               for k in range(MEMORY_FACTS_MAX)],
                 research_results=[{"tool": f"r{k}", "ok": True, "rows": 1,
                                    "args": {"y": "字" * _MEMORY_ARGS_CHARS}}
                                   for k in range(3)] +
                                  [{"tool": "conclusion", "text": "研" * _MEMORY_FIELD_CHARS}],
                 report={"executive_summary": "摘" * _MEMORY_FIELD_CHARS,
                         "key_findings": ["发" * MEMORY_CONCLUSION_CHARS] * 3,
                         "recommendations": ["议" * MEMORY_CONCLUSION_CHARS] * 3},
                 ) for i in range(1, 6)]
    out = build_memory(runs, round_no=6)
    mc = out["context"]
    assert _size(mc) <= MEMORY_CHARS_MAX                 # 预算护栏：裁后必不越上限
    assert mc["truncated"] is True                       # 越预算必须留痕
    assert out["stats"]["truncated"] is True
    # 四个 while 各一枚（未裁 hist=4/facts=8/research=3/末轮 conclusion=120；见括注换序反例）：
    # history 的 while 排最前：它丢最旧两轮只留 [4,5]。facts 或 research 的 while 抢在前面 →
    # hist 会停在 4（facts_first）或 3（research_first），这条先红。
    assert [h["round"] for h in mc["history"]] == [4, 5]
    # facts 的 while 在 history 之后：超预算被 history 吸收，facts 满额没动。facts 的 while 若
    # 排到 history 前，它会先 pop → facts 掉到 3，这条红（与上一条从两个方向夹住同一换序）。
    assert len(mc["facts"]) == MEMORY_FACTS_MAX
    # research 的 while 在最后两条明细之后：它满额没动。research 的 while 若排到 history 前，
    # 会被整条抽干 → research=0，这条红。
    assert len(mc["research_briefs"]) == MEMORY_RESEARCH_MAX
    # 末轮 conclusion 折半的 while 排最后：轮次丢够了就不再动结论，长度仍是满值 120。折半的
    # while 若排最前（reviewer 点名的「halve the last conclusion first」换序），120→…→20，这条红。
    assert len(mc["history"][-1]["conclusion"]) == MEMORY_CONCLUSION_CHARS


def test_nothing_usable_yields_no_context_and_honest_stats():
    out = build_memory([], round_no=1)
    assert out["context"] is None
    assert out["stats"] == {"injected": False, "history_rounds": 0, "facts": 0,
                            "research": 0, "truncated": False, "degraded_rounds": []}


def test_include_tuples_are_the_node_wiring_contract():
    assert SUPERVISOR_INCLUDE == ("history", "facts")
    assert BUSINESS_ANALYST_INCLUDE == ("history", "facts", "research_briefs")
    assert REPORT_INCLUDE == ("report_brief", "history")
    assert "report_brief" not in SUPERVISOR_INCLUDE       # 拆任务用不上报告
    assert "facts" not in REPORT_INCLUDE                  # 报告不判断查没查过


def test_render_block_returns_empty_string_without_memory():
    assert render_block(None, include=BUSINESS_ANALYST_INCLUDE) == ""
    empty = {"round": 2, "history": [], "facts": [], "research_briefs": [],
             "report_brief": None, "truncated": False}
    assert render_block(empty, include=BUSINESS_ANALYST_INCLUDE) == ""   # 空标题污染材料


def test_render_block_only_emits_the_included_lines():
    mc = {"round": 2, "history": [{"round": 1, "question": "问", "conclusion": "答"}],
          "facts": ["query_sales({}) → 4 行"],
          "research_briefs": ["调研结论：外部背景"],
          "report_brief": {"executive_summary": "摘要", "key_findings": ["发现"],
                           "recommendations": ["建议"]},
          "truncated": False}
    sup = render_block(mc, include=SUPERVISOR_INCLUDE)
    assert "【历轮结论】" in sup and "【上一轮已查过的数据】" in sup
    assert "【上一轮已检索的外部背景】" not in sup
    assert "【上一轮报告摘要】" not in sup
    rep = render_block(mc, include=REPORT_INCLUDE)
    assert "【上一轮报告摘要】" in rep and "摘要" in rep
    assert "【上一轮已查过的数据】" not in rep
