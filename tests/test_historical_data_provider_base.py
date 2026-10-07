"""Tests for app.data.provider_base: CostEstimate's own validation, and
that HistoricalMarketDataProvider cannot be instantiated directly (it is
an ABC every concrete provider must implement in full).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.data.models import InvalidHistoricalRequestError
from app.data.provider_base import CostEstimate, HistoricalMarketDataProvider


def test_cost_estimate_accepts_zero():
    estimate = CostEstimate(estimated_cost_usd=Decimal("0"))
    assert estimate.estimated_cost_usd == Decimal("0")


def test_cost_estimate_accepts_positive_decimal():
    estimate = CostEstimate(estimated_cost_usd=Decimal("3.14"))
    assert estimate.estimated_cost_usd == Decimal("3.14")


def test_cost_estimate_accepts_int_and_numeric_str():
    assert CostEstimate(estimated_cost_usd=5).estimated_cost_usd == Decimal("5")
    assert CostEstimate(estimated_cost_usd="2.50").estimated_cost_usd == Decimal("2.50")


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_cost_estimate_rejects_non_finite(bad):
    with pytest.raises(InvalidHistoricalRequestError):
        CostEstimate(estimated_cost_usd=bad)


def test_cost_estimate_rejects_negative():
    with pytest.raises(InvalidHistoricalRequestError):
        CostEstimate(estimated_cost_usd=Decimal("-0.01"))


def test_cost_estimate_rejects_bool():
    with pytest.raises(InvalidHistoricalRequestError):
        CostEstimate(estimated_cost_usd=True)
    with pytest.raises(InvalidHistoricalRequestError):
        CostEstimate(estimated_cost_usd=False)


def test_cost_estimate_rejects_float():
    # Phase 3 final completion pass (§9 direct retest): a bare float
    # (distinct from a Decimal) must still be rejected -- float is
    # intentionally not in the accepted-type set (Decimal/int/numeric
    # str only), unlike int, so this is not an equality-trap concern,
    # merely a type-acceptance one.
    with pytest.raises(InvalidHistoricalRequestError):
        CostEstimate(estimated_cost_usd=1.0)


@pytest.mark.parametrize("bad", [None, object(), [], {}])
def test_cost_estimate_rejects_malformed_types(bad):
    with pytest.raises(InvalidHistoricalRequestError):
        CostEstimate(estimated_cost_usd=bad)


def test_cost_estimate_rejects_unparseable_string():
    with pytest.raises(InvalidHistoricalRequestError):
        CostEstimate(estimated_cost_usd="not-a-number")


def test_provider_abc_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        HistoricalMarketDataProvider()


def test_provider_abc_rejects_partial_implementation():
    class Partial(HistoricalMarketDataProvider):
        @property
        def name(self):
            return "partial"
        # estimate_cost / fetch_bars deliberately not implemented

    with pytest.raises(TypeError):
        Partial()
