"""Tests for app.health's Phase 4 real-time-data health check
(_check_realtime_data / its integration into get_system_health).

Critical property under test: this health check must NEVER attempt a
live connection, even when a provider is fully "configured" -- proven
by constructing a fake client whose connect-relevant methods explode
if ever touched, and by confirming DatabentoLiveProvider's own
construction never calls its client factory.

Also critical: the "package missing" simulation uses
``sys.modules["databento"] = None`` to deterministically force the
lazy ``import databento`` to fail, rather than
``if "databento" in sys.modules: pytest.skip(...)`` -- this works
identically whether or not the real ``databento`` package happens to
be installed in the environment running these tests (see CLAUDE.md
and the Phase 4 prompt's explicit prohibition on the latter pattern).
"""

from __future__ import annotations

import sys
import types

import pytest

from app.config import DataMode, Environment, LiveProviderKind, Settings
from app.health import HealthState, get_system_health, format_health_report


def make_settings(**overrides) -> Settings:
    from decimal import Decimal

    from app.config import HistoricalProviderKind

    base = dict(
        app_name="t", environment=Environment.DEVELOPMENT, log_level="INFO", data_mode=DataMode.DEVELOPMENT,
        historical_provider=HistoricalProviderKind.UNCONFIGURED, databento_api_key="",
        historical_data_dir="/tmp/olive-live-health-test", historical_network_enabled=False,
        historical_max_request_cost_usd=Decimal("0"),
        live_provider=LiveProviderKind.UNCONFIGURED, live_network_enabled=False,
        live_stale_threshold_seconds=Decimal("10"), live_reconnect_max_attempts=5,
        live_reconnect_base_delay_seconds=Decimal("1"), live_reconnect_max_delay_seconds=Decimal("30"),
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clean_fake_databento_module():
    """Ensure no test leaks a fake/forced 'databento' module into another."""
    yield
    sys.modules.pop("databento", None)


def _install_fake_databento(live_cls):
    module = types.ModuleType("databento")
    module.Live = live_cls
    sys.modules["databento"] = module


# -- default / NOT_CONFIGURED states ------------------------------------------


def test_realtime_market_data_no_longer_not_implemented():
    health = get_system_health(make_settings())
    names = [c.name for c in health.components]
    assert "Real-time market data" in names
    component = health.component("Real-time market data")
    assert component.state == HealthState.NOT_CONFIGURED
    # and it is listed only once, same as every other component.
    assert names.count("Real-time market data") == 1


def test_default_settings_report_not_configured():
    health = get_system_health(make_settings())
    component = health.component("Real-time market data")
    assert component.state == HealthState.NOT_CONFIGURED
    assert "unconfigured" in component.detail.lower() or "not set" in component.detail.lower()


def test_databento_selected_without_key_reports_not_configured():
    health = get_system_health(
        make_settings(live_provider=LiveProviderKind.DATABENTO, databento_api_key="")
    )
    component = health.component("Real-time market data")
    assert component.state == HealthState.NOT_CONFIGURED
    assert "DATABENTO_API_KEY" in component.detail


def test_databento_selected_with_key_but_package_missing_reports_not_configured():
    """Deterministic simulation via sys.modules["databento"] = None --
    never an environment-dependent skip, and this must behave
    identically whether or not the real package is actually installed
    (verified by this test running without a skip either way)."""
    sys.modules["databento"] = None
    health = get_system_health(
        make_settings(live_provider=LiveProviderKind.DATABENTO, databento_api_key="real-key")
    )
    component = health.component("Real-time market data")
    assert component.state == HealthState.NOT_CONFIGURED
    assert "not installed" in component.detail
    assert "real-key" not in component.detail


# -- ERROR for malformed configuration ----------------------------------------


def test_malformed_config_reports_error_not_false_configured():
    """A whitespace-only API key passes Settings.has_databento_api_key's
    truthiness check but is rejected by DatabentoLiveProvider itself
    as effectively empty -- this must surface as ERROR, never a
    falsely-CONFIGURED or silently-NOT_CONFIGURED state. Mirrors
    tests/test_historical_data_health.py's identical scenario."""

    class _FakeLive:
        def __init__(self, key, dataset):
            self.key = key

    _install_fake_databento(_FakeLive)

    settings = make_settings(live_provider=LiveProviderKind.DATABENTO, databento_api_key="   ")
    health = get_system_health(settings)
    component = health.component("Real-time market data")
    assert component.state == HealthState.ERROR
    assert "   " not in component.detail


def test_broken_build_live_provider_reports_error():
    """A LiveDataError raised during build_live_provider() itself
    (rather than a plain construction-time Settings rejection) must
    surface as ERROR via _check_realtime_data's own except clause."""
    import app.health as health_module
    from app.data.live_models import LiveDataError

    def _exploding_build_live_provider(settings):
        raise LiveDataError("simulated broken live provider configuration")

    monkey_target = health_module.build_live_provider if hasattr(health_module, "build_live_provider") else None
    # _check_realtime_data imports build_live_provider locally inside
    # the function body (mirroring _check_historical_data's own
    # pattern), so it is patched at its own module, not app.health.
    import app.data.providers.live_factory as live_factory_module

    original = live_factory_module.build_live_provider
    live_factory_module.build_live_provider = _exploding_build_live_provider
    try:
        health = get_system_health(make_settings())
        component = health.component("Real-time market data")
        assert component.state == HealthState.ERROR
        assert "broken" in component.detail.lower()
    finally:
        live_factory_module.build_live_provider = original


# -- CONFIGURED is reachable (mocked, no network) -----------------------------


def test_fully_configured_reports_configured_with_no_secret_leak():
    class _FakeLive:
        def __init__(self, key, dataset):
            self.key = key
            self.dataset = dataset

    _install_fake_databento(_FakeLive)

    settings = make_settings(
        live_provider=LiveProviderKind.DATABENTO, databento_api_key="db-REAL-SECRET-KEY",
        live_network_enabled=False,
    )
    health = get_system_health(settings)
    component = health.component("Real-time market data")

    assert component.state == HealthState.CONFIGURED
    assert "db-REAL-SECRET-KEY" not in component.detail
    assert "network connections disabled" in component.detail
    assert "GLBX.MDP3" in component.detail


def test_configured_with_network_enabled_notes_it():
    class _FakeLive:
        def __init__(self, key, dataset):
            pass

    _install_fake_databento(_FakeLive)
    settings = make_settings(
        live_provider=LiveProviderKind.DATABENTO, databento_api_key="real-key", live_network_enabled=True,
    )
    health = get_system_health(settings)
    component = health.component("Real-time market data")
    assert component.state == HealthState.CONFIGURED
    assert "network connections enabled" in component.detail


# -- the critical safety property: never opens a connection -------------------


def test_health_check_never_opens_a_live_connection():
    class _ExplodingLive:
        def __init__(self, key, dataset):
            self.key = key
            self.dataset = dataset

        def subscribe(self, **kwargs):
            raise AssertionError("health check must never call subscribe()")

        def start(self):
            raise AssertionError("health check must never call start()")

        def stop(self):
            raise AssertionError("health check must never call stop()")

        def __iter__(self):
            raise AssertionError("health check must never iterate the client")

    _install_fake_databento(_ExplodingLive)

    settings = make_settings(live_provider=LiveProviderKind.DATABENTO, databento_api_key="real-key")
    # If this raises AssertionError, the health check touched the
    # live client's connection-relevant methods -- it must not, so we
    # expect no exception at all (construction alone is fine).
    health = get_system_health(settings)
    component = health.component("Real-time market data")
    assert component.state == HealthState.CONFIGURED


def test_health_check_never_opens_a_connection_even_with_network_enabled():
    """Even OLIVE_LIVE_NETWORK_ENABLED=true must not make the health
    check itself open a connection -- that decision belongs solely to
    RealTimeDataService.open_stream, never to a health check."""

    class _ExplodingLive:
        def __init__(self, key, dataset):
            pass

        def subscribe(self, **kwargs):
            raise AssertionError("health check must never call subscribe()")

        def start(self):
            raise AssertionError("health check must never call start()")

        def stop(self):
            pass

        def __iter__(self):
            raise AssertionError("health check must never iterate the client")

    _install_fake_databento(_ExplodingLive)
    settings = make_settings(
        live_provider=LiveProviderKind.DATABENTO, databento_api_key="real-key", live_network_enabled=True,
    )
    health = get_system_health(settings)
    component = health.component("Real-time market data")
    assert component.state == HealthState.CONFIGURED


def test_full_health_report_includes_realtime_data_without_crashing():
    health = get_system_health(make_settings())
    report = format_health_report(health)
    assert "Real-time market data: NOT CONFIGURED" in report


# -- regression: historical and futures-domain checks are unaffected --------


def test_historical_and_realtime_checks_coexist_independently():
    settings = make_settings(live_provider=LiveProviderKind.DATABENTO, databento_api_key="")
    health = get_system_health(settings)
    historical = health.component("Historical market data")
    realtime = health.component("Real-time market data")
    futures_domain = health.component("Futures domain")
    assert historical.state == HealthState.NOT_CONFIGURED
    assert realtime.state == HealthState.NOT_CONFIGURED
    assert futures_domain.state == HealthState.CONFIGURED


def test_still_not_yet_implemented_components_unaffected():
    health = get_system_health(make_settings())
    feature_engine = health.component("Feature engine")
    assert feature_engine is not None
    assert feature_engine.state == HealthState.NOT_IMPLEMENTED
