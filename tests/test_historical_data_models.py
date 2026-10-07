"""Tests for app.data.models: the historical-data error hierarchy,
HistoricalTimeframe, DataLabel, HistoricalBarRequest, HistoricalBar, and
HistoricalFetchResult/HistoricalFetchStatus.

Per CLAUDE.md's standing adversarial self-review policy, this file does
not stop at the happy path: every public dataclass is probed with
malformed types, None, bool-as-int, naive datetimes, plain dates,
NaN/Infinity, negative/zero values, and boundary conditions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.futures.models import ContractMonth, FuturesContract
from app.data.models import (
    ARROW_INT64_MAX,
    DataLabel,
    HistoricalBar,
    HistoricalBarRequest,
    HistoricalDataError,
    HistoricalFetchResult,
    HistoricalFetchStatus,
    HistoricalPriceTickMisalignedError,
    HistoricalTimeframe,
    InvalidHistoricalBarError,
    InvalidHistoricalRequestError,
    OLIVE_HISTORICAL_BAR_SCHEMA_VERSION,
    _coerce_timeframe,
    _require_historical_bar_request,
)

UTC = timezone.utc
NQ_CONTRACT = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)


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


# -- HistoricalTimeframe / _coerce_timeframe --------------------------------


def test_timeframe_databento_schema_mapping():
    assert HistoricalTimeframe.ONE_SECOND.databento_schema == "ohlcv-1s"
    assert HistoricalTimeframe.ONE_MINUTE.databento_schema == "ohlcv-1m"
    assert HistoricalTimeframe.ONE_HOUR.databento_schema == "ohlcv-1h"
    assert HistoricalTimeframe.ONE_DAY.databento_schema == "ohlcv-1d"


def test_coerce_timeframe_accepts_enum_and_value_string():
    assert _coerce_timeframe(HistoricalTimeframe.ONE_DAY) is HistoricalTimeframe.ONE_DAY
    assert _coerce_timeframe("ONE_DAY") is HistoricalTimeframe.ONE_DAY


@pytest.mark.parametrize("bad", [None, 123, True, "ohlcv-1m", "", object(), 1.5])
def test_coerce_timeframe_rejects_malformed_values(bad):
    with pytest.raises(InvalidHistoricalRequestError):
        _coerce_timeframe(bad)


# -- HistoricalBarRequest ---------------------------------------------------


def test_request_normalizes_non_utc_input_to_utc():
    chicago = timezone(timedelta(hours=-5))
    start = datetime(2026, 9, 1, 9, 0, tzinfo=chicago)
    end = datetime(2026, 9, 1, 10, 0, tzinfo=chicago)
    req = HistoricalBarRequest(contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE, start=start, end=end)
    assert req.start.tzinfo is UTC
    assert req.start == datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    assert req.end == datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


def test_request_accepts_utc_input():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2026, 9, 2, tzinfo=UTC)
    req = HistoricalBarRequest(contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_DAY, start=start, end=end)
    assert req.start == start
    assert req.end == end
    assert req.root_symbol == "NQ"


def test_request_rejects_wrong_contract_type():
    with pytest.raises(HistoricalDataError):
        HistoricalBarRequest(
            contract="NQZ6", timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 2, tzinfo=UTC),
        )


def test_request_rejects_none_contract():
    with pytest.raises(HistoricalDataError):
        HistoricalBarRequest(
            contract=None, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 2, tzinfo=UTC),
        )


@pytest.mark.parametrize("bad_timeframe", [None, "1m", 60, True, object()])
def test_request_rejects_malformed_timeframe(bad_timeframe):
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(
            contract=NQ_CONTRACT, timeframe=bad_timeframe,
            start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 2, tzinfo=UTC),
        )


def test_request_rejects_naive_start():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 1), end=datetime(2026, 9, 2, tzinfo=UTC),
        )


def test_request_rejects_naive_end():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 2),
        )


def test_request_rejects_plain_date_start():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=date(2026, 9, 1), end=datetime(2026, 9, 2, tzinfo=UTC),
        )


def test_request_rejects_plain_date_end():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 1, tzinfo=UTC), end=date(2026, 9, 2),
        )


def test_request_rejects_start_equal_end():
    moment = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE, start=moment, end=moment)


def test_request_rejects_start_after_end():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 2, tzinfo=UTC), end=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_request_rejects_start_equal_end_across_equivalent_timezones():
    # Phase 3 final completion pass (§11 direct retest): the SAME
    # instant, expressed through two different UTC offsets, must still
    # be rejected as start==end -- not accidentally treated as
    # start < end merely because the two literal offset strings differ.
    chicago = timezone(timedelta(hours=-5))
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            end=datetime(2026, 9, 1, 7, 0, tzinfo=chicago),  # == 12:00 UTC, same instant
        )


def test_request_rejects_start_after_end_across_equivalent_timezones():
    # start in UTC+0 is actually later than end in UTC-5 numerically
    # equal-looking local times -- must compare on true UTC instants.
    chicago = timezone(timedelta(hours=-5))
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalBarRequest(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            end=datetime(2026, 9, 1, 6, 59, tzinfo=chicago),  # == 11:59 UTC
        )


def test_require_historical_bar_request_passes_through_valid_instance():
    req = HistoricalBarRequest(
        contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert _require_historical_bar_request(req, context="test") is req


@pytest.mark.parametrize("bad", [None, "req", 123, {}, object()])
def test_require_historical_bar_request_rejects_non_request(bad):
    with pytest.raises(InvalidHistoricalRequestError):
        _require_historical_bar_request(bad, context="test")


# -- HistoricalBar -----------------------------------------------------------


def test_bar_happy_path_properties():
    bar = make_bar()
    assert bar.open == Decimal("25000.00")
    assert bar.high == Decimal("25005.00")
    assert bar.low == Decimal("24995.00")
    assert bar.close == Decimal("25002.50")
    assert bar.contract_identity == "NQ-2026-12"
    assert bar.schema_version == OLIVE_HISTORICAL_BAR_SCHEMA_VERSION


def test_bar_normalizes_root_symbol_case_and_whitespace():
    bar = make_bar(root_symbol=" nq ")
    assert bar.root_symbol == "NQ"


@pytest.mark.parametrize("bad", [None, 123, True, "", "   ", 1.5, object()])
def test_bar_rejects_malformed_root_symbol(bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(root_symbol=bad)


@pytest.mark.parametrize("bad", [None, "2026", True, 2026.0, object()])
def test_bar_rejects_bool_or_wrong_type_contract_year(bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(contract_year=bad)


@pytest.mark.parametrize("bad_month", [1, 2, 4, 5, 7, 8, 10, 11, 0, -3, True])
def test_bar_rejects_non_quarterly_contract_month(bad_month):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(contract_month=bad_month)


def test_bar_rejects_naive_ts_event():
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(ts_event=datetime(2026, 9, 15, 10, 0))


def test_bar_rejects_non_utc_ts_event():
    chicago = timezone(timedelta(hours=-5))
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=chicago))


def test_bar_rejects_plain_date_ts_event():
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(ts_event=date(2026, 9, 15))


@pytest.mark.parametrize("field_name", ["open_ticks", "high_ticks", "low_ticks", "close_ticks"])
@pytest.mark.parametrize("bad", [None, True, 1.5, "100000", object()])
def test_bar_rejects_bool_float_str_ticks(field_name, bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(**{field_name: bad})


@pytest.mark.parametrize("field_name", ["open_ticks", "high_ticks", "low_ticks", "close_ticks"])
@pytest.mark.parametrize("bad", [0, -1, -100000])
def test_bar_rejects_non_positive_ticks(field_name, bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(**{field_name: bad})


def test_bar_rejects_high_not_maximum():
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(open_ticks=100000, high_ticks=99000, low_ticks=98000, close_ticks=99500)


def test_bar_rejects_low_not_minimum():
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(open_ticks=100000, high_ticks=100020, low_ticks=100005, close_ticks=100010)


def test_bar_accepts_doji_all_equal_prices():
    bar = make_bar(open_ticks=100000, high_ticks=100000, low_ticks=100000, close_ticks=100000)
    assert bar.open == bar.high == bar.low == bar.close


@pytest.mark.parametrize("bad", [None, True, 1.5, "10", object()])
def test_bar_rejects_bool_float_str_volume(bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(volume=bad)


def test_bar_rejects_negative_volume():
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(volume=-1)


def test_bar_accepts_zero_volume():
    bar = make_bar(volume=0)
    assert bar.volume == 0


# -- Phase 3.3 §13: ARROW_INT64_MAX storage-representability bounds --------
# Olive's Parquet schema declares open/high/low/close_ticks and volume as
# signed 64-bit integers (pa.int64()); a Python int has no such bound
# (2**63, 2**80, ... all construct without error), so HistoricalBar must
# enforce this itself rather than defer to a later pyarrow serialization
# failure.


@pytest.mark.parametrize("field_name", ["open_ticks", "high_ticks", "low_ticks", "close_ticks"])
def test_bar_accepts_tick_count_at_exactly_arrow_int64_max(field_name):
    # high_ticks must remain the maximum and low_ticks the minimum, so
    # only high_ticks can actually BE ARROW_INT64_MAX while every other
    # field is independently tested at that same boundary with the
    # others held fixed below it.
    if field_name == "high_ticks":
        bar = make_bar(high_ticks=ARROW_INT64_MAX)
        assert bar.high_ticks == ARROW_INT64_MAX
    else:
        bar = make_bar(**{field_name: 100000, "high_ticks": ARROW_INT64_MAX})
        assert getattr(bar, field_name) == 100000


def test_bar_accepts_high_ticks_at_exactly_arrow_int64_max_with_others_small():
    bar = make_bar(open_ticks=1, high_ticks=ARROW_INT64_MAX, low_ticks=1, close_ticks=1)
    assert bar.high_ticks == ARROW_INT64_MAX


@pytest.mark.parametrize("field_name", ["open_ticks", "high_ticks", "low_ticks", "close_ticks"])
def test_bar_rejects_tick_count_exceeding_arrow_int64_max(field_name):
    over_max = ARROW_INT64_MAX + 1
    # Keep OHLC ordering valid for every field except the one actually
    # being pushed over the bound (which must always end up as the
    # maximum, so the bound check -- not the ordering check -- is what
    # fires).
    kwargs = dict(open_ticks=1, high_ticks=over_max, low_ticks=1, close_ticks=1)
    if field_name != "high_ticks":
        kwargs[field_name] = over_max
        kwargs["high_ticks"] = over_max
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(**kwargs)


def test_bar_rejects_volume_exceeding_arrow_int64_max():
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(volume=ARROW_INT64_MAX + 1)


def test_bar_accepts_volume_at_exactly_arrow_int64_max():
    bar = make_bar(volume=ARROW_INT64_MAX)
    assert bar.volume == ARROW_INT64_MAX


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_bar_rejects_non_finite_tick_size(bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(tick_size=bad)


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-0.25")])
def test_bar_rejects_non_positive_tick_size(bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(tick_size=bad)


@pytest.mark.parametrize("bad_label", [DataLabel.LIVE, DataLabel.DELAYED, DataLabel.SIMULATED, DataLabel.DEMO])
def test_bar_rejects_every_non_historical_data_label(bad_label):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(data_label=bad_label)


@pytest.mark.parametrize("bad", [None, "HISTORICAL", 1, object()])
def test_bar_rejects_malformed_data_label(bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(data_label=bad)


@pytest.mark.parametrize("field_name", ["provider", "dataset", "provider_raw_symbol"])
@pytest.mark.parametrize("bad", [None, "", "   ", 123])
def test_bar_rejects_malformed_provenance_strings(field_name, bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(**{field_name: bad})


def test_bar_accepts_none_provider_instrument_id():
    bar = make_bar(provider_instrument_id=None)
    assert bar.provider_instrument_id is None


def test_bar_rejects_non_string_provider_instrument_id():
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(provider_instrument_id=12345)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_bar_rejects_non_positive_schema_version(bad):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(schema_version=bad)


def test_bar_canonical_key_and_conflicts_with():
    bar1 = make_bar()
    bar2 = make_bar()  # identical content, same key
    assert bar1.canonical_key == bar2.canonical_key
    assert not bar1.conflicts_with(bar2)

    different_price = make_bar(open_ticks=200000, high_ticks=200020, low_ticks=199980, close_ticks=200010)
    assert bar1.canonical_key == different_price.canonical_key
    assert bar1.conflicts_with(different_price)

    different_time = make_bar(ts_event=datetime(2026, 9, 15, 10, 1, tzinfo=UTC))
    assert bar1.canonical_key != different_time.canonical_key
    assert not bar1.conflicts_with(different_time)  # different key -> not a "conflict", just unrelated


def test_conflicts_with_detects_differing_provider_instrument_id():
    """Phase 3.1 §23 regression: two bars sharing a canonical key and
    IDENTICAL OHLCV but a DIFFERENT provider_instrument_id must be
    detected as a conflict, not silently de-duplicated as if
    identical -- a provider's symbol-to-instrument mapping disagreeing
    across fetches is exactly the kind of provenance disagreement
    de-duplication must never discard."""
    bar1 = make_bar(provider_instrument_id="111")
    bar2 = make_bar(provider_instrument_id="222")
    assert bar1.canonical_key == bar2.canonical_key
    assert bar1.conflicts_with(bar2)


def test_conflicts_with_detects_differing_schema_version():
    bar1 = make_bar(schema_version=OLIVE_HISTORICAL_BAR_SCHEMA_VERSION)
    bar2 = make_bar(schema_version=OLIVE_HISTORICAL_BAR_SCHEMA_VERSION + 1 if OLIVE_HISTORICAL_BAR_SCHEMA_VERSION > 0 else 2)
    assert bar1.canonical_key == bar2.canonical_key
    assert bar1.conflicts_with(bar2)


# -- HistoricalBar.from_decimal_prices --------------------------------------


def test_from_decimal_prices_happy_path_exact_tick_alignment():
    bar = HistoricalBar.from_decimal_prices(
        contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
        ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        open_price=Decimal("25000.00"), high_price=Decimal("25001.00"),
        low_price=Decimal("24999.75"), close_price=Decimal("25000.50"),
        volume=100, tick_size=Decimal("0.25"), provider="databento", dataset="GLBX.MDP3",
        provider_raw_symbol="NQZ6",
    )
    assert bar.open_ticks == 100000
    assert bar.open == Decimal("25000.00")


@pytest.mark.parametrize("misaligned_price", [Decimal("25000.10"), Decimal("25000.01"), Decimal("25000.126")])
def test_from_decimal_prices_rejects_misaligned_price_never_rounds(misaligned_price):
    with pytest.raises(HistoricalPriceTickMisalignedError):
        HistoricalBar.from_decimal_prices(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
            open_price=misaligned_price, high_price=misaligned_price,
            low_price=misaligned_price, close_price=misaligned_price,
            volume=1, tick_size=Decimal("0.25"), provider="databento", dataset="GLBX.MDP3",
            provider_raw_symbol="NQZ6",
        )


def test_from_decimal_prices_rejects_non_finite_price():
    with pytest.raises(InvalidHistoricalBarError):
        HistoricalBar.from_decimal_prices(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
            open_price=Decimal("NaN"), high_price=Decimal("25001.00"),
            low_price=Decimal("24999.75"), close_price=Decimal("25000.50"),
            volume=1, tick_size=Decimal("0.25"), provider="databento", dataset="GLBX.MDP3",
            provider_raw_symbol="NQZ6",
        )


def test_from_decimal_prices_rejects_non_positive_tick_size():
    with pytest.raises(InvalidHistoricalBarError):
        HistoricalBar.from_decimal_prices(
            contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
            ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
            open_price=Decimal("25000.00"), high_price=Decimal("25001.00"),
            low_price=Decimal("24999.75"), close_price=Decimal("25000.50"),
            volume=1, tick_size=Decimal("0"), provider="databento", dataset="GLBX.MDP3",
            provider_raw_symbol="NQZ6",
        )


def test_from_decimal_prices_rejects_wrong_contract_type():
    with pytest.raises(HistoricalDataError):
        HistoricalBar.from_decimal_prices(
            contract="NQZ6", timeframe=HistoricalTimeframe.ONE_MINUTE,
            ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
            open_price=Decimal("25000.00"), high_price=Decimal("25001.00"),
            low_price=Decimal("24999.75"), close_price=Decimal("25000.50"),
            volume=1, tick_size=Decimal("0.25"), provider="databento", dataset="GLBX.MDP3",
            provider_raw_symbol="NQZ6",
        )


def test_from_decimal_prices_accepts_int_and_str_prices():
    bar = HistoricalBar.from_decimal_prices(
        contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE,
        ts_event=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        open_price="25000.00", high_price="25001.00",
        low_price="24999.75", close_price="25000.50",
        volume=1, tick_size=Decimal("0.25"), provider="databento", dataset="GLBX.MDP3",
        provider_raw_symbol="NQZ6",
    )
    assert bar.open == Decimal("25000.00")


# -- HistoricalFetchResult / HistoricalFetchStatus --------------------------


def test_fetch_status_is_success_property():
    assert HistoricalFetchStatus.SUCCESS.is_success
    assert HistoricalFetchStatus.SUCCESS_EMPTY.is_success
    for status in HistoricalFetchStatus:
        if status not in (HistoricalFetchStatus.SUCCESS, HistoricalFetchStatus.SUCCESS_EMPTY):
            assert not status.is_success


def test_fetch_result_rejects_malformed_status():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status="SUCCESS")


def test_fetch_result_defaults():
    result = HistoricalFetchResult(status=HistoricalFetchStatus.NOT_CONFIGURED)
    assert result.bars == ()
    assert result.message == ""
    assert result.estimated_cost_usd is None
    assert result.new_records_stored is None


# -- HistoricalFetchResult Phase 3.1 hardening (§22) -------------------------
# Independent review constructed instances with bars=None,
# estimated_cost_usd=NaN, and new_records_stored=-1 with no error --
# an impossible result state for a trustworthy structured outcome.


@pytest.mark.parametrize("bad_message", [None, 123, b"bytes", object()])
def test_fetch_result_rejects_non_str_message(bad_message):
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.FAILED, message=bad_message)


@pytest.mark.parametrize("bad_bars", [None, "not a tuple", 123, {"a": 1}])
def test_fetch_result_rejects_non_sequence_bars(bad_bars):
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, bars=bad_bars)


def test_fetch_result_rejects_bars_containing_non_bar_elements():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, bars=(make_bar(), "not a bar"))


def test_fetch_result_normalizes_list_bars_to_tuple():
    # Phase 3.3 §16: SUCCESS now also requires a real new_records_stored
    # int (HistoricalDataService always provides one on a genuine
    # success) -- supplied here so this test continues to exercise
    # ONLY what it's named for (list->tuple normalization of bars).
    bar = make_bar()
    result = HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, bars=[bar], new_records_stored=1)
    assert result.bars == (bar,)
    assert isinstance(result.bars, tuple)


@pytest.mark.parametrize(
    "bad_cost", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("-0.01")]
)
def test_fetch_result_rejects_non_finite_or_negative_cost(bad_cost):
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.REJECTED_COST_ESTIMATE_FAILED, estimated_cost_usd=bad_cost)


@pytest.mark.parametrize("bad_cost", [True, False, "1.00", 1, 1.5, object()])
def test_fetch_result_rejects_non_decimal_cost_types(bad_cost):
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, estimated_cost_usd=bad_cost)


@pytest.mark.parametrize("bad_count", [-1, True, False, 1.5, "3", object()])
def test_fetch_result_rejects_malformed_new_records_stored(bad_count):
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, new_records_stored=bad_count)


def test_fetch_result_accepts_zero_and_positive_new_records_stored():
    # Phase 3.2 §5: SUCCESS must represent an actual non-empty
    # fetch/storage outcome (the empty case is SUCCESS_EMPTY), so a
    # real bar is supplied here -- new_records_stored=0 remains a
    # legitimate SUCCESS outcome on its own (every fetched bar was
    # already durably stored from a prior call; nothing NEW was
    # written, but the fetch/validation itself genuinely succeeded).
    bar = make_bar()
    assert (
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, bars=(bar,), new_records_stored=0).new_records_stored
        == 0
    )
    assert (
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, bars=(bar,), new_records_stored=5).new_records_stored
        == 5
    )


# -- Phase 3.2 §5: HistoricalFetchResult status/field cross-consistency -----


def test_fetch_result_rejects_success_empty_with_nonempty_bars():
    bar = make_bar()
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS_EMPTY, bars=(bar,), new_records_stored=5)


def test_fetch_result_rejects_success_empty_with_positive_new_records():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS_EMPTY, bars=(), new_records_stored=5)


def test_fetch_result_success_empty_accepts_none_or_zero_new_records():
    assert HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS_EMPTY).new_records_stored is None
    assert (
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS_EMPTY, new_records_stored=0).new_records_stored
        == 0
    )


def test_fetch_result_rejects_success_with_no_bars():
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, bars=())


def test_fetch_result_rejects_success_with_none_new_records_stored():
    # Phase 3.3 §16: a SUCCESS result deliberately represents a
    # successful fetch that was ALSO durably stored -- unlike
    # new_records_stored=0 (every fetched bar was already stored from
    # a prior call), new_records_stored=None would mean the call never
    # even reported a count, which must never pair with SUCCESS
    # (HistoricalDataService always provides a real int on genuine
    # success; SUCCESS_EMPTY/FAILED/REJECTED_*/NOT_CONFIGURED are the
    # statuses that legitimately leave it unreported).
    bar = make_bar()
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=HistoricalFetchStatus.SUCCESS, bars=(bar,))


@pytest.mark.parametrize(
    "status",
    [
        HistoricalFetchStatus.NOT_CONFIGURED,
        HistoricalFetchStatus.REJECTED_INVALID_REQUEST,
        HistoricalFetchStatus.REJECTED_NOT_TRADABLE,
        HistoricalFetchStatus.REJECTED_NETWORK_DISABLED,
        HistoricalFetchStatus.REJECTED_COST_LIMIT,
        HistoricalFetchStatus.REJECTED_COST_ESTIMATE_FAILED,
    ],
)
def test_fetch_result_rejects_rejection_or_not_configured_status_with_bars(status):
    bar = make_bar()
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=status, bars=(bar,))


@pytest.mark.parametrize(
    "status",
    [
        HistoricalFetchStatus.NOT_CONFIGURED,
        HistoricalFetchStatus.REJECTED_INVALID_REQUEST,
        HistoricalFetchStatus.REJECTED_NOT_TRADABLE,
        HistoricalFetchStatus.REJECTED_NETWORK_DISABLED,
        HistoricalFetchStatus.REJECTED_COST_LIMIT,
        HistoricalFetchStatus.REJECTED_COST_ESTIMATE_FAILED,
    ],
)
def test_fetch_result_rejects_rejection_or_not_configured_status_with_positive_new_records(status):
    with pytest.raises(InvalidHistoricalRequestError):
        HistoricalFetchResult(status=status, new_records_stored=1)


def test_fetch_result_failed_status_is_not_over_constrained():
    # FAILED deliberately permits returning normalized bars alongside
    # it (the existing storage-failure path's diagnostic context) --
    # never over-constrained by the Phase 3.2 §5 cross-field rules.
    bar = make_bar()
    result = HistoricalFetchResult(status=HistoricalFetchStatus.FAILED, bars=(bar,), new_records_stored=None)
    assert result.bars == (bar,)


# -- Phase 3.2 §6: HistoricalBar.conflicts_with validates its argument ------


@pytest.mark.parametrize("bad_other", [None, "not a bar", 123, {"not": "a bar"}, object()])
def test_conflicts_with_rejects_non_historicalbar_argument(bad_other):
    bar = make_bar()
    with pytest.raises(InvalidHistoricalBarError):
        bar.conflicts_with(bad_other)


def test_conflicts_with_still_works_for_real_bars():
    bar1 = make_bar()
    bar2 = make_bar(volume=999)
    assert bar1.conflicts_with(bar2) is True
    assert bar1.conflicts_with(make_bar()) is False


# -- Phase 3.2 §7: provider_instrument_id must be meaningful when present ---


@pytest.mark.parametrize("bad_id", ["", "   ", "\t\n"])
def test_bar_rejects_empty_or_whitespace_provider_instrument_id(bad_id):
    with pytest.raises(InvalidHistoricalBarError):
        make_bar(provider_instrument_id=bad_id)


def test_bar_accepts_none_or_nonempty_provider_instrument_id():
    assert make_bar(provider_instrument_id=None).provider_instrument_id is None
    assert make_bar(provider_instrument_id="123").provider_instrument_id == "123"


# -- Phase 3.2 §29: extreme numeric values must not leak a raw decimal error


def test_bar_construction_translates_decimal_overflow_to_domain_error():
    import decimal

    # Force a tiny Emax in the active decimal context so that even a
    # modest, otherwise-ordinary tick*tick_size multiplication
    # genuinely overflows -- proving the translation path works,
    # without inventing an arbitrary "market price cap" in the
    # production code itself.
    with decimal.localcontext() as ctx:
        ctx.Emax = 2
        ctx.Emin = -2
        with pytest.raises(InvalidHistoricalBarError):
            make_bar(open_ticks=10**6, high_ticks=10**6, low_ticks=10**6, close_ticks=10**6)
