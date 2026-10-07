"""Tests for app.health's Phase 3 historical-data health check
(_check_historical_data / its integration into get_system_health).

The critical property under test: this health check must NEVER call any
Databento network method (get_cost/get_range), even when a provider is
fully "configured" -- enforced here by injecting a fake databento module
whose client methods raise AssertionError if ever touched.
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal

import pytest

from app.config import DataMode, Environment, HistoricalProviderKind, Settings
from app.health import HealthState, get_system_health, format_health_report


def make_settings(**overrides) -> Settings:
    base = dict(
        app_name="t", environment=Environment.DEVELOPMENT, log_level="INFO", data_mode=DataMode.DEVELOPMENT,
        historical_provider=HistoricalProviderKind.UNCONFIGURED, databento_api_key="",
        historical_data_dir="/tmp/olive-health-test", historical_network_enabled=False,
        historical_max_request_cost_usd=Decimal("0"),
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clean_fake_databento_module():
    """Ensure no test leaks a fake 'databento' module into another."""
    yield
    sys.modules.pop("databento", None)


def _install_fake_databento(client_cls):
    module = types.ModuleType("databento")
    module.Historical = client_cls
    sys.modules["databento"] = module


# -- default / NOT_CONFIGURED states ------------------------------------------


def test_historical_market_data_no_longer_not_implemented():
    health = get_system_health(make_settings())
    names = [c.name for c in health.components]
    assert "Historical market data" in names
    # the Phase 3 prompt's required default: NOT CONFIGURED, never
    # NOT_IMPLEMENTED (that would be dishonest -- this subsystem exists
    # now) and never falsely CONFIGURED.
    component = health.component("Historical market data")
    assert component.state == HealthState.NOT_CONFIGURED


def test_default_settings_report_not_configured():
    health = get_system_health(make_settings())
    component = health.component("Historical market data")
    assert component.state == HealthState.NOT_CONFIGURED
    assert "unconfigured" in component.detail.lower() or "not set" in component.detail.lower()


def test_databento_selected_without_key_reports_not_configured():
    health = get_system_health(
        make_settings(historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="")
    )
    component = health.component("Historical market data")
    assert component.state == HealthState.NOT_CONFIGURED
    assert "DATABENTO_API_KEY" in component.detail


def test_databento_selected_with_key_but_package_missing_reports_not_configured(monkeypatch):
    # Simulate an installed environment where importing Databento genuinely
    # fails. Merely removing/checking sys.modules is insufficient because
    # Python can immediately re-import an installed package from site-packages.
    monkeypatch.setitem(sys.modules, "databento", None)

    health = get_system_health(
        make_settings(historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="real-key")
    )
    component = health.component("Historical market data")
    assert component.state == HealthState.NOT_CONFIGURED
    assert "not installed" in component.detail
    assert "real-key" not in component.detail


# -- ERROR for malformed configuration ----------------------------------------


def test_malformed_config_reports_error_not_false_configured():
    """A whitespace-only API key passes Settings.has_databento_api_key's
    truthiness check but is rejected by DatabentoHistoricalProvider
    itself as effectively empty -- this must surface as ERROR, never as
    a falsely-CONFIGURED or silently-NOT_CONFIGURED state."""

    class _FakeHistorical:
        def __init__(self, key):
            self.key = key

    _install_fake_databento(_FakeHistorical)

    settings = make_settings(historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="   ")
    health = get_system_health(settings)
    component = health.component("Historical market data")
    assert component.state == HealthState.ERROR
    assert "   " not in component.detail


# -- CONFIGURED is reachable (mocked, no network) -----------------------------


def test_fully_configured_reports_configured_with_no_secret_leak():
    class _FakeHistorical:
        def __init__(self, key):
            self.key = key

    _install_fake_databento(_FakeHistorical)

    settings = make_settings(
        historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="db-REAL-SECRET-KEY",
        historical_network_enabled=False,
    )
    health = get_system_health(settings)
    component = health.component("Historical market data")

    assert component.state == HealthState.CONFIGURED
    assert "db-REAL-SECRET-KEY" not in component.detail
    assert "network fetches disabled" in component.detail
    assert "GLBX.MDP3" in component.detail


def test_configured_with_network_enabled_notes_it():
    class _FakeHistorical:
        def __init__(self, key):
            self.key = key

    _install_fake_databento(_FakeHistorical)
    settings = make_settings(
        historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="real-key",
        historical_network_enabled=True,
    )
    health = get_system_health(settings)
    component = health.component("Historical market data")
    assert component.state == HealthState.CONFIGURED
    assert "network fetches enabled" in component.detail


# -- the critical safety property: never touches network ---------------------


def test_health_check_never_calls_databento_network_methods():
    class _ExplodingMetadata:
        def get_cost(self, **kwargs):
            raise AssertionError("health check must never call metadata.get_cost")

    class _ExplodingTimeseries:
        def get_range(self, **kwargs):
            raise AssertionError("health check must never call timeseries.get_range")

    class _ExplodingHistorical:
        def __init__(self, key):
            self.metadata = _ExplodingMetadata()
            self.timeseries = _ExplodingTimeseries()

    _install_fake_databento(_ExplodingHistorical)

    settings = make_settings(historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="real-key")
    # If this raises AssertionError, the health check touched the network
    # client's methods -- it must not, so we expect no exception at all.
    health = get_system_health(settings)
    component = health.component("Historical market data")
    assert component.state == HealthState.CONFIGURED


def test_full_health_report_includes_historical_data_without_crashing():
    health = get_system_health(make_settings())
    report = format_health_report(health)
    assert "Historical market data: NOT CONFIGURED" in report


def test_historical_market_data_not_listed_as_not_implemented_anymore():
    health = get_system_health(make_settings())
    for component in health.components:
        if component.name == "Historical market data":
            assert component.state != HealthState.NOT_IMPLEMENTED
    # but the still-future Phase 4 component must remain honestly
    # NOT_IMPLEMENTED -- Phase 3 must not accidentally also "complete"
    # real-time market data.
    realtime = health.component("Real-time market data")
    assert realtime.state == HealthState.NOT_IMPLEMENTED
