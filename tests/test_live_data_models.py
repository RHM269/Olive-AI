"""Phase 4 tests for app.data.live_models: LiveSubscriptionRequest,
LiveTrade, LiveQuote, LiveBar, LiveStreamStatus, and the shared
validators each is built from.

Mirrors the adversarial-boundary discipline CLAUDE.md requires for
every domain object: malformed types, None, booleans where
ints/Decimals are expected, empty/whitespace strings, non-UTC/naive
datetimes, duplicate/empty collections, cross-field invariants
(OHLC, accepted+rejected<=received), and the type-level "no fake
LIVE data" guarantee (data_label is always DataLabel.LIVE).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from app.data.models import DataLabel, HistoricalTimeframe
from app.data.live_models import (
    ConnectionState,
    InvalidLiveEventError,
    InvalidLiveSubscriptionError,
    LiveBar,
    LiveEvent,
    LiveEventType,
    LiveQuote,
    LiveStreamStatus,
    LiveSubscriptionRequest,
    LiveTrade,
    _require_live_subscription_request,
)
from app.futures.models import ContractMonth, FuturesContract

NQ = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)
MNQ = FuturesContract(root_symbol="MNQ", year=2026, month=ContractMonth.DECEMBER)
ES = FuturesContract(root_symbol="ES", year=2026, month=ContractMonth.DECEMBER)
UTC_TS = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)


# -- LiveEventType -------------------------------------------------------


def test_databento_schema_mapping_is_exact():
    assert LiveEventType.TRADE.databento_schema == "trades"
    assert LiveEventType.QUOTE.databento_schema == "mbp-1"
    assert LiveEventType.BAR.databento_schema == "ohlcv-1s"


# -- LiveSubscriptionRequest ----------------------------------------------


def test_subscription_accepts_nq_and_mnq():
    sub = LiveSubscriptionRequest(contracts=(NQ, MNQ), event_types=(LiveEventType.TRADE,))
    assert sub.contract_identities == frozenset({NQ.identity, MNQ.identity})


@pytest.mark.parametrize("bad_contracts", [None, "NQZ6", 123, True, {}, NQ])
def test_subscription_rejects_non_sequence_contracts(bad_contracts):
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=bad_contracts, event_types=(LiveEventType.TRADE,))


def test_subscription_rejects_empty_contracts():
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=(), event_types=(LiveEventType.TRADE,))


def test_subscription_rejects_non_nq_mnq_root():
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=(ES,), event_types=(LiveEventType.TRADE,))


def test_subscription_rejects_malformed_contract_element():
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=(NQ, "not-a-contract"), event_types=(LiveEventType.TRADE,))


def test_subscription_rejects_duplicate_contract():
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=(NQ, NQ), event_types=(LiveEventType.TRADE,))


@pytest.mark.parametrize("bad_event_types", [None, "TRADE", 123, True, {}])
def test_subscription_rejects_non_sequence_event_types(bad_event_types):
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=(NQ,), event_types=bad_event_types)


def test_subscription_rejects_empty_event_types():
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=(NQ,), event_types=())


def test_subscription_rejects_malformed_event_type_element():
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=(NQ,), event_types=("TRADE",))


def test_subscription_rejects_duplicate_event_type():
    with pytest.raises(InvalidLiveSubscriptionError):
        LiveSubscriptionRequest(contracts=(NQ,), event_types=(LiveEventType.TRADE, LiveEventType.TRADE))


def test_subscription_accepts_all_three_event_types_together():
    sub = LiveSubscriptionRequest(
        contracts=(NQ,), event_types=(LiveEventType.TRADE, LiveEventType.QUOTE, LiveEventType.BAR)
    )
    assert len(sub.event_types) == 3


def test_require_live_subscription_request_rejects_wrong_type():
    with pytest.raises(InvalidLiveSubscriptionError):
        _require_live_subscription_request("not a subscription", context="test")


def test_require_live_subscription_request_accepts_valid():
    sub = LiveSubscriptionRequest(contracts=(NQ,), event_types=(LiveEventType.TRADE,))
    assert _require_live_subscription_request(sub, context="test") is sub


def test_subscription_is_frozen():
    sub = LiveSubscriptionRequest(contracts=(NQ,), event_types=(LiveEventType.TRADE,))
    with pytest.raises(Exception):
        sub.contracts = (MNQ,)


# -- LiveTrade -------------------------------------------------------------


def _trade(**overrides):
    base = dict(
        contract=NQ, provider="databento", dataset="GLBX.MDP3", provider_raw_symbol="NQZ6",
        ts_event=UTC_TS, price=Decimal("21000.25"), size=2,
    )
    base.update(overrides)
    return LiveTrade(**base)


def test_trade_happy_path():
    trade = _trade()
    assert trade.data_label is DataLabel.LIVE
    assert trade.price == Decimal("21000.25")


@pytest.mark.parametrize("bad_contract", [None, "NQZ6", 123, True])
def test_trade_rejects_malformed_contract(bad_contract):
    with pytest.raises(InvalidLiveEventError):
        _trade(contract=bad_contract)


@pytest.mark.parametrize("field_name", ["provider", "dataset", "provider_raw_symbol"])
@pytest.mark.parametrize("bad_value", [None, "", "   ", 123, True])
def test_trade_rejects_malformed_string_fields(field_name, bad_value):
    with pytest.raises(InvalidLiveEventError):
        _trade(**{field_name: bad_value})


def test_trade_rejects_naive_ts_event():
    with pytest.raises(InvalidLiveEventError):
        _trade(ts_event=datetime(2026, 10, 7, 14, 30))


def test_trade_rejects_non_utc_ts_event():
    non_utc = UTC_TS.astimezone(timezone(timedelta(hours=5)))
    with pytest.raises(InvalidLiveEventError):
        _trade(ts_event=non_utc)


def test_trade_accepts_optional_ts_recv_and_olive_received_at():
    trade = _trade(ts_recv=UTC_TS, olive_received_at=UTC_TS)
    assert trade.ts_recv == UTC_TS
    assert trade.olive_received_at == UTC_TS


def test_trade_rejects_naive_ts_recv():
    with pytest.raises(InvalidLiveEventError):
        _trade(ts_recv=datetime(2026, 10, 7, 14, 30))


@pytest.mark.parametrize("bad_price", [None, "not-a-number", 0, -1, True, Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-5")])
def test_trade_rejects_malformed_or_nonpositive_price(bad_price):
    with pytest.raises(InvalidLiveEventError):
        _trade(price=bad_price)


def test_trade_accepts_numeric_string_price():
    """_require_finite_decimal deliberately accepts int/str convertible
    to Decimal (mirrors the historical adapter's equivalent helper) --
    this is intentional caller convenience, not a validation gap."""
    trade = _trade(price="21000.25")
    assert trade.price == Decimal("21000.25")


@pytest.mark.parametrize("bad_size", [None, "2", 0, -1, True, 1.5])
def test_trade_rejects_malformed_or_nonpositive_size(bad_size):
    with pytest.raises(InvalidLiveEventError):
        _trade(size=bad_size)


@pytest.mark.parametrize("bad_sequence", [-1, True, "1", 1.5])
def test_trade_rejects_malformed_sequence(bad_sequence):
    with pytest.raises(InvalidLiveEventError):
        _trade(sequence=bad_sequence)


def test_trade_accepts_sequence_zero():
    trade = _trade(sequence=0)
    assert trade.sequence == 0


@pytest.mark.parametrize("bad_id", ["", "   ", 123, True])
def test_trade_rejects_malformed_provider_instrument_id(bad_id):
    with pytest.raises(InvalidLiveEventError):
        _trade(provider_instrument_id=bad_id)


@pytest.mark.parametrize("bad_label", [None, "LIVE", DataLabel.HISTORICAL, DataLabel.DELAYED, DataLabel.SIMULATED, DataLabel.DEMO, True])
def test_trade_rejects_any_non_live_data_label(bad_label):
    """The type-level 'no fake LIVE data' guarantee: every member of
    DataLabel other than LIVE itself must be rejected."""
    with pytest.raises(InvalidLiveEventError):
        _trade(data_label=bad_label)


def test_trade_is_frozen():
    trade = _trade()
    with pytest.raises(Exception):
        trade.price = Decimal("1")


# -- LiveQuote --------------------------------------------------------------


def _quote(**overrides):
    base = dict(
        contract=NQ, provider="databento", dataset="GLBX.MDP3", provider_raw_symbol="NQZ6",
        ts_event=UTC_TS,
    )
    base.update(overrides)
    return LiveQuote(**base)


def test_quote_accepts_both_sides_none():
    quote = _quote()
    assert quote.bid_price is None and quote.ask_price is None


def test_quote_accepts_both_sides_present():
    quote = _quote(bid_price=Decimal("100"), bid_size=1, ask_price=Decimal("101"), ask_size=2)
    assert quote.bid_price == Decimal("100")
    assert quote.ask_price == Decimal("101")


def test_quote_accepts_one_sided_bid_only():
    quote = _quote(bid_price=Decimal("100"), bid_size=1)
    assert quote.bid_price == Decimal("100")
    assert quote.ask_price is None and quote.ask_size is None


def test_quote_accepts_one_sided_ask_only():
    quote = _quote(ask_price=Decimal("101"), ask_size=2)
    assert quote.ask_price == Decimal("101")
    assert quote.bid_price is None and quote.bid_size is None


@pytest.mark.parametrize("side", ["bid", "ask"])
def test_quote_rejects_price_without_size(side):
    with pytest.raises(InvalidLiveEventError):
        _quote(**{f"{side}_price": Decimal("100")})


@pytest.mark.parametrize("side", ["bid", "ask"])
def test_quote_rejects_size_without_price(side):
    with pytest.raises(InvalidLiveEventError):
        _quote(**{f"{side}_size": 1})


@pytest.mark.parametrize("side", ["bid", "ask"])
@pytest.mark.parametrize("bad_price", [0, -1, True, Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_quote_rejects_nonpositive_or_malformed_price(side, bad_price):
    with pytest.raises(InvalidLiveEventError):
        _quote(**{f"{side}_price": bad_price, f"{side}_size": 1})


@pytest.mark.parametrize("side", ["bid", "ask"])
@pytest.mark.parametrize("bad_size", [0, -1, True, 1.5])
def test_quote_rejects_nonpositive_or_malformed_size(side, bad_size):
    with pytest.raises(InvalidLiveEventError):
        _quote(**{f"{side}_price": Decimal("100"), f"{side}_size": bad_size})


def test_quote_rejects_non_live_data_label():
    with pytest.raises(InvalidLiveEventError):
        _quote(data_label=DataLabel.HISTORICAL)


def test_quote_is_frozen():
    quote = _quote()
    with pytest.raises(Exception):
        quote.bid_price = Decimal("1")


# -- LiveBar ----------------------------------------------------------------


def _bar(**overrides):
    base = dict(
        contract=NQ, provider="databento", dataset="GLBX.MDP3", provider_raw_symbol="NQZ6",
        interval=HistoricalTimeframe.ONE_SECOND, ts_event=UTC_TS,
        open=Decimal("21000"), high=Decimal("21001"), low=Decimal("20999"), close=Decimal("21000.5"),
        volume=10,
    )
    base.update(overrides)
    return LiveBar(**base)


def test_bar_happy_path():
    bar = _bar()
    assert bar.high == Decimal("21001")
    assert bar.data_label is DataLabel.LIVE


@pytest.mark.parametrize("bad_interval", [None, "ONE_SECOND", 1, True])
def test_bar_rejects_malformed_interval(bad_interval):
    with pytest.raises(InvalidLiveEventError):
        _bar(interval=bad_interval)


@pytest.mark.parametrize("field_name", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad_value", [None, "not-a-number", 0, -1, True, Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_bar_rejects_malformed_or_nonpositive_ohlc(field_name, bad_value):
    with pytest.raises(InvalidLiveEventError):
        _bar(**{field_name: bad_value})


def test_bar_rejects_high_below_other_prices():
    with pytest.raises(InvalidLiveEventError):
        _bar(open=Decimal("100"), high=Decimal("90"), low=Decimal("80"), close=Decimal("95"))


def test_bar_rejects_low_above_other_prices():
    with pytest.raises(InvalidLiveEventError):
        _bar(open=Decimal("100"), high=Decimal("110"), low=Decimal("105"), close=Decimal("95"))


def test_bar_accepts_all_prices_equal():
    bar = _bar(open=Decimal("100"), high=Decimal("100"), low=Decimal("100"), close=Decimal("100"))
    assert bar.high == bar.low == bar.open == bar.close == Decimal("100")


@pytest.mark.parametrize("bad_volume", [None, "10", -1, True, 1.5])
def test_bar_rejects_malformed_or_negative_volume(bad_volume):
    with pytest.raises(InvalidLiveEventError):
        _bar(volume=bad_volume)


def test_bar_accepts_zero_volume():
    bar = _bar(volume=0)
    assert bar.volume == 0


def test_bar_rejects_non_live_data_label():
    with pytest.raises(InvalidLiveEventError):
        _bar(data_label=DataLabel.SIMULATED)


def test_bar_is_frozen():
    bar = _bar()
    with pytest.raises(Exception):
        bar.volume = 999


# -- LiveEvent convenience tuple ----------------------------------------------


def test_live_event_isinstance_covers_all_three_concrete_types():
    assert isinstance(_trade(), LiveEvent)
    assert isinstance(_quote(), LiveEvent)
    assert isinstance(_bar(), LiveEvent)
    assert not isinstance("not an event", LiveEvent)
    assert not isinstance(NQ, LiveEvent)


# -- LiveStreamStatus ---------------------------------------------------------


def test_status_happy_path_defaults():
    status = LiveStreamStatus(state=ConnectionState.DISCONNECTED)
    assert status.events_received == 0
    assert status.is_stale is False


@pytest.mark.parametrize("bad_state", [None, "CONNECTED", 1, True])
def test_status_rejects_malformed_state(bad_state):
    with pytest.raises(InvalidLiveEventError):
        LiveStreamStatus(state=bad_state)


@pytest.mark.parametrize(
    "field_name", ["events_received", "events_accepted", "events_rejected", "reconnect_count"]
)
@pytest.mark.parametrize("bad_value", [None, "1", -1, True, 1.5])
def test_status_rejects_malformed_counters(field_name, bad_value):
    with pytest.raises(InvalidLiveEventError):
        LiveStreamStatus(state=ConnectionState.CONNECTED, **{field_name: bad_value})


def test_status_rejects_accepted_plus_rejected_exceeding_received():
    with pytest.raises(InvalidLiveEventError):
        LiveStreamStatus(state=ConnectionState.CONNECTED, events_received=1, events_accepted=1, events_rejected=1)


def test_status_accepts_accepted_plus_rejected_equal_to_received():
    status = LiveStreamStatus(state=ConnectionState.CONNECTED, events_received=2, events_accepted=1, events_rejected=1)
    assert status.events_received == 2


def test_status_rejects_naive_last_event_at():
    with pytest.raises(InvalidLiveEventError):
        LiveStreamStatus(state=ConnectionState.CONNECTED, last_event_at=datetime(2026, 10, 7, 14, 30))


def test_status_rejects_naive_last_receive_at():
    with pytest.raises(InvalidLiveEventError):
        LiveStreamStatus(state=ConnectionState.CONNECTED, last_receive_at=datetime(2026, 10, 7, 14, 30))


@pytest.mark.parametrize("bad_is_stale", [None, "true", 1, 0])
def test_status_rejects_non_bool_is_stale(bad_is_stale):
    with pytest.raises(InvalidLiveEventError):
        LiveStreamStatus(state=ConnectionState.CONNECTED, is_stale=bad_is_stale)


def test_status_is_frozen():
    status = LiveStreamStatus(state=ConnectionState.CONNECTED)
    with pytest.raises(Exception):
        status.events_received = 5


# -- ConnectionState enum completeness ---------------------------------------


def test_connection_state_has_every_documented_member():
    expected = {"DISCONNECTED", "CONNECTING", "CONNECTED", "RECONNECTING", "DEGRADED", "FAILED", "STOPPED"}
    assert {member.value for member in ConnectionState} == expected
