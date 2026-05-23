"""Pricing conversion tests."""

from __future__ import annotations

import pytest

from droids_agents import pricing


def test_usd_to_max_total_tokens_one_usd() -> None:
    # $1 / $11.4 per Mtok ≈ 87_719 tokens
    tokens = pricing.usd_to_max_total_tokens(1.0)
    assert 80_000 < tokens < 95_000


def test_usd_to_max_total_tokens_monotonic() -> None:
    a = pricing.usd_to_max_total_tokens(0.5)
    b = pricing.usd_to_max_total_tokens(5.0)
    assert b > a > 0


@pytest.mark.parametrize("bad", [0, -1.0, -0.0001])
def test_usd_to_max_total_tokens_rejects_non_positive(bad: float) -> None:
    with pytest.raises(ValueError):
        pricing.usd_to_max_total_tokens(bad)


def test_estimate_cost_usd_sonnet_known_rates() -> None:
    # 1M prompt @ $3 + 1M completion @ $15 = $18
    cost = pricing.estimate_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == pytest.approx(18.0, rel=1e-6)


def test_estimate_cost_usd_haiku_known_rates() -> None:
    # 1M prompt @ $1 + 1M completion @ $5 = $6
    cost = pricing.estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(6.0, rel=1e-6)


def test_estimate_cost_usd_unknown_model_raises() -> None:
    with pytest.raises(KeyError):
        pricing.estimate_cost_usd("claude-mystery-9-0", 1000, 1000)
