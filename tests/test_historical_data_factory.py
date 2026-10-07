"""Tests for app.data.providers.factory.build_historical_provider.

No test here requires the real ``databento`` package or a network
connection -- the "package installed" branch is exercised by injecting a
minimal fake module into ``sys.modules`` (removed again in a finally
block so it never leaks into other test files).
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal

import pytest

from app.config import DataMode, Environment, HistoricalProviderKind, Settings
from app.data.models import InvalidHistoricalServiceConfigurationError
from app.data.providers.databento import DatabentoHistoricalProvider
from app.data.providers.factory import build_historical_provider
from app.data.providers.unconfigured import UnconfiguredHistoricalProvider


def make_settings(**overrides) -> Settings:
    base = dict(
        app_name="t", environment=Environment.DEVELOPMENT, log_level="INFO", data_mode=DataMode.DEVELOPMENT,
        historical_provider=HistoricalProviderKind.UNCONFIGURED, databento_api_key="",
        historical_data_dir="/tmp/olive-factory-test", historical_network_enabled=False,
        historical_max_request_cost_usd=Decimal("0"),
    )
    base.update(overrides)
    return Settings(**base)


def test_default_settings_build_unconfigured_provider():
    provider = build_historical_provider(make_settings())
    assert isinstance(provider, UnconfiguredHistoricalProvider)
    assert "not set" in provider.reason or "unconfigured" in provider.reason.lower()


def test_databento_selected_without_key_builds_unconfigured_naming_reason():
    provider = build_historical_provider(
        make_settings(historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="")
    )
    assert isinstance(provider, UnconfiguredHistoricalProvider)
    assert "DATABENTO_API_KEY" in provider.reason


def test_databento_selected_with_key_but_package_missing_builds_unconfigured(monkeypatch):
    """Simulate an environment where importing Databento genuinely fails,
    regardless of whether the real package is installed in this venv."""
    monkeypatch.setitem(sys.modules, "databento", None)

    provider = build_historical_provider(
        make_settings(historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="real-key")
    )
    assert isinstance(provider, UnconfiguredHistoricalProvider)
    assert "not installed" in provider.reason


def test_databento_selected_with_key_and_package_builds_real_provider():
    fake_databento = types.ModuleType("databento")

    class _FakeHistorical:
        def __init__(self, key):
            self.key = key

    fake_databento.Historical = _FakeHistorical
    sys.modules["databento"] = fake_databento
    try:
        provider = build_historical_provider(
            make_settings(historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="real-key")
        )
        assert isinstance(provider, DatabentoHistoricalProvider)
        assert provider.name == "databento"
    finally:
        del sys.modules["databento"]


def test_build_historical_provider_rejects_non_settings_type():
    """Phase 3.1 §17: an Olive-owned error, never a raw TypeError, at
    this constructor-adjacent boundary too."""
    with pytest.raises(InvalidHistoricalServiceConfigurationError):
        build_historical_provider("not a settings object")


@pytest.mark.parametrize("bad", [None, 123, object(), {}])
def test_build_historical_provider_rejects_other_malformed_types(bad):
    with pytest.raises(InvalidHistoricalServiceConfigurationError):
        build_historical_provider(bad)


def test_building_provider_never_touches_network(monkeypatch):
    """Constructing a provider via the factory must never itself call
    any method that would perform network I/O -- verified by injecting a
    fake databento.Historical whose methods explode if ever called."""
    fake_databento = types.ModuleType("databento")

    class _ExplodingHistorical:
        def __init__(self, key):
            class _Exploding:
                def __getattr__(self, name):
                    raise AssertionError(f"factory must never touch Historical.{name}")

            self.metadata = _Exploding()
            self.timeseries = _Exploding()

    fake_databento.Historical = _ExplodingHistorical
    sys.modules["databento"] = fake_databento
    try:
        provider = build_historical_provider(
            make_settings(historical_provider=HistoricalProviderKind.DATABENTO, databento_api_key="real-key")
        )
        assert isinstance(provider, DatabentoHistoricalProvider)  # constructed fine; nothing exploded
    finally:
        del sys.modules["databento"]
