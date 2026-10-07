"""Tests for app.data.providers.databento.DatabentoHistoricalProvider.

Every test here injects a fake client via the ``client=`` constructor
parameter -- the real ``databento`` package is never required to be
installed to run or pass this file (see that module's own docstring).
No test performs real network I/O.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from app.futures.models import ContractMonth, FuturesContract
from app.futures.registry import load_instrument_registry
from app.data.models import (
    HistoricalBarRequest,
    HistoricalTimeframe,
    InvalidHistoricalRequestError,
    HistoricalBarContractMismatchError,
    ProviderAuthenticationError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderResponseIdentityError,
    ProviderSymbologyError,
    ProviderDataError,
    CostEstimationFailedError,
    ProviderNotConfiguredError,
)
from app.data.providers.databento import (
    DatabentoHistoricalProvider,
    DEFAULT_DATASET,
    _is_databento_exception,
    _distinct_resolved_instrument_id,
)

UTC = timezone.utc


@pytest.fixture(scope="module")
def registry():
    return load_instrument_registry()


@pytest.fixture
def nq_instrument(registry):
    return registry.get("NQ")


@pytest.fixture
def mnq_instrument(registry):
    return registry.get("MNQ")


NQ_CONTRACT = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)
MNQ_CONTRACT = FuturesContract(root_symbol="MNQ", year=2026, month=ContractMonth.DECEMBER)


def make_request(contract=NQ_CONTRACT, timeframe=HistoricalTimeframe.ONE_MINUTE):
    return HistoricalBarRequest(
        contract=contract, timeframe=timeframe,
        start=datetime(2026, 12, 1, tzinfo=UTC), end=datetime(2026, 12, 1, 1, tzinfo=UTC),
    )


class FakeMetadata:
    def __init__(self, cost=Decimal("12.5"), error=None):
        self.cost = cost
        self.error = error
        self.calls = []

    def get_cost(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.cost


class FakeStore:
    def __init__(self, df):
        self._df = df

    def to_df(self, price_type="float", tz="UTC", **kw):
        return self._df


class FakeTimeseries:
    def __init__(self, df=None, error=None):
        self.df = df
        self.error = error
        self.calls = []

    def get_range(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return FakeStore(self.df)


class FakeSymbology:
    """Fake double for Databento's ``symbology`` namespace
    (``client.symbology.resolve(...)``). By default, every raw symbol in
    ``mapping`` resolves UNAMBIGUOUSLY to the same instrument ID
    regardless of the requested window -- exactly the "everything lines
    up" happy path. ``mapping`` can instead map a raw symbol to a
    *callable* ``(start_date, end_date) -> instrument_id_or_None`` to
    simulate a raw symbol resolving to DIFFERENT instrument IDs over
    different windows (Databento's documented year-reuse problem --
    e.g. NQZ6 resolving to one instrument within December 2026 but a
    different one over some other window), which is exactly what the
    identity-safety gate in ``fetch_bars`` must catch.
    """

    def __init__(self, mapping=None, not_found=None, partial=None, ambiguous=None, error=None):
        self.mapping = mapping if mapping is not None else {"NQZ6": "12345", "MNQZ6": "12345"}
        self.not_found = set(not_found or ())
        self.partial = set(partial or ())
        self.ambiguous = set(ambiguous or ())
        self.error = error
        self.calls = []

    def resolve(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        symbols = kwargs.get("symbols", [])
        start_date = kwargs.get("start_date")
        end_date = kwargs.get("end_date")
        result: dict = {}
        not_found: list = []
        partial: list = []
        for sym in symbols:
            if sym in self.not_found:
                not_found.append(sym)
                continue
            if sym in self.partial:
                partial.append(sym)
                continue
            if sym in self.ambiguous:
                result[sym] = [{"d0": start_date, "d1": end_date, "s": "111"}, {"d0": start_date, "d1": end_date, "s": "222"}]
                continue
            target = self.mapping.get(sym)
            if target is None:
                not_found.append(sym)
                continue
            instrument_id = target(start_date, end_date) if callable(target) else target
            if instrument_id is None:
                not_found.append(sym)
                continue
            result[sym] = [{"d0": start_date, "d1": end_date, "s": instrument_id}]
        return {"result": result, "not_found": not_found, "partial": partial}


class FakeClient:
    def __init__(self, df=None, cost=Decimal("12.5"), cost_error=None, fetch_error=None, symbology=None):
        self.metadata = FakeMetadata(cost=cost, error=cost_error)
        self.timeseries = FakeTimeseries(df=df, error=fetch_error)
        self.symbology = symbology if symbology is not None else FakeSymbology()


def make_df(symbol="NQZ6", minutes=2, start_minute=0, instrument_id=12345):
    idx = pd.DatetimeIndex(
        [datetime(2026, 12, 1, 0, start_minute + m, tzinfo=UTC) for m in range(minutes)], name="ts_event"
    )
    return pd.DataFrame(
        {
            "open": [Decimal("25000.00") + Decimal(m) for m in range(minutes)],
            "high": [Decimal("25001.00") + Decimal(m) for m in range(minutes)],
            "low": [Decimal("24999.75") + Decimal(m) for m in range(minutes)],
            "close": [Decimal("25000.50") + Decimal(m) for m in range(minutes)],
            "volume": [100 + m for m in range(minutes)],
            "symbol": [symbol] * minutes,
            "instrument_id": [instrument_id] * minutes,
        },
        index=idx,
    )


class FakeBentoError(Exception):
    """A fake exception that mimics being raised by the real databento
    package by declaring a matching __module__, exactly as
    _is_databento_exception checks for."""

    __module__ = "databento.common.error"


# -- construction ------------------------------------------------------------


def test_constructor_requires_non_empty_api_key():
    with pytest.raises(ProviderNotConfiguredError):
        DatabentoHistoricalProvider(api_key="", client=FakeClient())


@pytest.mark.parametrize("bad_key", [None, 123, True, "   "])
def test_constructor_rejects_malformed_api_key(bad_key):
    with pytest.raises(ProviderNotConfiguredError):
        DatabentoHistoricalProvider(api_key=bad_key, client=FakeClient())


def test_constructor_requires_non_empty_dataset():
    with pytest.raises(ProviderNotConfiguredError):
        DatabentoHistoricalProvider(api_key="fake-key", dataset="", client=FakeClient())


def test_name_and_dataset_properties():
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=FakeClient())
    assert provider.name == "databento"
    assert provider.dataset == DEFAULT_DATASET


def test_api_key_never_stored_as_attribute():
    provider = DatabentoHistoricalProvider(api_key="db-SECRET-VALUE-12345", client=FakeClient())
    for attr_name in vars(provider):
        attr_value = getattr(provider, attr_name)
        assert "db-SECRET-VALUE-12345" not in repr(attr_value)
    assert "db-SECRET-VALUE-12345" not in repr(provider)


# -- estimate_cost: exact call parameters ------------------------------------


@pytest.mark.parametrize(
    "contract,expected_symbol", [(NQ_CONTRACT, "NQZ6"), (MNQ_CONTRACT, "MNQZ6")]
)
def test_estimate_cost_sends_exact_raw_symbol_stype_in_dataset_schema(contract, expected_symbol):
    client = FakeClient(cost=Decimal("3.00"))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    req = make_request(contract=contract)

    result = provider.estimate_cost(req)

    assert result.estimated_cost_usd == Decimal("3.00")
    call = client.metadata.calls[0]
    assert call["dataset"] == "GLBX.MDP3"
    assert call["symbols"] == [expected_symbol]
    assert call["stype_in"] == "raw_symbol"
    assert call["schema"] == "ohlcv-1m"
    assert call["start"] == req.start
    assert call["end"] == req.end


def test_estimate_cost_never_uses_parent_or_continuous_symbology():
    client = FakeClient()
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    provider.estimate_cost(make_request())
    symbols = client.metadata.calls[0]["symbols"]
    assert symbols == ["NQZ6"]
    assert "NQ.FUT" not in symbols
    assert not any(".c.0" in s or ".v.0" in s or ".n.0" in s for s in symbols)


def test_estimate_cost_rejects_malformed_request():
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=FakeClient())
    with pytest.raises(InvalidHistoricalRequestError):
        provider.estimate_cost("not a request")


def test_estimate_cost_translates_vendor_exception_without_leaking_key():
    error = FakeBentoError("unauthorized: bad key")
    client = FakeClient(cost_error=error)
    provider = DatabentoHistoricalProvider(api_key="db-SUPER-SECRET", client=client)
    with pytest.raises(CostEstimationFailedError) as exc_info:
        provider.estimate_cost(make_request())
    assert "db-SUPER-SECRET" not in str(exc_info.value)


def test_estimate_cost_rejects_non_numeric_cost_response():
    client = FakeClient()
    client.metadata.get_cost = lambda **kwargs: "not-a-number"
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(CostEstimationFailedError):
        provider.estimate_cost(make_request())


def test_estimate_cost_never_calls_get_range():
    client = FakeClient(df=make_df())
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    provider.estimate_cost(make_request())
    assert client.timeseries.calls == []


def test_programming_bug_in_client_is_not_swallowed_as_provider_error():
    """A TypeError from Olive's own code calling the client wrong (not a
    databento-originated exception) must propagate unmodified, never be
    mistaken for a provider failure."""
    client = FakeClient()
    client.metadata.get_cost = lambda **kwargs: (_ for _ in ()).throw(TypeError("bug: wrong arity"))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(TypeError):
        provider.estimate_cost(make_request())


# -- fetch_bars: exact call parameters and normalization --------------------


@pytest.mark.parametrize(
    "contract,instrument_fixture,expected_symbol",
    [(NQ_CONTRACT, "nq_instrument", "NQZ6"), (MNQ_CONTRACT, "mnq_instrument", "MNQZ6")],
)
def test_fetch_bars_sends_exact_call_parameters(request, contract, instrument_fixture, expected_symbol):
    instrument = request.getfixturevalue(instrument_fixture)
    client = FakeClient(df=make_df(symbol=expected_symbol))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    req = make_request(contract=contract)

    bars = provider.fetch_bars(req, instrument)

    assert len(bars) == 2
    call = client.timeseries.calls[0]
    assert call["dataset"] == "GLBX.MDP3"
    assert call["symbols"] == [expected_symbol]
    assert call["stype_in"] == "raw_symbol"
    assert call["schema"] == "ohlcv-1m"
    assert call["start"] == req.start
    assert call["end"] == req.end


def test_fetch_bars_builds_correct_ticks_and_provenance(nq_instrument):
    client = FakeClient(df=make_df())
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    bars = provider.fetch_bars(make_request(), nq_instrument)
    first = bars[0]
    assert first.open == Decimal("25000.00")
    assert first.provider == "databento"
    assert first.dataset == "GLBX.MDP3"
    assert first.provider_raw_symbol == "NQZ6"
    assert first.provider_instrument_id == "12345"
    assert first.data_label.value == "HISTORICAL"


def test_fetch_bars_empty_response_is_empty_tuple_not_error(nq_instrument):
    empty_df = make_df(minutes=0)
    client = FakeClient(df=empty_df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    bars = provider.fetch_bars(make_request(), nq_instrument)
    assert bars == ()


def test_fetch_bars_rejects_wrong_instrument_type():
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=FakeClient(df=make_df()))
    with pytest.raises(InvalidHistoricalRequestError):
        provider.fetch_bars(make_request(), "not an instrument")


def test_fetch_bars_rejects_instrument_root_mismatch(mnq_instrument):
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=FakeClient(df=make_df()))
    with pytest.raises(HistoricalBarContractMismatchError):
        provider.fetch_bars(make_request(contract=NQ_CONTRACT), mnq_instrument)


def test_fetch_bars_rejects_provider_symbol_mismatch_response_identity(nq_instrument):
    """Olive must never trust a provider's own reported symbol without
    checking it: a response claiming a different symbol than requested
    must be rejected, not stored."""
    mismatched_df = make_df(symbol="WRONGSYM")
    client = FakeClient(df=mismatched_df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_never_uses_parent_or_continuous_symbology(nq_instrument):
    client = FakeClient(df=make_df())
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    provider.fetch_bars(make_request(), nq_instrument)
    symbols = client.timeseries.calls[0]["symbols"]
    assert symbols == ["NQZ6"]
    assert "NQ.FUT" not in symbols


def test_fetch_bars_rejects_unexpected_dataframe_index_name(nq_instrument):
    bad_df = make_df()
    bad_df.index.name = "something_else"
    client = FakeClient(df=bad_df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderUnavailableError):
        provider.fetch_bars(make_request(), nq_instrument)


@pytest.mark.parametrize(
    "status_code,expected_error",
    [
        (401, ProviderAuthenticationError),
        (403, ProviderPermissionError),
        (429, ProviderRateLimitError),
        (500, ProviderUnavailableError),
        (503, ProviderUnavailableError),
    ],
)
def test_fetch_bars_translates_vendor_exceptions_by_status_code(nq_instrument, status_code, expected_error):
    error = FakeBentoError(f"vendor error {status_code}")
    error.status_code = status_code
    client = FakeClient(fetch_error=error)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(expected_error):
        provider.fetch_bars(make_request(), nq_instrument)


@pytest.mark.parametrize(
    "message,expected_error",
    [
        ("Unauthorized access", ProviderAuthenticationError),
        ("Forbidden: no subscription for this dataset", ProviderPermissionError),
        ("rate limit exceeded, slow down", ProviderRateLimitError),
        ("request timed out after 30s", ProviderTimeoutError),
        ("something else entirely went wrong", ProviderUnavailableError),
    ],
)
def test_fetch_bars_translates_vendor_exceptions_by_message(nq_instrument, message, expected_error):
    error = FakeBentoError(message)
    client = FakeClient(fetch_error=error)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(expected_error):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_translated_exception_never_leaks_api_key(nq_instrument):
    error = FakeBentoError("unauthorized")
    error.status_code = 401
    client = FakeClient(fetch_error=error)
    provider = DatabentoHistoricalProvider(api_key="db-TOTALLY-SECRET-999", client=client)
    with pytest.raises(ProviderAuthenticationError) as exc_info:
        provider.fetch_bars(make_request(), nq_instrument)
    assert "db-TOTALLY-SECRET-999" not in str(exc_info.value)


def test_fetch_bars_non_databento_exception_propagates_unmodified(nq_instrument):
    client = FakeClient()
    client.timeseries.get_range = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bug, not a vendor failure"))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(RuntimeError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_rejects_malformed_request(nq_instrument):
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=FakeClient(df=make_df()))
    with pytest.raises(InvalidHistoricalRequestError):
        provider.fetch_bars(None, nq_instrument)


# -- _is_databento_exception --------------------------------------------------


def test_fetch_bars_symbology_resolve_failure_prevents_paid_fetch(nq_instrument):
    """Phase 3.1 §15 matrix (1/4): a hard failure in symbology.resolve()
    itself must prevent the paid get_range call entirely."""
    client = FakeClient(df=make_df(), symbology=FakeSymbology(error=FakeBentoError("resolve blew up")))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_symbology_not_found_prevents_paid_fetch(nq_instrument):
    """Phase 3.1 §15 matrix (2/4): no mapping at all for the raw symbol
    must prevent the paid get_range call."""
    client = FakeClient(df=make_df(), symbology=FakeSymbology(not_found=["NQZ6"]))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_symbology_partial_mapping_prevents_paid_fetch(nq_instrument):
    client = FakeClient(df=make_df(), symbology=FakeSymbology(partial=["NQZ6"]))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_symbology_ambiguous_mapping_prevents_paid_fetch(nq_instrument):
    client = FakeClient(df=make_df(), symbology=FakeSymbology(ambiguous=["NQZ6"]))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_decade_reuse_collision_rejected_before_paid_fetch(nq_instrument):
    """Phase 3.1 §13-15 matrix (3/4), the central new finding: a raw
    symbol such as 'NQZ6' is REUSED across years (2016/2026/2036 all
    share the trailing digit '6'). Simulate a request for contract
    NQ-2026-12 whose request window (deliberately, as an attack/stale-
    cache scenario) actually falls in a DIFFERENT year than the
    contract's own month. The two symbology-resolution windows must
    disagree, and the paid get_range call must never happen."""

    def resolves_by_window_year(start_date, end_date):
        return "20260001" if start_date.year == 2026 else "20160001"

    client = FakeClient(
        df=make_df(),
        symbology=FakeSymbology(mapping={"NQZ6": resolves_by_window_year}),
    )
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)

    mismatched_window_request = HistoricalBarRequest(
        contract=NQ_CONTRACT,  # NQ-2026-12
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2016, 12, 1, tzinfo=UTC),
        end=datetime(2016, 12, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(mismatched_window_request, nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_consistent_decade_mapping_allows_paid_fetch(nq_instrument):
    """Phase 3.1 §15 matrix (4/4): when both symbology-resolution
    windows agree (the honest, non-colliding case), the paid fetch may
    proceed as normal -- the cost gate (enforced elsewhere, in
    HistoricalDataService) still applies separately."""

    def resolves_by_window_year(start_date, end_date):
        return "20260001" if start_date.year == 2026 else "20160001"

    client = FakeClient(
        df=make_df(instrument_id="20260001"),
        symbology=FakeSymbology(mapping={"NQZ6": resolves_by_window_year}),
    )
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)

    bars = provider.fetch_bars(make_request(), nq_instrument)  # request window is Dec 2026
    assert len(bars) == 2
    assert client.timeseries.calls  # the paid fetch did happen


def test_fetch_bars_row_instrument_id_mismatch_rejected(nq_instrument):
    """Defense-in-depth: even if symbology resolution agrees, a
    returned ROW whose own instrument_id disagrees with the resolved
    expectation must still be rejected."""
    client = FakeClient(df=make_df(instrument_id="999999"))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_rejects_ts_recv_indexed_dataframe(nq_instrument):
    """Phase 3.1 §11: OHLCV schemas must be indexed by ts_event
    specifically. A response indexed by ts_recv (receive time, not
    event time) must be rejected, never silently relabeled."""
    bad_df = make_df()
    bad_df.index.name = "ts_recv"
    client = FakeClient(df=bad_df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderUnavailableError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_requests_map_symbols_true(nq_instrument):
    """Phase 3.1 §12: to_df must be called with map_symbols=True
    explicitly -- never left to whatever the library default is."""
    captured = {}
    df = make_df()

    class SpyingStore(FakeStore):
        def to_df(self, **kwargs):
            captured.update(kwargs)
            return super().to_df(**kwargs)

    client = FakeClient(df=df)
    client.timeseries.get_range = lambda **kwargs: SpyingStore(df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    provider.fetch_bars(make_request(), nq_instrument)
    assert captured.get("map_symbols") is True


def make_df_with_volume(volume_value, symbol="NQZ6", instrument_id=12345):
    """Build a single-row response DataFrame with an arbitrary,
    possibly-malformed ``volume`` value, as an object-dtype column so
    pandas doesn't coerce it on construction (the way it would if the
    column were forced to int64/float64 dtype)."""
    idx = pd.DatetimeIndex([datetime(2026, 12, 1, 0, 0, tzinfo=UTC)], name="ts_event")
    df = pd.DataFrame(
        {
            "open": [Decimal("25000.00")],
            "high": [Decimal("25001.00")],
            "low": [Decimal("24999.75")],
            "close": [Decimal("25000.50")],
            "volume": pd.array([volume_value], dtype=object),
            "symbol": [symbol],
            "instrument_id": [instrument_id],
        },
        index=idx,
    )
    return df


def test_fetch_bars_rejects_fractional_volume(nq_instrument):
    """Phase 3.1 §8: a fractional provider volume (1.5) must never be
    silently truncated -- it must be rejected as a structured provider
    data error, not stored as 1."""
    bad_df = make_df_with_volume(1.5)
    client = FakeClient(df=bad_df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderDataError) as exc_info:
        provider.fetch_bars(make_request(), nq_instrument)
    assert "1.5" in str(exc_info.value)


def test_fetch_bars_rejects_bool_volume(nq_instrument):
    bad_df = make_df_with_volume(True)
    client = FakeClient(df=bad_df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderDataError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_rejects_tick_misaligned_price_as_structured_provider_error(nq_instrument):
    """Phase 3.1 §9: a provider row-data-quality problem (a price that
    is not an exact multiple of the instrument's tick size) must
    surface as a structured ProviderDataError that
    HistoricalDataService's `except HistoricalProviderError` around
    fetch_bars actually catches -- never a raw escaping
    InvalidHistoricalBarError."""
    bad_df = make_df(minutes=1)
    bad_df.loc[bad_df.index[0], "open"] = Decimal("25000.13")  # not a multiple of tick_size 0.25
    client = FakeClient(df=bad_df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderDataError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_to_df_programming_bug_propagates_unmodified(nq_instrument):
    """Phase 3.1 §10: to_df must only translate genuine Databento
    decode failures. A plain programming bug (Olive calling to_df with
    a signature it doesn't support, surfacing as e.g. TypeError from
    pandas/Olive's own code, NOT a databento-tagged exception) must
    propagate unmodified, never be disguised as a provider outage."""
    client = FakeClient(df=make_df())

    class BrokenStore:
        def to_df(self, **kwargs):
            raise TypeError("to_df() got an unexpected keyword argument")

    client.timeseries.get_range = lambda **kwargs: BrokenStore()
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(TypeError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_missing_symbol_column_fails_closed(nq_instrument):
    """Phase 3.1 §12: if the response has no resolved 'symbol' at all
    (map_symbols=True was requested but the provider still didn't
    supply it), Olive must fail closed rather than silently skip the
    identity check."""
    bad_df = make_df(minutes=1).drop(columns=["symbol"])
    client = FakeClient(df=bad_df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_400_status_invalid_key_message_classified_as_auth_and_key_never_leaks(nq_instrument):
    """Phase 3.1 §4-5, the critical finding: Databento's documented
    invalid-Basic-auth failure shape literally embeds the configured
    API key in its own message text
    ("Invalid username in Basic auth ('<key>')"), and can arrive with
    HTTP status 400 rather than 401. Olive must classify this as an
    authentication failure WITHOUT ever echoing that raw vendor message
    -- the fake key used here must not appear anywhere in the raised
    exception's message, args, or repr."""
    fake_api_key = "db-TOTALLY-REAL-LOOKING-SECRET-0042"
    vendor_message = f"Invalid username in Basic auth ('{fake_api_key}')"
    error = FakeBentoError(vendor_message)
    error.status_code = 400
    client = FakeClient(fetch_error=error)
    provider = DatabentoHistoricalProvider(api_key=fake_api_key, client=client)

    with pytest.raises(ProviderAuthenticationError) as exc_info:
        provider.fetch_bars(make_request(), nq_instrument)

    raised = exc_info.value
    haystacks = [str(raised), repr(raised), str(raised.args), repr(raised.args)]
    for haystack in haystacks:
        assert fake_api_key not in haystack
    # And the exception chain must be severed -- `from None` -- so even
    # printing the full chain can never surface the vendor exception.
    assert raised.__cause__ is None


def test_estimate_cost_400_status_invalid_key_message_never_leaks():
    fake_api_key = "db-ANOTHER-REAL-LOOKING-SECRET-0099"
    vendor_message = f"Invalid username in Basic auth ('{fake_api_key}')"
    error = FakeBentoError(vendor_message)
    error.status_code = 400
    client = FakeClient(cost_error=error)
    provider = DatabentoHistoricalProvider(api_key=fake_api_key, client=client)

    with pytest.raises(CostEstimationFailedError) as exc_info:
        provider.estimate_cost(make_request())

    raised = exc_info.value
    for haystack in [str(raised), repr(raised), str(raised.args)]:
        assert fake_api_key not in haystack
    assert raised.__cause__ is None


def test_estimate_cost_rejects_nan_cost_response():
    """Phase 3.1 §6: a malformed provider cost (NaN) must become
    CostEstimationFailedError, not escape as InvalidHistoricalRequestError
    (which HistoricalDataService's cost-estimation except clause,
    scoped to HistoricalProviderError, would not catch)."""
    client = FakeClient()
    client.metadata.get_cost = lambda **kwargs: Decimal("NaN")
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(CostEstimationFailedError):
        provider.estimate_cost(make_request())


def test_estimate_cost_rejects_negative_cost_response():
    client = FakeClient()
    client.metadata.get_cost = lambda **kwargs: Decimal("-1")
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(CostEstimationFailedError):
        provider.estimate_cost(make_request())


def test_estimate_cost_rejects_infinite_cost_response():
    client = FakeClient()
    client.metadata.get_cost = lambda **kwargs: Decimal("Infinity")
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(CostEstimationFailedError):
        provider.estimate_cost(make_request())


def test_is_databento_exception_true_for_matching_module():
    assert _is_databento_exception(FakeBentoError("x"))


def test_is_databento_exception_false_for_builtin_exception():
    assert not _is_databento_exception(ValueError("x"))
    assert not _is_databento_exception(TypeError("x"))
    assert not _is_databento_exception(RuntimeError("x"))


# -- Phase 3.2 §15: the injected client is itself a public boundary --------


def test_constructor_rejects_malformed_injected_client_string():
    with pytest.raises(ProviderNotConfiguredError):
        DatabentoHistoricalProvider(api_key="fake-key", client="bad")


@pytest.mark.parametrize(
    "missing_attr",
    ["metadata", "timeseries", "symbology"],
)
def test_constructor_rejects_client_missing_a_required_namespace(missing_attr):
    client = FakeClient()
    setattr(client, missing_attr, None)
    with pytest.raises(ProviderNotConfiguredError):
        DatabentoHistoricalProvider(api_key="fake-key", client=client)


def test_constructor_rejects_client_with_non_callable_method():
    client = FakeClient()
    client.metadata.get_cost = "not callable"
    with pytest.raises(ProviderNotConfiguredError):
        DatabentoHistoricalProvider(api_key="fake-key", client=client)


def test_constructor_accepts_well_formed_fake_client():
    client = FakeClient()
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    assert provider.name == "databento"


# -- Phase 3.2 §8: a response row MISSING instrument_id must fail closed,
# never have its provenance silently fabricated from the symbology-resolved
# expected value ------------------------------------------------------------


def test_fetch_bars_rejects_row_with_missing_instrument_id_column(nq_instrument):
    df = make_df(minutes=1).drop(columns=["instrument_id"])
    client = FakeClient(df=df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_rejects_row_with_none_instrument_id(nq_instrument):
    df = make_df(minutes=1)
    df["instrument_id"] = [None]
    client = FakeClient(df=df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_rejects_row_with_nan_instrument_id(nq_instrument):
    df = make_df(minutes=1)
    df["instrument_id"] = pd.array([float("nan")], dtype=object)
    client = FakeClient(df=df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_rejects_row_with_empty_instrument_id(nq_instrument):
    df = make_df(minutes=1)
    df["instrument_id"] = [""]
    client = FakeClient(df=df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_never_fabricates_provider_instrument_id_fallback(nq_instrument):
    """Regression for the specific independent-review reproduction: a
    missing instrument_id column must raise, not silently produce a
    stored bar whose provider_instrument_id is the symbology-resolved
    expected value Databento never actually returned."""
    df = make_df(minutes=1).drop(columns=["instrument_id"])
    client = FakeClient(df=df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)
    # (no bars were ever constructed -- fetch_bars raised before any
    # HistoricalBar.from_decimal_prices call could fabricate provenance)


@pytest.mark.parametrize(
    "missing_field, bad_value",
    [("symbol", None)],
)
def test_fetch_bars_rejects_row_with_missing_symbol(nq_instrument, missing_field, bad_value):
    df = make_df(minutes=1)
    df[missing_field] = [bad_value]
    client = FakeClient(df=df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_rejects_row_with_wrong_symbol(nq_instrument):
    df = make_df(minutes=1, symbol="WRONGSYM")
    client = FakeClient(df=df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderResponseIdentityError):
        provider.fetch_bars(make_request(), nq_instrument)


def test_fetch_bars_rejects_row_with_wrong_instrument_id(nq_instrument):
    df = make_df(minutes=1, instrument_id=99999)  # resolved expected is 12345
    client = FakeClient(df=df)
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)


# -- Phase 3.2 §9/§24: symbology date-range coverage must fully cover every
# instant in the half-open [request.start, request.end) datetime interval,
# matching Databento's EXCLUSIVE end_date contract ---------------------------


def test_symbology_actual_range_covers_intraday_cross_midnight_end(nq_instrument):
    client = FakeClient(df=make_df(minutes=0))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    request = HistoricalBarRequest(
        contract=NQ_CONTRACT,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 30, 23, 0, tzinfo=UTC),
        end=datetime(2026, 10, 1, 0, 30, tzinfo=UTC),
    )
    provider.fetch_bars(request, nq_instrument)
    assert len(client.symbology.calls) == 2
    actual_range_call = client.symbology.calls[1]
    assert actual_range_call["start_date"] == date(2026, 9, 30)
    # end_date is EXCLUSIVE -- October 1st 00:00-00:30 is requested, so
    # the date interval must extend through October 2nd (exclusive) to
    # cover it; October 1st alone (exclusive) would cover NOTHING of it.
    assert actual_range_call["end_date"] == date(2026, 10, 2)


def test_symbology_actual_range_end_exactly_midnight_does_not_overextend(nq_instrument):
    client = FakeClient(df=make_df(minutes=0))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    request = HistoricalBarRequest(
        contract=NQ_CONTRACT,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 30, 23, 0, tzinfo=UTC),
        end=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
    )
    provider.fetch_bars(request, nq_instrument)
    actual_range_call = client.symbology.calls[1]
    assert actual_range_call["start_date"] == date(2026, 9, 30)
    assert actual_range_call["end_date"] == date(2026, 10, 1)


def test_symbology_actual_range_end_one_microsecond_past_midnight(nq_instrument):
    client = FakeClient(df=make_df(minutes=0))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    request = HistoricalBarRequest(
        contract=NQ_CONTRACT,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 30, 23, 0, tzinfo=UTC),
        end=datetime(2026, 10, 1, 0, 0, 0, 1, tzinfo=UTC),
    )
    provider.fetch_bars(request, nq_instrument)
    actual_range_call = client.symbology.calls[1]
    # Even one microsecond past midnight on Oct 1 means Oct 1 contains
    # a requested instant, so the exclusive end_date must extend to
    # Oct 2 to cover it.
    assert actual_range_call["end_date"] == date(2026, 10, 2)


def test_symbology_actual_range_same_day_intraday_request(nq_instrument):
    client = FakeClient(df=make_df(minutes=0))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    request = HistoricalBarRequest(
        contract=NQ_CONTRACT,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 30, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 30, 23, 0, tzinfo=UTC),
    )
    provider.fetch_bars(request, nq_instrument)
    actual_range_call = client.symbology.calls[1]
    assert actual_range_call["start_date"] == date(2026, 9, 30)
    assert actual_range_call["end_date"] == date(2026, 10, 1)


def test_symbology_actual_range_cross_month_intraday_end(nq_instrument):
    client = FakeClient(df=make_df(minutes=0))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    request = HistoricalBarRequest(
        contract=NQ_CONTRACT,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 1, 31, 23, 30, tzinfo=UTC),
        end=datetime(2026, 2, 1, 0, 10, tzinfo=UTC),
    )
    provider.fetch_bars(request, nq_instrument)
    actual_range_call = client.symbology.calls[1]
    assert actual_range_call["start_date"] == date(2026, 1, 31)
    assert actual_range_call["end_date"] == date(2026, 2, 2)


def test_symbology_actual_range_cross_year_intraday_end(nq_instrument):
    client = FakeClient(df=make_df(minutes=0))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    request = HistoricalBarRequest(
        contract=NQ_CONTRACT,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2025, 12, 31, 23, 30, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
    )
    provider.fetch_bars(request, nq_instrument)
    actual_range_call = client.symbology.calls[1]
    assert actual_range_call["start_date"] == date(2025, 12, 31)
    assert actual_range_call["end_date"] == date(2026, 1, 2)


# -- Phase 3.3 §11/§12: strict symbology instrument-ID validation BEFORE
# any paid timeseries.get_range call ----------------------------------------


class RawResolutionSymbology:
    """A symbology double that returns an arbitrary, possibly-malformed
    raw resolution dict verbatim -- unlike FakeSymbology, which always
    builds a well-formed ``{"s": ...}`` entry itself, this lets tests
    inject exactly the adversarial resolution shapes Phase 3.3 §11/§12
    must reject before any paid fetch."""

    def __init__(self, raw_resolution):
        self._raw_resolution = raw_resolution
        self.calls = []

    def resolve(self, **kwargs):
        self.calls.append(kwargs)
        return self._raw_resolution


@pytest.mark.parametrize(
    "bad_instrument_id",
    [None, "", "   ", 0, True, False, 1.5, "abc", "-5", "5.0", object()],
)
def test_fetch_bars_rejects_malformed_resolved_instrument_id_before_paid_fetch(nq_instrument, bad_instrument_id):
    """Phase 3.3 §11 reproduction: a resolution whose 's' value is not
    a well-formed positive decimal-digit string must be rejected
    BEFORE the paid timeseries.get_range call -- never silently
    accepted via a blind str(instrument_id) conversion."""
    resolution = {"result": {"NQZ6": [{"s": bad_instrument_id}]}, "partial": [], "not_found": []}
    client = FakeClient(df=make_df(), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


@pytest.mark.parametrize(
    "bad_entry",
    [None, {}, [], "a-bare-string", 123, 1.5, True],
)
def test_fetch_bars_rejects_malformed_symbology_entry_shape_before_paid_fetch(nq_instrument, bad_entry):
    """Phase 3.3 §12: an entry in the symbology mapping list that is
    not a usable dict-like (or attribute-bearing) shape at all must
    raise an Olive-owned ProviderSymbologyError -- never a raw
    AttributeError/TypeError -- and must never reach the paid fetch."""
    resolution = {"result": {"NQZ6": [bad_entry]}, "partial": [], "not_found": []}
    client = FakeClient(df=make_df(), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_rejects_symbology_entry_with_wrong_type_s(nq_instrument):
    """Phase 3.3 §12: a mapping entry whose 's' field is present but
    of the wrong TYPE (an int, not a str) must be rejected -- Databento's
    documented resolution response represents 's' as a string even
    though the underlying instrument ID is an unsigned integer."""
    resolution = {"result": {"NQZ6": [{"s": 12345}]}, "partial": [], "not_found": []}
    client = FakeClient(df=make_df(), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_accepts_well_formed_digit_string_instrument_id(nq_instrument):
    """Sanity check: a genuinely well-formed resolution (a positive
    decimal-digit string, matching every row's own instrument_id) is
    still accepted and the paid fetch proceeds normally."""
    resolution = {"result": {"NQZ6": [{"s": "12345"}]}, "partial": [], "not_found": []}
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    bars = provider.fetch_bars(make_request(), nq_instrument)
    assert len(bars) == 2
    assert client.timeseries.calls  # the paid fetch DID happen


# -- Phase 3.3 QA compliance rework §7/§8/§12/§13: malformed NESTED
# symbology containers must raise ProviderSymbologyError, never a raw
# TypeError/AttributeError, and must never reach the paid
# timeseries.get_range call. These are the full response-tree checks --
# testing only leaf ("s") values (above) does not prove the containers
# ONE LEVEL UP (result, result[raw_symbol] itself, not_found, partial)
# are safe. ---------------------------------------------------------------


def test_fetch_bars_rejects_malformed_entries_container_int_before_paid_fetch(nq_instrument):
    """Confirmed defect reproduction: {"result": {"NQZ6": 123}, ...}
    previously reached `for entry in entries:` with entries=123 and
    leaked `TypeError: 'int' object is not iterable`."""
    resolution = {"result": {"NQZ6": 123}, "not_found": [], "partial": []}
    client = FakeClient(df=make_df(), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


@pytest.mark.parametrize(
    "malformed_entries",
    [
        {"s": "12345"},  # a single entry dict used AS the container -- wrong shape
        "12345",
        b"12345",
        12345.0,
        True,
        {1, 2, 3},
    ],
)
def test_fetch_bars_rejects_malformed_entries_container_other_types_before_paid_fetch(
    nq_instrument, malformed_entries
):
    """Broader parametrized variant of the int-container defect: ANY
    non-list/tuple value for result[raw_symbol] -- including a bare
    dict (one malformed entry masquerading as the whole container), a
    str/bytes (iterable, but yields characters/bytes, not mapping
    entries), a float, a bool, and a set -- must be rejected before the
    paid fetch, never iterated over directly."""
    resolution = {"result": {"NQZ6": malformed_entries}, "not_found": [], "partial": []}
    client = FakeClient(df=make_df(), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


@pytest.mark.parametrize("malformed_not_found", [123, "NQZ6", {"a": 1}, True, 1.5])
def test_fetch_bars_rejects_malformed_not_found_container_before_paid_fetch(nq_instrument, malformed_not_found):
    """Confirmed defect reproduction (and parametrized variants):
    {"result": {"NQZ6": [...]}, "not_found": 123, "partial": []}
    previously evaluated `mapping.get("not_found") or []` to `123`
    (not `[]`, since `or` only replaces FALSY values) and then leaked a
    raw TypeError from `"NQZ6" in 123`. A bare string is included too:
    even though `"NQZ6" in "NQZ6"` would happen to be True, `in` against
    a str is a SUBSTRING check, not a symbol-membership check, and is
    exactly as malformed as the int case."""
    resolution = {"result": {"NQZ6": [{"s": "12345"}]}, "not_found": malformed_not_found, "partial": []}
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


@pytest.mark.parametrize("malformed_partial", [123, "NQZ6", {"a": 1}, True, 1.5])
def test_fetch_bars_rejects_malformed_partial_container_before_paid_fetch(nq_instrument, malformed_partial):
    """Confirmed defect reproduction (and parametrized variants): the
    same `or []`-does-not-coerce-truthy-int bug via `partial=123`."""
    resolution = {"result": {"NQZ6": [{"s": "12345"}]}, "not_found": [], "partial": malformed_partial}
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


@pytest.mark.parametrize("malformed_result", [123, "NQZ6", [1, 2, 3], True, 1.5])
def test_fetch_bars_rejects_malformed_result_mapping_before_paid_fetch(nq_instrument, malformed_result):
    """§8: the top-level 'result' field itself must be validated as a
    mapping before indexing into it with `.get(raw_symbol)` -- not just
    the entries one level below it."""
    resolution = {"result": malformed_result, "not_found": [], "partial": []}
    client = FakeClient(df=make_df(), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


class _AttributeStyleResolution:
    """A minimal attribute-bearing (non-dict) resolution response
    object, exercising `_resolution_mapping`'s attribute-fallback
    branch directly -- distinct from every other test in this file,
    which injects a plain dict."""

    def __init__(self, *, result=None, not_found=None, partial=None):
        self.result = result
        self.not_found = not_found
        self.partial = partial


@pytest.mark.parametrize(
    "resolution_obj",
    [
        _AttributeStyleResolution(),  # every attribute absent -> None
        _AttributeStyleResolution(result=123, not_found=123, partial=123),
        _AttributeStyleResolution(result={"NQZ6": 123}),
    ],
)
def test_distinct_resolved_instrument_id_rejects_malformed_attribute_style_resolution(resolution_obj):
    """§13: the generic nested-boundary adversarial style applies to
    the attribute-style (non-dict) resolution object too, not just the
    dict shape every other test here uses -- mutating the WHOLE object
    (missing attributes entirely) as well as its individual attribute
    values must both raise ProviderSymbologyError, never
    AttributeError/TypeError, and never silently resolve via the
    now-removed `or []`/`or {}` truthiness coercion."""
    with pytest.raises(ProviderSymbologyError):
        _distinct_resolved_instrument_id(resolution_obj, "NQZ6")


@pytest.mark.parametrize(
    "malformed_member",
    [123, True, False, None, {}, [], "", "   "],
)
def test_fetch_bars_rejects_not_found_with_malformed_member_before_paid_fetch(nq_instrument, malformed_member):
    """Phase 3 final completion pass (item C): the not_found container
    was previously only checked for its own TYPE (list/tuple) -- a
    malformed MEMBER such as not_found=[123] passed the container check
    and then simply never matched `raw_symbol in not_found`, silently
    treated as if raw_symbol had legitimately not been not-found. Every
    element must now be a non-empty symbol string."""
    resolution = {
        "result": {"NQZ6": [{"s": "12345"}]},
        "not_found": [malformed_member],
        "partial": [],
    }
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


@pytest.mark.parametrize(
    "malformed_member",
    [123, True, False, None, {}, [], "", "   "],
)
def test_fetch_bars_rejects_partial_with_malformed_member_before_paid_fetch(nq_instrument, malformed_member):
    """Same gap, mirrored for `partial`."""
    resolution = {
        "result": {"NQZ6": [{"s": "12345"}]},
        "not_found": [],
        "partial": [malformed_member],
    }
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_accepts_well_formed_not_found_and_partial_members(nq_instrument):
    # Sanity check: a genuinely well-formed not_found/partial (symbols
    # that are simply not raw_symbol) is not rejected by the new
    # member-level check.
    resolution = {
        "result": {"NQZ6": [{"s": "12345"}]},
        "not_found": ["MESZ6"],
        "partial": ["ESZ6"],
    }
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    bars = provider.fetch_bars(make_request(), nq_instrument)
    assert len(bars) == 2


# -- Phase 3 final completion pass (item 3): the ENTIRE `result` mapping
# must be structurally trustworthy -- every key, every sibling entries
# container, and every sibling entry's 's' value -- not only
# result[raw_symbol] in isolation. --------------------------------------


@pytest.mark.parametrize("malformed_key", [123, True, None])
def test_fetch_bars_rejects_result_with_malformed_key_before_paid_fetch(nq_instrument, malformed_key):
    resolution = {
        "result": {"NQZ6": [{"s": "12345"}], malformed_key: [{"s": "999"}]},
        "not_found": [],
        "partial": [],
    }
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_rejects_result_with_malformed_sibling_entries_container(nq_instrument):
    # The requested symbol's own entries are perfectly well-formed, but
    # a SIBLING key's entries container is malformed -- the whole
    # response must still be rejected as untrustworthy.
    resolution = {
        "result": {"NQZ6": [{"s": "12345"}], "MESZ6": 999},
        "not_found": [],
        "partial": [],
    }
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_rejects_result_with_malformed_sibling_entry_s_value(nq_instrument):
    resolution = {
        "result": {"NQZ6": [{"s": "12345"}], "MESZ6": [{"s": 999}]},
        "not_found": [],
        "partial": [],
    }
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    with pytest.raises(ProviderSymbologyError):
        provider.fetch_bars(make_request(), nq_instrument)
    assert client.timeseries.calls == []


def test_fetch_bars_accepts_well_formed_sibling_result_entries(nq_instrument):
    # Sanity check: a genuinely well-formed sibling entry (a different
    # symbol Olive did not ask about, but which the vendor response
    # structurally describes correctly anyway) is not rejected.
    resolution = {
        "result": {"NQZ6": [{"s": "12345"}], "MESZ6": [{"s": "999"}]},
        "not_found": [],
        "partial": [],
    }
    client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
    provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
    bars = provider.fetch_bars(make_request(), nq_instrument)
    assert len(bars) == 2


def test_distinct_resolved_instrument_id_no_malformed_case_reaches_get_range(nq_instrument):
    """§12: an explicit, single assertion that covers every malformed
    resolution shape in this section at once -- none of them ever
    reaches the paid timeseries.get_range call, which is the entire
    point of resolving symbology via the FREE endpoint first."""
    malformed_resolutions = [
        {"result": {"NQZ6": 123}, "not_found": [], "partial": []},
        {"result": {"NQZ6": [{"s": "12345"}]}, "not_found": 123, "partial": []},
        {"result": {"NQZ6": [{"s": "12345"}]}, "not_found": [], "partial": 123},
        {"result": 123, "not_found": [], "partial": []},
    ]
    for resolution in malformed_resolutions:
        client = FakeClient(df=make_df(instrument_id=12345), symbology=RawResolutionSymbology(resolution))
        provider = DatabentoHistoricalProvider(api_key="fake-key", client=client)
        with pytest.raises(ProviderSymbologyError):
            provider.fetch_bars(make_request(), nq_instrument)
        assert client.timeseries.calls == []
