"""Tests for app.health."""

from __future__ import annotations

from datetime import timezone

import pytest

from decimal import Decimal

import app.health as health_module
from app.config import load_settings
from app.futures.models import ContractMonth, FuturesInstrument, SettlementType
from app.futures.registry import FuturesInstrumentRegistry, load_instrument_registry
from app.health import HealthState, format_health_report, get_system_health

_OLIVE_ENV_VARS = ("APP_NAME", "ENVIRONMENT", "LOG_LEVEL", "OLIVE_DATA_MODE")

# Every component still not built as of Phase 2 must honestly report as such.
# "Futures domain" is no longer in this list -- Phase 2 implements it for
# real, and its own CONFIGURED expectation is tested separately below.
_NOT_YET_BUILT = (
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

    assert component is not None, f"Missing expected placeholder component: {name}"
    assert component.state not in (HealthState.ONLINE, HealthState.CONFIGURED), (
        f"{name} must not be reported as operational yet, got {component.state}"
    )


def test_futures_domain_reports_configured_based_on_real_check(settings):
    """Phase 2: the futures domain must report CONFIGURED only because the
    registry and cycle calendar actually loaded and validated -- not a
    hard-coded string (see test_futures_health.py-style checks in
    tests/test_futures_registry.py / test_futures_calendar.py for the
    underlying guarantees this depends on)."""
    health = get_system_health(settings)

    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.CONFIGURED
    assert "NQ" in futures_domain.detail
    assert "MNQ" in futures_domain.detail


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
    assert "Futures domain: CONFIGURED" in report
    assert "Olive Web: NOT IMPLEMENTED" in report
    # The System component itself should not be duplicated in the body.
    assert report.count("System:") == 0


# ============================================================================
# Phase 2.1 adversarial regression tests (external review findings)
# ============================================================================


def _extra_instrument(root_symbol: str) -> FuturesInstrument:
    """Build a self-consistent, generically-valid instrument for an
    unrelated root symbol (e.g. "ES") -- used only to simulate a config
    file that loads successfully but contains an extra, non-Olive root.
    """
    return FuturesInstrument(
        root_symbol=root_symbol,
        display_name=f"{root_symbol} Futures",
        exchange="CME",
        underlying="Unrelated Index",
        currency="USD",
        multiplier=Decimal("50"),
        tick_size=Decimal("0.25"),
        tick_value=Decimal("12.50"),
        settlement_type=SettlementType.CASH,
        contract_months=(
            ContractMonth.MARCH,
            ContractMonth.JUNE,
            ContractMonth.SEPTEMBER,
            ContractMonth.DECEMBER,
        ),
    )


def test_production_health_rejects_unexpected_tradable_root(settings, monkeypatch):
    """A registry that loads successfully but contains an unexpected extra
    root (e.g. "ES") must be reported as ERROR, never as CONFIGURED --
    FuturesInstrumentRegistry is a generic loader and does not by itself
    enforce "Olive only trades NQ/MNQ" (that constraint is
    app.futures.validation's job, consumed by this health check).

    Phase 2.5 note: this uses a genuine ``FuturesInstrumentRegistry``
    (not a duck-typed stand-in) so it continues to exercise the real
    "wrong root set" logic in ``validate_olive_tradable_registry``
    rather than being short-circuited by the new ``_require_registry``
    type check added in this phase.
    """
    real_registry = load_instrument_registry()
    instruments = {root: real_registry.get(root) for root in real_registry.roots}
    instruments["ES"] = _extra_instrument("ES")
    extra_root_registry = FuturesInstrumentRegistry(instruments)

    monkeypatch.setattr(
        health_module,
        "load_instrument_registry",
        lambda: extra_root_registry,
    )

    health = get_system_health(settings)
    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.ERROR
    assert "unexpected" in futures_domain.detail
    assert "ES" in futures_domain.detail


def test_production_health_rejects_missing_tradable_root(settings, monkeypatch):
    """The mirror case: a registry missing one of Olive's two required
    roots must also be reported as ERROR, not CONFIGURED.

    Phase 2.5 note: uses a genuine ``FuturesInstrumentRegistry`` built
    from the real NQ instrument only, for the same reason as above.
    """
    real_registry = load_instrument_registry()
    nq_only_registry = FuturesInstrumentRegistry({"NQ": real_registry.get("NQ")})

    monkeypatch.setattr(
        health_module,
        "load_instrument_registry",
        lambda: nq_only_registry,
    )

    health = get_system_health(settings)
    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.ERROR
    assert "missing" in futures_domain.detail
    assert "MNQ" in futures_domain.detail


def test_production_health_rejects_duck_typed_fake_registry(settings, monkeypatch):
    """Phase 2.5 regression: a hand-built object that merely exposes a
    ``.roots`` attribute (the shape the old Phase 2.1-2.4 fake used) is
    not a real ``FuturesInstrumentRegistry`` and must be rejected by
    ``_require_registry`` before its fake attribute is ever trusted --
    reported as ERROR, never CONFIGURED, and never a raw
    ``AttributeError`` escaping ``get_system_health``.
    """

    class _DuckTypedFakeRegistry:
        def __init__(self, roots: tuple[str, ...]) -> None:
            self.roots = roots

        def get(self, root_symbol: str) -> FuturesInstrument:
            raise AssertionError("a rejected fake registry must never be queried for instruments")

    monkeypatch.setattr(
        health_module,
        "load_instrument_registry",
        lambda: _DuckTypedFakeRegistry(("NQ", "MNQ")),
    )

    health = get_system_health(settings)
    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.ERROR
    assert "FuturesInstrumentRegistry" in futures_domain.detail


def test_production_health_still_configured_for_real_registry(settings):
    """Sanity check alongside the two adversarial cases above: the real,
    unmodified NQ/MNQ config must still report CONFIGURED (regression
    guard against the adversarial tests' monkeypatching masking a break
    in the non-faked path).
    """
    health = get_system_health(settings)
    futures_domain = health.component("Futures domain")

    assert futures_domain is not None
    assert futures_domain.state is HealthState.CONFIGURED
