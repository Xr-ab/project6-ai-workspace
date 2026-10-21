"""摘要压缩的 IO 侧（app/ai/graph/memory_summary.py，docs/11 §4.5）。

这一层为什么值得单测：它是记忆链上**唯一会失败**的一环（调模型）。失败姿态必须
是"降级成规则摘要、绝不抛、token 照记"——这三件事写错都不会在 happy path 上显形，
只有把模型打桩成失败/超时/空文本才照得出来。桩打在模块级 `llm_service` 上，
不碰真网络、不烧 token（`-m "not db and not redis"` 的离线面）。
"""
import asyncio
import time

from app.ai.graph.memory import MEMORY_SUMMARY_CHARS
from app.ai.graph.memory_summary import (
    SUMMARY_MIN_CHARS,
    SUMMARY_MIN_ROUNDS,
    SummaryResult,
    _clean,
    render_material,
    summarize_archived,
)


class _FakeLLM:
    """替身：记录调用次数，按剧本返回 (text, prompt_tokens, completion_tokens) 或抛错。"""

    def __init__(self, *, text=None, exc=None, sleep=0.0):
        self.text, self.exc, self.sleep = text, exc, sleep
        self.calls = 0

    async def achat_stats(self, messages):
        self.calls += 1
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.exc:
            raise self.exc
        return self.text or "", 11, 22


def _patch_llm(monkeypatch, fake):
    """按**属性路径**换掉模块里的 llm_service：函数体写的是 `llm_service.achat_stats(...)`，
    import 时绑定的名字在函数外，所以要替换模块属性而不是 import 名。"""
    monkeypatch.setattr("app.ai.graph.memory_summary.llm_service", fake)
    return fake


_ROUNDS = [
    {"round": 1, "question": "华东同比？", "conclusion": "同比 -6.3%"},
    {"round": 2, "question": "那华南呢？", "conclusion": "华南 +4.1%"},
]


def test_clean_strips_prefixes_quotes_and_collapses_newlines():
    assert _clean("摘要：从华东下滑追到家电品类") == "从华东下滑追到家电品类"
    assert _clean("总结:华南走势相反") == "华南走势相反"
    # 换行压成单空格：摘要块在 render_block 里只占一行，多行会把后面几个块带歪
    assert _clean("主线一\n主线二\r\n主线三") == "主线一 主线二 主线三"
    assert _clean('"加引号的摘要"') == "加引号的摘要"
    assert _clean("   ") is None
    assert _clean(None) is None


def test_render_material_skips_blank_rounds():
    text = render_material(_ROUNDS + [{"round": 3, "question": "", "conclusion": ""}])
    assert text.count("第 ") == 2
    assert "第 1 轮" in text and "问题：华东同比？" in text and "结论：同比 -6.3%" in text
    assert render_material([]) == ""


async def test_success_returns_clipped_text_and_token_count(monkeypatch):
    fake = _patch_llm(monkeypatch, _FakeLLM(
        text="  摘要：主线从华东同比下滑一路追到家电品类，再收窄到月度降幅  "))
    result = await summarize_archived(_ROUNDS)
    assert result.ok and fake.calls == 1
    assert result.text == "主线从华东同比下滑一路追到家电品类，再收窄到月度降幅"
    assert (result.prompt_tokens, result.completion_tokens) == (11, 22)
    assert result.error is None
    assert result.duration_ms >= 0


async def test_summary_module_does_not_clip_the_cutoff_lives_in_memory(monkeypatch):
    """本模块**不**截断：截断口径只有一份（memory 的 clip_text），两边各裁一次就会
    出现"摘要 200 字、落库 180 字"的不一致。这里钉的就是"原样交回"这个约定。"""
    _patch_llm(monkeypatch, _FakeLLM(text="摘" * (MEMORY_SUMMARY_CHARS + 80)))
    result = await summarize_archived(_ROUNDS)
    assert len(result.text) == MEMORY_SUMMARY_CHARS + 80


async def test_failure_degrades_without_raising(monkeypatch):
    _patch_llm(monkeypatch, _FakeLLM(exc=RuntimeError("连接被重置")))
    result = await summarize_archived(_ROUNDS)
    assert not result.ok and result.text is None
    assert "摘要调用失败" in result.error and "RuntimeError" in result.error
    # token 记 0：失败那次的用量拿不到（没返回 usage），宁可少记也不能编
    assert (result.prompt_tokens, result.completion_tokens) == (0, 0)
    assert result.finished_at >= result.started_at


async def test_timeout_degrades_without_raising(monkeypatch):
    fake = _patch_llm(monkeypatch, _FakeLLM(text="太慢的摘要", sleep=0.05))
    monkeypatch.setattr("app.ai.graph.memory_summary.SUMMARY_TIMEOUT_SECONDS", 0.01)
    result = await summarize_archived(_ROUNDS)
    assert not result.ok and fake.calls == 1
    assert "超时" in result.error


async def test_empty_output_is_an_error_not_a_silent_success(monkeypatch):
    """空文本不能当"摘要成功"：否则 summary 是空串、stats 却写着 model，事后无从查。"""
    _patch_llm(monkeypatch, _FakeLLM(text="   "))
    result = await summarize_archived(_ROUNDS)
    assert not result.ok and "空文本" in result.error
    # 请求成功返回了、usage 也拿到了：判废不能把 token 一起吞了（真库实测漏过）
    assert (result.prompt_tokens, result.completion_tokens) == (11, 22)


async def test_no_material_means_no_model_call(monkeypatch):
    fake = _patch_llm(monkeypatch, _FakeLLM(text="不该被调用"))
    result = await summarize_archived([])
    assert fake.calls == 0
    assert not result.ok and result.error is None   # 没尝试过，所以不是失败


async def test_single_round_never_reaches_the_model(monkeypatch):
    """单轮没有"主线"可总结：模型只能复读，规则版的问题链信息量更高还不要钱。

    真库实测（2026-10-21 e2e 第 6 轮，只压 1 轮）模型返回「数据。」3 字 ——
    这种输出占着【更早轮次摘要】的标题却不承载任何信息，比规则摘要更糟。
    """
    fake = _patch_llm(monkeypatch, _FakeLLM(text="数据。"))
    result = await summarize_archived(_ROUNDS[:1])
    assert fake.calls == 0
    assert not result.ok and result.error is None   # 不是失败，是"不值得调"
    assert SUMMARY_MIN_ROUNDS == 2                  # 门本身就是断言的一部分


async def test_output_shorter_than_the_quality_floor_is_rejected(monkeypatch):
    """质量闸：短到承载不了主线的一句话一律判不可用，回退规则摘要。

    第二个值是 e2e 第 7 轮的真实输出（压 2 轮时模型抄了半个句子）。
    """
    _patch_llm(monkeypatch, _FakeLLM(text="者查询条件设置不正确。"))
    result = await summarize_archived(_ROUNDS)
    assert not result.ok
    assert "过短" in result.error and str(SUMMARY_MIN_CHARS) in result.error
    # 判废也记 token：调用真发生了、钱真花了（真库第 7 轮的 error span 曾写着 tokens=0）
    assert (result.prompt_tokens, result.completion_tokens) == (11, 22)
    assert result.attempted is True


async def test_attempted_flag_separates_skip_from_real_call(monkeypatch):
    """attempted = "有没有真发请求"的唯一口径，task_runner 靠它决定落不落 span。

    合法跳过（原料不足两道门）attempted=False：没花 token，记进 agent_runs 就是
    一行 tokens=0 / status=error 的幽灵 span，成本面板会当成一次失败调用；
    真调了模型（成功/抛错/超时/判废）attempted=True：token 花了，降级也要留行。
    """
    skip_single = await summarize_archived(_ROUNDS[:1])
    skip_empty = await summarize_archived([])
    assert skip_single.attempted is False and skip_empty.attempted is False

    _patch_llm(monkeypatch, _FakeLLM(text="主线从华东同比下滑一路追到家电品类，再收窄到月度降幅"))
    assert (await summarize_archived(_ROUNDS)).attempted is True

    _patch_llm(monkeypatch, _FakeLLM(exc=RuntimeError("连接被重置")))
    assert (await summarize_archived(_ROUNDS)).attempted is True


def test_span_span_shape_is_consumed_by_the_persist_side():
    """摘要 span 的键必须与 node_guard 写的形状同族（_persist_agent_runs 逐个读）。"""
    keys = {"span_id", "node", "status", "started_at", "finished_at",
            "duration_ms", "error_message", "prompt_tokens", "completion_tokens", "output"}
    result = SummaryResult(text="x", prompt_tokens=1, completion_tokens=2, duration_ms=3)
    span = {
        "span_id": "id", "node": "memory_summary",
        "status": "ok" if result.ok else "error",
        "started_at": result.started_at, "finished_at": result.finished_at,
        "duration_ms": result.duration_ms, "error_message": result.error,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "output": {"content": result.text or ""},
    }
    assert keys <= set(span)
