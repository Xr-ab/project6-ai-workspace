"""token → 钱的唯一计算处（app/ai/pricing.py:12 / 15 / 25 / 40）。

判据用 `is None` 而不是真值：0.0 是合法单价（免费模型），用真值判断会把它读成未配置。
"""
import app.ai.pricing as pricing
from app.ai.pricing import _COST_ROUND, compute_cost, currency, price_configured


def _set_prices(monkeypatch, inp, outp, unit="CNY"):
    monkeypatch.setattr(pricing.settings, "llm_input_price_per_1k", inp)
    monkeypatch.setattr(pricing.settings, "llm_output_price_per_1k", outp)
    monkeypatch.setattr(pricing.settings, "llm_price_currency", unit)


def test_both_sides_required_before_any_number_is_emitted(monkeypatch):
    _set_prices(monkeypatch, 1.0, None)
    assert price_configured() is False
    _set_prices(monkeypatch, None, 2.0)
    assert price_configured() is False
    _set_prices(monkeypatch, 1.0, 2.0)
    assert price_configured() is True


def test_zero_price_is_configured_not_missing(monkeypatch):
    _set_prices(monkeypatch, 0.0, 0.0)
    assert price_configured() is True
    assert compute_cost(1000, 1000) == 0.0


def test_none_means_unavailable_not_zero(monkeypatch):
    _set_prices(monkeypatch, None, None)
    assert compute_cost(1000, 1000) is None


def test_arithmetic_and_six_digit_rounding_match_the_column_width(monkeypatch):
    _set_prices(monkeypatch, 1.0, 2.0)
    assert compute_cost(1500, 500) == 2.5
    assert _COST_ROUND == 6
    _set_prices(monkeypatch, 0.1234567, 0.0)
    assert compute_cost(1000, 0) == round(0.1234567, 6)


def test_negative_and_missing_token_counts_clamp_to_zero(monkeypatch):
    _set_prices(monkeypatch, 1.0, 1.0)
    assert compute_cost(-10, 5) == 0.005
    assert compute_cost(None, None) == 0.0


def test_currency_follows_the_price_source(monkeypatch):
    _set_prices(monkeypatch, 1.0, 1.0, unit="USD")
    assert currency() == "USD"
