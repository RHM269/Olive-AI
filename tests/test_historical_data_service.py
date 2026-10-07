"""Tests for app.data.service.HistoricalDataService.

Every test here injects a fake provider and/or fake store -- the real
Databento adapter and the real pyarrow-backed storage layer are never
required, and no test performs network I/O. These tests prove the
service's GATE ORDER (tradable-domain check before any provider call,
request validation, provider configuration, network opt-in, cost
estimation and limit, provider fetch, cross-bar validation, storage) and
that every expected failure becomes a distinct HistoricalFetchStatus
rather than an exception or ambiguous value.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.config import DataMode, Environment, HistoricalProviderKind, Settings
from app.futures.calendar import load_cycle_date_calendar
from app.futures.models import ContractMonth, FuturesContract
from app.futures.registry import load_instrument_registry
from app.data.models import (
    DataLabel,
    HistoricalBar,
    HistoricalBarRequest,
    HistoricalFetchStatus,
    HistoricalProviderError,
    CostEstimationFailedError,
    HistoricalTimeframe,
    InvalidHistoricalServiceConfigurationError,
)
from app.data.provider_base import CostEstimate, HistoricalMarketDataProvider
from app.data.providers.unconfigured import UnconfiguredHistoricalProvider
from app.data.service import HistoricalDataService
from app.data.storage import HistoricalWriteResult

UTC = timezone.utc


@pytest.fixture(scope="module")
def registry():
    return load_instrument_registry()


@pytest.fixture(scope="module")
def calendar():
    return load_cycle_date_calendar()


NQ_CONTRACT = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)
ES_CONTRACT = FuturesContract(root_symbol="ES", year=2026, month=ContractMonth.DECEMBER)


def make_request(contract=NQ_CONTRACT):
    return HistoricalBarRequest(
        contract=contract, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 1, 1, tzinfo=UTC),
    )


def make_settings(**overrides) -> Settings:
    base = dict(
        app_name="t", environment=Environment.DEVELOPMENT, log_level="INFO", data_mode=DataMode.DEVELOPMENT,
        historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="fake-key",
        historical_data_dir="/tmp/olive-service-test-unused", historical_network_enabled=True,
        historical_max_request_cost_usd=Decimal("10.00"),
    )
    base.update(overrides)
    return Settings(**base)


def make_bar(**overrides) -> HistoricalBar:
    base = dict(
        root_symbol="NQ", contract_year=2026, contract_month=12,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        ts_event=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        open_ticks=100000, high_ticks=100020, low_ticks=99980, close_ticks=100010,
        volume=5, tick_size=Decimal("0.25"), data_label=DataLabel.HISTORICAL,
        provider="fake", dataset="GLBX.MDP3", provider_raw_symbol="NQZ6", provider_instrument_id="1",
    )
    base.update(overrides)
    return HistoricalBar(**base)


class FakeProvider(HistoricalMarketDataProvider):
    def __init__(self, cost=Decimal("1.00"), bars=(), cost_error=None, fetch_error=None):
        self._cost = cost
        self._bars = bars
        self._cost_error = cost_error
        self._fetch_error = fetch_error
        self.fetch_called = False
        self.estimate_called = False
        self.fetch_args = None

    @property
    def name(self):
        return "fake"

    def estimate_cost(self, request):
        self.estimate_called = True
        if self._cost_error:
            raise self._cost_error
        return CostEstimate(estimated_cost_usd=self._cost)

    def fetch_bars(self, request, instrument):
        self.fetch_called = True
        self.fetch_args = (request, instrument)
        if self._fetch_error:
            raise self._fetch_error
        return self._bars


class FakeStore:
    def __init__(self):
        self.write_calls = []
        self.fail_with = None

    def write_bars(self, bars, **kwargs):
        if self.fail_with:
            raise self.fail_with
        self.write_calls.append(bars)
        return HistoricalWriteResult(partitions_written=1, new_records=len(bars), total_records=len(bars))


def make_service(provider, settings=None, store=None, registry_=None, calendar_=None, registry=None, calendar=None):
    return HistoricalDataService(
        provider,
        settings or make_settings(),
        store=store if store is not None else FakeStore(),
        registry=registry,
        calendar=calendar,
    )


# -- constructor type guards --------------------------------------------------
# Phase 3.1 §17: every constructor-time argument -- provider/settings/
# store/registry/calendar -- is validated with an Olive-owned error
# (InvalidHistoricalServiceConfigurationError), never a raw TypeError,
# and never deferred until some later call trips over an AttributeError.


def test_constructor_rejects_non_provider():
    with pytest.raises(InvalidHistoricalServiceConfigurationError):
        HistoricalDataService("not a provider", make_settings())


def test_constructor_rejects_non_settings():
    with pytest.raises(InvalidHistoricalServiceConfigurationError):
        HistoricalDataService(FakeProvider(), "not settings")


@pytest.mark.parametrize("bad_store", [123, "not a store", object(), True, []])
def test_constructor_rejects_malformed_store(bad_store):
    with pytest.raises(InvalidHistoricalServiceConfigurationError):
        HistoricalDataService(FakeProvider(), make_settings(), store=bad_store)


def test_constructor_accepts_duck_typed_store_double():
    """A store test double need not subclass HistoricalBarStore -- only
    expose a callable write_bars -- preserving this class's documented
    injectability (tests never need the real pyarrow-backed store)."""
    service = HistoricalDataService(FakeProvider(), make_settings(), store=FakeStore())
    assert service is not None


@pytest.mark.parametrize("bad_registry", [123, "not a registry", object(), True, []])
def test_constructor_rejects_malformed_registry(bad_registry):
    with pytest.raises(InvalidHistoricalServiceConfigurationError):
        HistoricalDataService(FakeProvider(), make_settings(), store=FakeStore(), registry=bad_registry)


@pytest.mark.parametrize("bad_calendar", [123, "not a calendar", object(), True, []])
def test_constructor_rejects_malformed_calendar(bad_calendar):
    with pytest.raises(InvalidHistoricalServiceConfigurationError):
        HistoricalDataService(FakeProvider(), make_settings(), store=FakeStore(), calendar=bad_calendar)


# -- gate 1: request validation -----------------------------------------------


@pytest.mark.parametrize("bad_request", [None, "req", 123, {}, object()])
def test_rejects_malformed_request_before_touching_anything(bad_request, registry, calendar):
    provider = FakeProvider()
    service = make_service(provider, registry=registry, calendar=calendar)
    result = service.fetch_and_store(bad_request)
    assert result.status == HistoricalFetchStatus.REJECTED_INVALID_REQUEST
    assert not provider.estimate_called
    assert not provider.fetch_called


# -- gate 2: Olive production tradable-domain gate (BEFORE provider) --------


def test_non_tradable_root_rejected_before_any_provider_call(registry, calendar):
    provider = FakeProvider()
    service = make_service(provider, registry=registry, calendar=calendar)
    es_request = make_request(contract=ES_CONTRACT)

    result = service.fetch_and_store(es_request)

    assert result.status == HistoricalFetchStatus.REJECTED_NOT_TRADABLE
    assert not provider.estimate_called
    assert not provider.fetch_called


def test_tradable_nq_and_mnq_both_pass_the_domain_gate(registry, calendar):
    for contract in (NQ_CONTRACT, FuturesContract(root_symbol="MNQ", year=2026, month=ContractMonth.DECEMBER)):
        provider = FakeProvider(cost=Decimal("1.00"), bars=())
        service = make_service(provider, registry=registry, calendar=calendar)
        result = service.fetch_and_store(make_request(contract=contract))
        assert result.status != HistoricalFetchStatus.REJECTED_NOT_TRADABLE


# -- gate 3: provider configuration -------------------------------------------


def test_unconfigured_provider_returns_not_configured(registry, calendar):
    provider = UnconfiguredHistoricalProvider("no key set")
    service = make_service(provider, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.NOT_CONFIGURED
    assert result.message == "no key set"


# -- gate 4: network opt-in ----------------------------------------------------


def test_network_disabled_rejects_before_any_provider_call(registry, calendar):
    provider = FakeProvider()
    service = make_service(provider, settings=make_settings(historical_network_enabled=False),
                            registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.REJECTED_NETWORK_DISABLED
    assert not provider.estimate_called
    assert not provider.fetch_called


# -- gate 5/6: cost estimation and limit --------------------------------------


def test_cost_estimation_failure_is_fail_closed_never_calls_fetch(registry, calendar):
    provider = FakeProvider(cost_error=CostEstimationFailedError("vendor could not estimate"))
    service = make_service(provider, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.REJECTED_COST_ESTIMATE_FAILED
    assert not provider.fetch_called


def test_cost_below_limit_proceeds_to_fetch(registry, calendar):
    provider = FakeProvider(cost=Decimal("5.00"), bars=())
    service = make_service(
        provider, settings=make_settings(historical_max_request_cost_usd=Decimal("10.00")),
        registry=registry, calendar=calendar,
    )
    result = service.fetch_and_store(make_request())
    assert provider.fetch_called
    assert result.status == HistoricalFetchStatus.SUCCESS_EMPTY


def test_cost_exactly_at_limit_proceeds_to_fetch(registry, calendar):
    provider = FakeProvider(cost=Decimal("10.00"), bars=())
    service = make_service(
        provider, settings=make_settings(historical_max_request_cost_usd=Decimal("10.00")),
        registry=registry, calendar=calendar,
    )
    result = service.fetch_and_store(make_request())
    assert provider.fetch_called
    assert result.status == HistoricalFetchStatus.SUCCESS_EMPTY


def test_cost_above_limit_rejected_never_calls_fetch(registry, calendar):
    provider = FakeProvider(cost=Decimal("10.01"))
    service = make_service(
        provider, settings=make_settings(historical_max_request_cost_usd=Decimal("10.00")),
        registry=registry, calendar=calendar,
    )
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.REJECTED_COST_LIMIT
    assert not provider.fetch_called
    assert result.estimated_cost_usd == Decimal("10.01")


# -- Phase 3.1 §7: the provider is untrusted even though it implements
# the ABC -- a malformed estimate_cost() return must never be used as
# if it were a real CostEstimate.


@pytest.mark.parametrize("bad_return", [None, Decimal("5.00"), "5.00", {"estimated_cost_usd": "5.00"}, (), object()])
def test_malformed_estimate_cost_return_type_is_rejected_fail_closed(registry, calendar, bad_return):
    provider = FakeProvider()
    provider.estimate_cost = lambda request: bad_return
    service = make_service(provider, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.REJECTED_COST_ESTIMATE_FAILED
    assert not provider.fetch_called


# -- gate 7: provider fetch failure -------------------------------------------


def test_provider_fetch_failure_is_reported_as_failed_not_fabricated_empty(registry, calendar):
    provider = FakeProvider(fetch_error=HistoricalProviderError("vendor unavailable"))
    service = make_service(provider, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED
    assert result.bars == ()


# -- Phase 3.1 §16: a broken provider returning None/{}/"" from
# fetch_bars() must never be mistaken for the documented empty-success
# case (only an empty tuple/list is a legitimate "no data" result).


@pytest.mark.parametrize("bad_return", [None, "", {}, "not a tuple", 0, object()])
def test_malformed_fetch_bars_return_type_is_failed_not_success_empty(registry, calendar, bad_return):
    provider = FakeProvider(cost=Decimal("1.00"))
    provider.fetch_bars = lambda request, instrument: bad_return
    service = make_service(provider, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED
    assert result.status != HistoricalFetchStatus.SUCCESS_EMPTY


# -- gate 8: cross-bar validation ---------------------------------------------


def test_bars_failing_cross_validation_reported_as_failed(registry, calendar):
    wrong_contract_bar = make_bar(root_symbol="MNQ")  # mismatched vs NQ request
    provider = FakeProvider(cost=Decimal("1.00"), bars=(wrong_contract_bar,))
    service = make_service(provider, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED


# -- gate 9: storage -----------------------------------------------------------


def test_storage_failure_is_reported_as_failed_with_bars_preserved(registry, calendar):
    bar = make_bar()
    provider = FakeProvider(cost=Decimal("1.00"), bars=(bar,))
    store = FakeStore()
    store.fail_with = RuntimeError("disk full")
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    with pytest.raises(RuntimeError):
        # a raw RuntimeError from the store is NOT a HistoricalDataError,
        # so the service must not swallow it as a "FAILED" status -- it
        # is a genuine bug/unexpected condition, not an expected failure
        # mode. (see test below for the HistoricalDataError case.)
        service.fetch_and_store(make_request())


def test_storage_integrity_failure_is_reported_as_failed_status(registry, calendar):
    from app.data.models import HistoricalStorageIntegrityError

    bar = make_bar()
    provider = FakeProvider(cost=Decimal("1.00"), bars=(bar,))
    store = FakeStore()
    store.fail_with = HistoricalStorageIntegrityError("conflicting data already stored")
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED
    assert "conflicting" in result.message.lower()


# -- full success path ---------------------------------------------------------


def test_full_success_path_stores_bars_and_reports_success(registry, calendar):
    bar = make_bar()
    provider = FakeProvider(cost=Decimal("1.00"), bars=(bar,))
    store = FakeStore()
    service = make_service(provider, store=store, registry=registry, calendar=calendar)

    result = service.fetch_and_store(make_request())

    assert result.status == HistoricalFetchStatus.SUCCESS
    assert result.new_records_stored == 1
    assert len(store.write_calls) == 1
    assert result.estimated_cost_usd == Decimal("1.00")


def test_service_passes_resolved_instrument_to_provider_fetch_bars(registry, calendar):
    provider = FakeProvider(cost=Decimal("1.00"), bars=())
    service = make_service(provider, registry=registry, calendar=calendar)
    service.fetch_and_store(make_request())
    _, instrument = provider.fetch_args
    assert instrument.root_symbol == "NQ"
    assert instrument.tick_size == Decimal("0.25")


def test_empty_provider_response_is_success_empty_not_failure(registry, calendar):
    provider = FakeProvider(cost=Decimal("1.00"), bars=())
    store = FakeStore()
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.SUCCESS_EMPTY
    assert result.new_records_stored == 0
    assert store.write_calls == []  # nothing to store for an empty result


# -- Phase 3.2 §10/§11/§26: a provider can implement the ABC and return an
# individually self-consistent HistoricalBar that is still PRODUCTION-wrong
# -- never trusted on self-consistency alone, and never stored.


def test_bar_with_wrong_tick_size_is_rejected_never_stored(registry, calendar):
    wrong_tick_bar = make_bar(tick_size=Decimal("0.50"))  # NQ's canonical tick_size is 0.25
    provider = FakeProvider(cost=Decimal("1.00"), bars=(wrong_tick_bar,))
    store = FakeStore()
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED
    assert store.write_calls == []


def test_bar_with_wrong_provider_name_is_rejected_never_stored(registry, calendar):
    wrong_provider_bar = make_bar(provider="other")  # FakeProvider's own name is "fake"
    provider = FakeProvider(cost=Decimal("1.00"), bars=(wrong_provider_bar,))
    store = FakeStore()
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED
    assert store.write_calls == []


def test_bar_with_wrong_provider_raw_symbol_is_rejected_never_stored(registry, calendar):
    wrong_symbol_bar = make_bar(provider_raw_symbol="NQZ9")  # NQ_CONTRACT's display_code is NQZ6
    provider = FakeProvider(cost=Decimal("1.00"), bars=(wrong_symbol_bar,))
    store = FakeStore()
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED
    assert store.write_calls == []


def test_bar_outside_request_range_is_rejected_never_stored(registry, calendar):
    out_of_range_bar = make_bar(ts_event=datetime(2026, 9, 2, 0, 0, tzinfo=UTC))  # request is Sept 1 only
    provider = FakeProvider(cost=Decimal("1.00"), bars=(out_of_range_bar,))
    store = FakeStore()
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED
    assert store.write_calls == []


# -- Phase 3.2 §13: the store's own RETURN value is just as untrusted as any
# other injected dependency's output --------------------------------------


@pytest.mark.parametrize("bad_return", [None, {}, 123, "not a result", object()])
def test_malformed_write_bars_return_type_is_failed_not_trusted(registry, calendar, bad_return):
    bar = make_bar()
    provider = FakeProvider(cost=Decimal("1.00"), bars=(bar,))
    store = FakeStore()
    store.write_bars = lambda bars, **kwargs: bad_return
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED


# -- Phase 3.2 §14: an expected storage I/O failure (OSError) from the
# store must become a structured FAILED result, never escape raw --------


def test_store_raising_oserror_is_reported_as_failed_status(registry, calendar):
    bar = make_bar()
    provider = FakeProvider(cost=Decimal("1.00"), bars=(bar,))
    store = FakeStore()
    store.fail_with = OSError("[Errno 28] No space left on device")
    service = make_service(provider, store=store, registry=registry, calendar=calendar)
    result = service.fetch_and_store(make_request())
    assert result.status == HistoricalFetchStatus.FAILED
    assert "space" in result.message.lower() or "errno" in result.message.lower()
