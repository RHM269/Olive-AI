"""Phase 4 tests for app.data.live_service.RealTimeDataService.

Mirrors tests/test_historical_data_service.py's discipline: every
safety gate (subscription validation, Olive futures-domain gate,
production tradability, provider configuration, network kill switch)
must be enforced IN ORDER, before the provider's own connect() is
ever reached, and every expected failure mode must raise a specific
LiveDataError subclass rather than a raw exception.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.config import load_settings
from app.futures.models import ContractMonth, FuturesContract
from app.data.live_models import (
    ConnectionState,
    InvalidLiveServiceConfigurationError,
    InvalidLiveSubscriptionError,
    LiveEventType,
    LiveNetworkDisabledError,
    LiveProviderNotConfiguredError,
    LiveStreamStatus,
    LiveSubscriptionRequest,
)
from app.data.live_provider_base import RealTimeMarketDataProvider
from app.data.live_service import RealTimeDataService
from app.data.providers.unconfigured_live import UnconfiguredLiveProvider

NQ = FuturesContract(root_symbol="NQ", year=2026, month=ContractMonth.DECEMBER)


class FakeConnectedProvider(RealTimeMarketDataProvider):
    """A minimal, fully-configured fake provider for exercising
    RealTimeDataService's own orchestration logic without any real
    Databento machinery."""

    def __init__(self, connect_error: Exception | None = None):
        self._state = ConnectionState.DISCONNECTED
        self.connect_calls: list = []
        self._connect_error = connect_error

    @property
    def name(self) -> str:
        return "fake"

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def status(self) -> LiveStreamStatus:
        return LiveStreamStatus(state=self._state)

    def connect(self, subscription, instruments):
        self.connect_calls.append((subscription, dict(instruments)))
        if self._connect_error is not None:
            raise self._connect_error
        self._state = ConnectionState.CONNECTED

    def events(self):
        return iter(())

    def close(self):
        self._state = ConnectionState.STOPPED


@pytest.fixture
def settings():
    return load_settings(load_dotenv_file=False)


@pytest.fixture
def network_enabled_settings(settings):
    return dataclasses.replace(settings, live_network_enabled=True)


def make_sub(contracts=(NQ,), event_types=(LiveEventType.TRADE,)):
    return LiveSubscriptionRequest(contracts=contracts, event_types=event_types)


# -- Constructor validation -------------------------------------------------


def test_constructor_rejects_non_provider(settings):
    with pytest.raises(InvalidLiveServiceConfigurationError):
        RealTimeDataService("not a provider", settings)


def test_constructor_rejects_non_settings():
    with pytest.raises(InvalidLiveServiceConfigurationError):
        RealTimeDataService(UnconfiguredLiveProvider(), "not settings")


def test_constructor_rejects_malformed_registry(settings):
    with pytest.raises(InvalidLiveServiceConfigurationError):
        RealTimeDataService(UnconfiguredLiveProvider(), settings, registry="not a registry")


def test_constructor_rejects_malformed_calendar(settings):
    with pytest.raises(InvalidLiveServiceConfigurationError):
        RealTimeDataService(UnconfiguredLiveProvider(), settings, calendar="not a calendar")


def test_constructor_loads_real_registry_and_calendar_by_default(network_enabled_settings):
    """No injected registry/calendar -> loads Olive's real Phase 2
    configuration (a local file read, never network I/O)."""
    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, network_enabled_settings)
    # If construction reached here without raising, the real registry
    # and calendar loaded successfully; exercised further below via
    # open_stream's own domain-validation gate.
    result = service.open_stream(make_sub())
    assert result is provider


# -- open_stream: subscription validation ------------------------------------


def test_open_stream_rejects_non_subscription(network_enabled_settings):
    service = RealTimeDataService(FakeConnectedProvider(), network_enabled_settings)
    with pytest.raises(InvalidLiveSubscriptionError):
        service.open_stream("not a subscription")


def test_open_stream_rejects_when_production_domain_is_broken(network_enabled_settings):
    """A registry missing one of Olive's required roots fails the
    whole-domain production gate (validate_olive_futures_domain),
    reached BEFORE any provider interaction -- this is the first of
    the two production-tradability layers open_stream enforces (see
    app.data.validation's "generic structural validity vs. Olive
    production correctness" split, also documented in CLAUDE.md)."""
    from app.futures.registry import FuturesInstrumentRegistry, load_instrument_registry

    real_registry = load_instrument_registry()
    mnq_only_registry = FuturesInstrumentRegistry({"MNQ": real_registry.get("MNQ")})

    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, network_enabled_settings, registry=mnq_only_registry)
    with pytest.raises(InvalidLiveServiceConfigurationError):
        service.open_stream(make_sub(contracts=(NQ,)))
    assert provider.connect_calls == []  # never reached the provider


def test_open_stream_rejects_contract_with_wrong_canonical_spec():
    """The SECOND production-tradability layer
    (require_olive_tradable_contract, applied per subscribed contract)
    is reached once the whole-domain gate passes: a registry that
    still declares exactly {NQ, MNQ} (so validate_olive_futures_domain
    itself passes) but whose NQ entry does not match Olive's canonical
    financial specification (wrong tick_size) must be rejected per
    contract, not merely accepted because the domain "shape" is
    right."""
    from app.futures.registry import FuturesInstrumentRegistry, load_instrument_registry

    real_registry = load_instrument_registry()
    # Self-consistent (tick_value == tick_size * multiplier still
    # holds) but wrong for NQ's actual canonical spec -- exactly the
    # Phase 2 "generic structural validity vs. Olive production
    # correctness" distinction this gate exists to catch.
    wrong_nq = dataclasses.replace(real_registry.get("NQ"), tick_size=Decimal("1.00"), tick_value=Decimal("20.00"))
    broken_registry = FuturesInstrumentRegistry({"NQ": wrong_nq, "MNQ": real_registry.get("MNQ")})

    provider = FakeConnectedProvider()
    settings = dataclasses.replace(load_settings(load_dotenv_file=False), live_network_enabled=True)
    service = RealTimeDataService(provider, settings, registry=broken_registry)
    with pytest.raises((InvalidLiveSubscriptionError, InvalidLiveServiceConfigurationError)):
        service.open_stream(make_sub(contracts=(NQ,)))
    assert provider.connect_calls == []


# -- open_stream: provider configuration gate --------------------------------


def test_open_stream_rejects_unconfigured_provider(network_enabled_settings):
    service = RealTimeDataService(UnconfiguredLiveProvider("no provider selected"), network_enabled_settings)
    with pytest.raises(LiveProviderNotConfiguredError, match="no provider selected"):
        service.open_stream(make_sub())


# -- open_stream: network kill switch ----------------------------------------


def test_open_stream_rejects_when_network_disabled(settings):
    """settings.live_network_enabled defaults False."""
    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, settings)
    with pytest.raises(LiveNetworkDisabledError):
        service.open_stream(make_sub())
    assert provider.connect_calls == []  # provider.connect() never called


def test_open_stream_proceeds_when_network_enabled(network_enabled_settings):
    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, network_enabled_settings)
    result = service.open_stream(make_sub())
    assert result is provider
    assert len(provider.connect_calls) == 1
    assert provider.state is ConnectionState.CONNECTED


def test_network_kill_switch_is_independent_of_historical_kill_switch(settings):
    """Enabling the HISTORICAL network flag must never also enable
    the live one."""
    historical_enabled_only = dataclasses.replace(settings, historical_network_enabled=True)
    assert historical_enabled_only.live_network_enabled is False
    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, historical_enabled_only)
    with pytest.raises(LiveNetworkDisabledError):
        service.open_stream(make_sub())


# -- open_stream: instrument map passed to provider.connect() ---------------


def test_open_stream_passes_production_validated_instruments_to_connect(network_enabled_settings):
    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, network_enabled_settings)
    service.open_stream(make_sub())
    _, instruments = provider.connect_calls[0]
    assert NQ.identity in instruments
    assert instruments[NQ.identity].root_symbol == "NQ"
    assert instruments[NQ.identity].tick_size == Decimal("0.25")


# -- open_stream: provider connect() failure propagates ----------------------


def test_open_stream_propagates_provider_connect_failure(network_enabled_settings):
    from app.data.live_models import LiveProviderAuthenticationError

    provider = FakeConnectedProvider(connect_error=LiveProviderAuthenticationError("auth failed"))
    service = RealTimeDataService(provider, network_enabled_settings)
    with pytest.raises(LiveProviderAuthenticationError):
        service.open_stream(make_sub())


# -- close_stream / status passthrough ---------------------------------------


def test_close_stream_delegates_to_provider():
    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, load_settings(load_dotenv_file=False))
    service.close_stream()
    assert provider.state is ConnectionState.STOPPED


def test_status_property_delegates_to_provider():
    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, load_settings(load_dotenv_file=False))
    assert service.status.state is ConnectionState.DISCONNECTED


def test_provider_property_exposes_underlying_provider():
    provider = FakeConnectedProvider()
    service = RealTimeDataService(provider, load_settings(load_dotenv_file=False))
    assert service.provider is provider


# -- is_feed_unexpectedly_stale: session-aware disambiguation ----------------


class _StaleStatusProvider(FakeConnectedProvider):
    def __init__(self, is_stale: bool):
        super().__init__()
        self._is_stale = is_stale

    @property
    def status(self) -> LiveStreamStatus:
        return LiveStreamStatus(state=ConnectionState.CONNECTED, is_stale=self._is_stale)


def test_is_feed_unexpectedly_stale_false_when_provider_not_stale():
    service = RealTimeDataService(_StaleStatusProvider(False), load_settings(load_dotenv_file=False))
    assert service.is_feed_unexpectedly_stale(at=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)) is False


def test_is_feed_unexpectedly_stale_false_during_weekend_closure():
    service = RealTimeDataService(_StaleStatusProvider(True), load_settings(load_dotenv_file=False))
    sunday_morning_utc = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
    assert service.is_feed_unexpectedly_stale(at=sunday_morning_utc) is False


def test_is_feed_unexpectedly_stale_false_during_maintenance_window():
    service = RealTimeDataService(_StaleStatusProvider(True), load_settings(load_dotenv_file=False))
    # 16:30 Chicago (CDT, UTC-5 in October) = 21:30 UTC on a weekday is
    # within the documented [16:00, 17:00) maintenance window.
    weekday_maintenance_utc = datetime(2026, 10, 6, 21, 30, tzinfo=timezone.utc)
    assert service.is_feed_unexpectedly_stale(at=weekday_maintenance_utc) is False


def test_is_feed_unexpectedly_stale_true_during_open_session():
    service = RealTimeDataService(_StaleStatusProvider(True), load_settings(load_dotenv_file=False))
    weekday_open_utc = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
    assert service.is_feed_unexpectedly_stale(at=weekday_open_utc) is True


def test_is_feed_unexpectedly_stale_uses_current_time_when_at_omitted():
    service = RealTimeDataService(_StaleStatusProvider(False), load_settings(load_dotenv_file=False))
    # Not stale regardless of session, since the provider itself
    # reports is_stale=False -- exercises the "at=None" default path
    # without making the test's correctness depend on wall-clock time.
    assert service.is_feed_unexpectedly_stale() is False
