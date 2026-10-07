"""Tests for app.data.validation: require_olive_tradable_contract and
normalize_and_validate_bars.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.futures.models import ContractMonth, FuturesContract, FuturesDomainError
from app.futures.registry import load_instrument_registry
from app.data.models import (
    DataLabel,
    HistoricalBar,
    HistoricalBarContractMismatchError,
    HistoricalBarConflictError,
    HistoricalBarOutOfRangeError,
    HistoricalBarRequest,
    HistoricalDataError,
    HistoricalTimeframe,
    NotOliveTradableContractError,
)
from app.data.validation import normalize_and_validate_bars, require_olive_tradable_contract

UTC = timezone.utc


@pytest.fixture(scope="module")
def registry():
    return load_instrument_registry()


NQ_CONTRACT = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)
MNQ_CONTRACT = FuturesContract(root_symbol="MNQ", year=2026, month=ContractMonth.DECEMBER)
ES_CONTRACT = FuturesContract(root_symbol="ES", year=2026, month=ContractMonth.DECEMBER)


def make_bar(**overrides) -> HistoricalBar:
    base = dict(
        root_symbol="NQ",
        contract_year=2026,
        contract_month=12,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        open_ticks=100000,
        high_ticks=100020,
        low_ticks=99980,
        close_ticks=100010,
        volume=10,
        tick_size=Decimal("0.25"),
        data_label=DataLabel.HISTORICAL,
        provider="databento",
        dataset="GLBX.MDP3",
        provider_raw_symbol="NQZ6",
        provider_instrument_id="999",
    )
    base.update(overrides)
    return HistoricalBar(**base)


def make_request(**overrides) -> HistoricalBarRequest:
    base = dict(
        contract=NQ_CONTRACT,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 15, 10, 5, tzinfo=UTC),
    )
    base.update(overrides)
    return HistoricalBarRequest(**base)


# -- require_olive_tradable_contract ----------------------------------------


def test_nq_is_tradable(registry):
    instrument = require_olive_tradable_contract(NQ_CONTRACT, registry)
    assert instrument.root_symbol == "NQ"
    assert instrument.tick_size == Decimal("0.25")


def test_mnq_is_tradable(registry):
    instrument = require_olive_tradable_contract(MNQ_CONTRACT, registry)
    assert instrument.root_symbol == "MNQ"


def test_es_is_rejected_as_not_tradable(registry):
    with pytest.raises(NotOliveTradableContractError):
        require_olive_tradable_contract(ES_CONTRACT, registry)


@pytest.mark.parametrize("bad_contract", [None, "NQZ6", 123, object(), {"root_symbol": "NQ"}])
def test_rejects_wrong_contract_type_as_historical_data_error_not_futures_error(bad_contract, registry):
    """Regression test: app.data.validation.require_olive_tradable_contract
    must translate a futures-domain type error into its own
    NotOliveTradableContractError (a HistoricalDataError), never let the
    underlying app.futures.models.FuturesDomainError leak through this
    boundary -- this was found to leak during Phase 3's own adversarial
    test-writing pass (the exact "Phase 2.4-style boundary leak" CLAUDE.md
    warns about) and was fixed in app.data.validation /
    app.data.models alongside this test.
    """
    with pytest.raises(NotOliveTradableContractError):
        require_olive_tradable_contract(bad_contract, registry)
    # and, separately, confirm it's never the raw futures-domain type:
    try:
        require_olive_tradable_contract(bad_contract, registry)
    except HistoricalDataError as exc:
        assert not isinstance(exc, FuturesDomainError)


@pytest.mark.parametrize("bad_registry", [None, "registry", 123, object()])
def test_rejects_wrong_registry_type_as_historical_data_error(bad_registry):
    with pytest.raises(NotOliveTradableContractError):
        require_olive_tradable_contract(NQ_CONTRACT, bad_registry)


# -- normalize_and_validate_bars ---------------------------------------------


def test_normalize_sorts_out_of_order_bars():
    req = make_request()
    bar_late = make_bar(ts_event=datetime(2026, 9, 15, 10, 3, tzinfo=UTC))
    bar_early = make_bar(ts_event=datetime(2026, 9, 15, 10, 1, tzinfo=UTC))
    result = normalize_and_validate_bars([bar_late, bar_early], request=req)
    assert [b.ts_event for b in result] == [bar_early.ts_event, bar_late.ts_event]


def test_normalize_dedupes_identical_duplicate():
    req = make_request()
    bar = make_bar(ts_event=datetime(2026, 9, 15, 10, 1, tzinfo=UTC))
    bar_dup = make_bar(ts_event=datetime(2026, 9, 15, 10, 1, tzinfo=UTC))
    result = normalize_and_validate_bars([bar, bar_dup], request=req)
    assert len(result) == 1


def test_normalize_raises_on_conflicting_duplicate():
    req = make_request()
    bar = make_bar(ts_event=datetime(2026, 9, 15, 10, 1, tzinfo=UTC), open_ticks=100000, close_ticks=100010)
    bar_conflict = make_bar(
        ts_event=datetime(2026, 9, 15, 10, 1, tzinfo=UTC), open_ticks=200000, high_ticks=200020,
        low_ticks=199980, close_ticks=200010,
    )
    with pytest.raises(HistoricalBarConflictError):
        normalize_and_validate_bars([bar, bar_conflict], request=req)


def test_normalize_rejects_wrong_root_symbol():
    req = make_request()
    wrong = make_bar(root_symbol="MNQ")
    with pytest.raises(HistoricalBarContractMismatchError):
        normalize_and_validate_bars([wrong], request=req)


def test_normalize_rejects_wrong_contract_year():
    req = make_request()
    wrong = make_bar(contract_year=2027)
    with pytest.raises(HistoricalBarContractMismatchError):
        normalize_and_validate_bars([wrong], request=req)


def test_normalize_rejects_wrong_contract_month():
    req = make_request()
    wrong = make_bar(contract_month=9)
    with pytest.raises(HistoricalBarContractMismatchError):
        normalize_and_validate_bars([wrong], request=req)


def test_normalize_rejects_wrong_timeframe():
    req = make_request(timeframe=HistoricalTimeframe.ONE_MINUTE)
    wrong = make_bar(timeframe=HistoricalTimeframe.ONE_HOUR)
    with pytest.raises(HistoricalBarContractMismatchError):
        normalize_and_validate_bars([wrong], request=req)


def test_normalize_rejects_timestamp_before_start():
    req = make_request(
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 10, 5, tzinfo=UTC)
    )
    too_early = make_bar(ts_event=datetime(2026, 9, 15, 9, 59, tzinfo=UTC))
    with pytest.raises(HistoricalBarOutOfRangeError):
        normalize_and_validate_bars([too_early], request=req)


def test_normalize_rejects_timestamp_at_or_after_end_exclusive():
    req = make_request(
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 10, 5, tzinfo=UTC)
    )
    at_end = make_bar(ts_event=datetime(2026, 9, 15, 10, 5, tzinfo=UTC))  # end is exclusive
    with pytest.raises(HistoricalBarOutOfRangeError):
        normalize_and_validate_bars([at_end], request=req)


def test_normalize_accepts_timestamp_exactly_at_start_inclusive():
    req = make_request(
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 10, 5, tzinfo=UTC)
    )
    at_start = make_bar(ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=UTC))
    result = normalize_and_validate_bars([at_start], request=req)
    assert len(result) == 1


def test_normalize_rejects_non_historicalbar_elements():
    req = make_request()
    with pytest.raises(HistoricalBarContractMismatchError):
        normalize_and_validate_bars([{"not": "a bar"}], request=req)


def test_normalize_rejects_malformed_request():
    with pytest.raises(HistoricalDataError):
        normalize_and_validate_bars([], request="not a request")


def test_normalize_accepts_empty_bars():
    req = make_request()
    assert normalize_and_validate_bars([], request=req) == ()


def test_normalize_accepts_generator_input():
    req = make_request()
    bar = make_bar(ts_event=datetime(2026, 9, 15, 10, 1, tzinfo=UTC))

    def gen():
        yield bar

    result = normalize_and_validate_bars(gen(), request=req)
    assert len(result) == 1
