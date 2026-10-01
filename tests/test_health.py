"""Tests for app.health."""

from __future__ import annotations

from datetime import timezone

import pytest

from app.config import load_settings
from app.health import HealthState, format_health_report, get_system_health

_OLIVE_ENV_VARS = ("APP_NAME", "ENVIRONMENT", "LOG_LEVEL", "OLIVE_DATA_MODE")

# Every component Phase 1 must honestly report as not yet built.
_NOT_YET_BUILT = (
    "Futures domain",
    "Historical market data",
    "Real-time market data",
    "Feature engine",
    "Strategy engine",
    "Prediction engine",
    "Signal engine",
    "Backtesting engine",
    "Paper trading",
    "Alerts",
    "Olive Web",
)


@pytest.fixture
def settings(monkeypatch):
    for var in _OLIVE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return load_settings(load_dotenv_file=False)


def test_system_reports_online(settings):
    health = get_system_health(settings)

    system = health.component("System")

    assert system is not None
    assert system.state is HealthState.ONLINE
    assert health.is_online is True


def test_configuration_reports_configured(settings):
    health = get_system_health(settings)

    configuration = health.component("Configuration")

    assert configuration is not None
    assert configuration.state is HealthState.CONFIGURED


@pytest.mark.parametrize("name", _NOT_YET_BUILT)
def test_unbuilt_components_are_not_reported_as_operational(settings, name):
    health = get_system_health(settings)

    component = health.component(name)

    assert component is not None, f"Missing expected Phase 1 placeholder component: {name}"
    assert component.state not in (HealthState.ONLINE, HealthState.CONFIGURED), (
        f"{name} must not be reported as operational during Phase 1, "
        f"got {component.state}"
    )


def test_health_timestamp_is_timezone_aware(settings):
    health = get_system_health(settings)

    assert health.generated_at.tzinfo is not None
    assert health.generated_at.utcoffset() == timezone.utc.utcoffset(None)


def test_component_names_are_unique(settings):
    health = get_system_health(settings)

    names = [component.name for component in health.components]

    assert len(names) == len(set(names))


def test_health_is_structured_not_just_strings(settings):
    health = get_system_health(settings)

    for component in health.components:
        assert isinstance(component.name, str)
        assert isinstance(component.state, HealthState)


def test_unknown_component_lookup_returns_none(settings):
    health = get_system_health(settings)

    assert health.component("Nonexistent Component") is None


def test_format_health_report_reflects_component_states(settings):
    health = get_system_health(settings)

    report = format_health_report(health)

    assert "SYSTEM STATUS: ONLINE" in report
    assert "Configuration: CONFIGURED" in report
    assert "Futures domain: NOT IMPLEMENTED" in report
    assert "Olive Web: NOT IMPLEMENTED" in report
    # The System component itself should not be duplicated in the body.
    assert report.count("System:") == 0
