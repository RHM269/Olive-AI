"""Phase 4 tests for app.data.providers.live_factory.build_live_provider.

Mirrors tests/test_historical_data_factory.py's structure: every path
that cannot produce a working Databento live provider must return an
honest UnconfiguredLiveProvider naming the real reason, never silently
substitute a fake provider, and never perform a network call or import
the real 'databento' package merely by selecting it in configuration.
"""

from __future__ import annotations

import dataclasses
import sys
import types

import pytest

from app.config import LiveProviderKind, load_settings
from app.data.live_models import InvalidLiveServiceConfigurationError
from app.data.providers.databento_live import DatabentoLiveProvider
from app.data.providers.live_factory import build_live_provider
from app.data.providers.unconfigured_live import UnconfiguredLiveProvider


@pytest.fixture
def base_settings():
    return load_settings(load_dotenv_file=False)


@pytest.fixture(autouse=True)
def _clean_fake_databento_module():
    yield
    sys.modules.pop("databento", None)


def test_rejects_non_settings_argument():
    with pytest.raises(InvalidLiveServiceConfigurationError):
        build_live_provider("not a settings object")


def test_default_unconfigured_returns_unconfigured_provider(base_settings):
    provider = build_live_provider(base_settings)
    assert isinstance(provider, UnconfiguredLiveProvider)
    assert "OLIVE_LIVE_PROVIDER" in provider.reason


def test_databento_selected_without_key_returns_unconfigured(base_settings):
    settings = dataclasses.replace(base_settings, live_provider=LiveProviderKind.DATABENTO, databento_api_key="")
    provider = build_live_provider(settings)
    assert isinstance(provider, UnconfiguredLiveProvider)
    assert "DATABENTO_API_KEY" in provider.reason


def test_databento_selected_with_key_but_package_missing_returns_unconfigured(base_settings):
    sys.modules["databento"] = None  # deterministic missing-package simulation
    settings = dataclasses.replace(
        base_settings, live_provider=LiveProviderKind.DATABENTO, databento_api_key="real-key"
    )
    provider = build_live_provider(settings)
    assert isinstance(provider, UnconfiguredLiveProvider)
    assert "not installed" in provider.reason
    assert "real-key" not in provider.reason


def test_databento_selected_with_key_and_package_present_constructs_real_provider(base_settings):
    fake_module = types.ModuleType("databento")

    class FakeLive:
        def __init__(self, key, dataset):
            pass

    fake_module.Live = FakeLive
    sys.modules["databento"] = fake_module

    settings = dataclasses.replace(
        base_settings, live_provider=LiveProviderKind.DATABENTO, databento_api_key="real-key"
    )
    provider = build_live_provider(settings)
    assert isinstance(provider, DatabentoLiveProvider)
    assert provider.name == "databento"


def test_constructed_provider_uses_settings_reconnect_policy(base_settings):
    fake_module = types.ModuleType("databento")

    class FakeLive:
        def __init__(self, key, dataset):
            pass

    fake_module.Live = FakeLive
    sys.modules["databento"] = fake_module

    from decimal import Decimal

    settings = dataclasses.replace(
        base_settings,
        live_provider=LiveProviderKind.DATABENTO,
        databento_api_key="real-key",
        live_reconnect_max_attempts=9,
        live_reconnect_base_delay_seconds=Decimal("2"),
        live_reconnect_max_delay_seconds=Decimal("64"),
        live_stale_threshold_seconds=Decimal("20"),
    )
    provider = build_live_provider(settings)
    assert provider._reconnect_policy.max_attempts == 9
    assert provider._reconnect_policy.base_delay_seconds == Decimal("2")
    assert provider._reconnect_policy.max_delay_seconds == Decimal("64")
    assert provider._stale_threshold_seconds == Decimal("20")


def test_constructing_provider_never_imports_real_client_object(base_settings):
    """build_live_provider only checks the package is IMPORTABLE (an
    existence check) -- it must never actually construct a live client
    or open a connection."""
    fake_module = types.ModuleType("databento")
    constructed = {"n": 0}

    class FakeLive:
        def __init__(self, key, dataset):
            constructed["n"] += 1

    fake_module.Live = FakeLive
    sys.modules["databento"] = fake_module

    settings = dataclasses.replace(
        base_settings, live_provider=LiveProviderKind.DATABENTO, databento_api_key="real-key"
    )
    build_live_provider(settings)
    assert constructed["n"] == 0


def test_build_live_provider_does_not_affect_historical_provider_selection(base_settings):
    """Selecting a live provider must have no bearing on the
    independently-configured historical provider (and vice versa) --
    this factory touches only live_provider/live-subsystem fields."""
    from app.config import HistoricalProviderKind
    from app.data.providers.factory import build_historical_provider
    from app.data.providers.unconfigured import UnconfiguredHistoricalProvider

    settings = dataclasses.replace(
        base_settings,
        live_provider=LiveProviderKind.DATABENTO,
        databento_api_key="real-key",
        historical_provider=HistoricalProviderKind.UNCONFIGURED,
    )
    sys.modules["databento"] = types.SimpleNamespace(Live=lambda key, dataset: None)
    build_live_provider(settings)
    historical = build_historical_provider(settings)
    assert isinstance(historical, UnconfiguredHistoricalProvider)
