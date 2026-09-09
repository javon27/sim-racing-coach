import pytest

from simcoach.model.base import TokenUsage
from simcoach.pricing import FREE, PRICES, estimate_chat_cost, estimate_cost

M = "claude-sonnet-5"


def test_estimate_cost_golden_arithmetic():
    rate_in, rate_out, _, _ = PRICES[M]
    c = estimate_cost({"input": 1_000_000, "output": 1_000_000}, M)
    assert c == pytest.approx(rate_in + rate_out)


def test_estimate_cost_counts_cache_read_and_write_separately():
    assert estimate_cost({"cache_read": 1_000_000}, M) == pytest.approx(PRICES[M][2])
    assert estimate_cost({"cache_write": 1_000_000}, M) == pytest.approx(PRICES[M][3])


def test_estimate_cost_accepts_a_token_usage_too():
    u = TokenUsage(input=1_000_000)
    assert estimate_cost(u, M) == pytest.approx(PRICES[M][0])


def test_estimate_cost_of_nothing_is_zero():
    assert estimate_cost({}, M) == 0.0


def test_dated_model_id_resolves_to_its_base_price():
    """factory.py's haiku alias is 'claude-haiku-4-5-20251001' — a dated id must
    not fall off the table."""
    assert estimate_cost({"input": 1_000_000}, "claude-haiku-4-5-20251001") == \
        estimate_cost({"input": 1_000_000}, "claude-haiku-4-5")


def test_unknown_model_returns_none_not_zero():
    """A fabricated $0.00 would mislead a BYO-key driver more than a blank."""
    assert estimate_cost({"input": 1_000_000}, "some-future-model") is None
    assert estimate_cost({"input": 1_000_000}, None) is None
    assert estimate_cost({"input": 1_000_000}, "") is None


def test_a_prefix_of_a_model_id_is_not_a_match():
    """Only the table key may be the prefix, never the other way round — else a
    truncated/garbled id would silently price as a real model."""
    assert estimate_cost({"input": 1}, "claude-sonnet") is None


def test_free_providers_cost_zero():
    assert estimate_cost({"input": 1_000_000}, "fake") == 0.0
    assert estimate_cost({"input": 1_000_000}, "local") == 0.0
    assert set(FREE) >= {"fake", "local"}


def test_estimate_chat_cost_flags_an_incomplete_total():
    total, complete = estimate_chat_cost({
        M: {"input": 1_000_000, "output": 0},
        "mystery-model": {"input": 1_000_000, "output": 0},
    })
    assert complete is False
    assert total == pytest.approx(PRICES[M][0])
    assert estimate_chat_cost({M: {"input": 0, "output": 0}}) == (0.0, True)


def test_estimate_chat_cost_of_an_empty_chat_is_zero_and_complete():
    assert estimate_chat_cost({}) == (0.0, True)


def test_estimate_chat_cost_sums_across_models():
    total, complete = estimate_chat_cost({
        M: {"input": 1_000_000},
        "claude-opus-5": {"input": 1_000_000},
    })
    assert complete is True
    assert total == pytest.approx(PRICES[M][0] + PRICES["claude-opus-5"][0])


def test_estimate_chat_cost_legacy_null_model_rows_are_incomplete_only_if_billed():
    """A pre-v12 row (model "") carrying no tokens must not spoil the total; one
    that somehow carries tokens must."""
    assert estimate_chat_cost({"": {"input": 0}, M: {"input": 1_000_000}})[1] is True
    assert estimate_chat_cost({"": {"input": 10}, M: {"input": 1_000_000}})[1] is False


def test_every_factory_alias_target_is_priced():
    """Guard against a Settings model the meter can't price."""
    from simcoach.model.factory import MODEL_ALIASES
    for mid in MODEL_ALIASES.values():
        assert estimate_cost({"input": 1}, mid) is not None, mid


def test_prices_are_four_positive_rates_with_output_dearer_than_input():
    for mid, rates in PRICES.items():
        assert len(rates) == 4, mid
        assert all(r > 0 for r in rates), mid
        assert rates[1] > rates[0], mid        # output always costs more than input
        assert rates[2] < rates[0], mid        # cache reads are the cheap bucket
