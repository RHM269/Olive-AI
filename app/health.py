"""Structured, truthful system health reporting for Olive AI.

Olive must always be able to honestly describe what it can and cannot
currently do. This module defines a small, machine-readable health
contract (:class:`SystemHealth` / :class:`ComponentHealth`) that later
phases extend as real subsystems come online, plus a separate CLI
formatter so the same structured data can eventually feed Olive Web,
an API, or monitoring without duplicating logic.

This module performs no network calls and no market-data access of
any kind -- it only reports on Olive's own internal state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from app.config import Settings
from app.futures.models import FuturesDomainError
from app.futures.registry import load_instrument_registry
from app.futures.calendar import load_cycle_date_calendar
from app.futures.validation import validate_olive_futures_domain


class HealthState(str, Enum):
    """The possible states of an Olive component.

    ``NOT_IMPLEMENTED`` and ``NOT_CONFIGURED`` are deliberately
    distinct: a component that does not exist yet (e.g. historical
    market data before Phase 3) is ``NOT_IMPLEMENTED``, while a
    component that exists but is missing required configuration (e.g.
    a data provider with no API key, in a later phase) is
    ``NOT_CONFIGURED``.
    """

    ONLINE = "ONLINE"
    CONFIGURED = "CONFIGURED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ComponentHealth:
    """The health of a single named Olive subsystem."""

    name: str
    state: HealthState
    detail: str = ""


@dataclass(frozen=True)
class SystemHealth:
    """A complete, timestamped snapshot of Olive's system health."""

    generated_at: datetime
    environment: str
    data_mode: str
    components: tuple[ComponentHealth, ...]

    def component(self, name: str) -> ComponentHealth | None:
        """Look up a single component's health by name, if present."""
        for candidate in self.components:
            if candidate.name == name:
                return candidate
        return None

    @property
    def is_online(self) -> bool:
        system = self.component("System")
        return system is not None and system.state is HealthState.ONLINE


# Components not yet implemented, with the phase that will build them.
# Kept as simple (name, phase) pairs rather than scattering magic
# strings through get_system_health(). "Futures domain" is handled
# separately below via a real check now that Phase 2 implements it.
_NOT_YET_IMPLEMENTED: tuple[tuple[str, str], ...] = (
    ("Historical market data", "Phase 3"),
    ("Real-time market data", "Phase 4"),
    ("Feature engine", "Phase 5"),
    ("Strategy engine", "Phase 6"),
    ("Prediction engine", "Phase 7"),
    ("Signal engine", "Phase 8"),
    ("Backtesting engine", "Phase 9"),
    ("Paper trading", "Phase 10"),
    ("Alerts", "Phase 12+"),
    ("Olive Web", "Phase 11"),
)


def _check_futures_domain() -> ComponentHealth:
    """Real health check for the futures domain (Phase 2).

    Verifies the instrument registry actually loads, that the
    quarterly cycle-date calendar loads, and -- as of Phase 2.4 --
    that both actually satisfy Olive's required PRODUCTION NQ/MNQ
    domain, via ``app.futures.validation.validate_olive_futures_domain``.
    That single production-validation call absorbs what used to be a
    hand-rolled tradable-roots check here (exactly one home for that
    invariant now) and adds what a fourth external audit found this
    health check was still missing: confirming the loaded NQ/MNQ
    entries actually describe the real NQ/MNQ product, and that
    Olive's required source-backed official calendar coverage (see
    ``app.futures.validation.OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES``)
    hasn't silently disappeared or drifted. A structurally valid but
    factually wrong NQ/MNQ configuration, or a truncated calendar, can
    no longer report CONFIGURED. Any legitimate domain problem is
    reported as ERROR rather than crashing the whole health report or
    silently claiming CONFIGURED.
    """
    try:
        registry = load_instrument_registry()
    except FuturesDomainError as exc:
        return ComponentHealth(
            name="Futures domain",
            state=HealthState.ERROR,
            detail=f"Instrument registry failed to load: {exc}",
        )

    try:
        cycle_calendar = load_cycle_date_calendar()
    except FuturesDomainError as exc:
        return ComponentHealth(
            name="Futures domain",
            state=HealthState.ERROR,
            detail=f"Cycle-date calendar failed to load: {exc}",
        )

    try:
        validate_olive_futures_domain(registry, cycle_calendar)
    except FuturesDomainError as exc:
        return ComponentHealth(
            name="Futures domain",
            state=HealthState.ERROR,
            detail=f"Production futures-domain validation failed: {exc}",
        )

    return ComponentHealth(
        name="Futures domain",
        state=HealthState.CONFIGURED,
        detail=f"{', '.join(registry.roots)} loaded; cycle calendar as_of {cycle_calendar.as_of}.",
    )


def get_system_health(settings: Settings) -> SystemHealth:
    """Build Olive's current, truthful system health snapshot.

    ``System``, ``Configuration``, and (as of Phase 2) ``Futures
    domain`` are reported as real, verified state. Every subsystem
    still unimplemented (market data, features, strategies,
    predictions, signals, backtesting, paper trading, alerts, web) is
    honestly reported as ``NOT_IMPLEMENTED``.
    """
    components: list[ComponentHealth] = [
        ComponentHealth(
            name="System",
            state=HealthState.ONLINE,
            detail="Olive AI application started successfully.",
        ),
        ComponentHealth(
            name="Configuration",
            state=HealthState.CONFIGURED,
            detail=(
                f"environment={settings.environment.value}, "
                f"data_mode={settings.data_mode.value}"
            ),
        ),
        _check_futures_domain(),
    ]

    for name, phase in _NOT_YET_IMPLEMENTED:
        components.append(
            ComponentHealth(
                name=name,
                state=HealthState.NOT_IMPLEMENTED,
                detail=f"Planned for {phase}.",
            )
        )

    return SystemHealth(
        generated_at=datetime.now(timezone.utc),
        environment=settings.environment.value,
        data_mode=settings.data_mode.value,
        components=tuple(components),
    )


def format_health_report(health: SystemHealth) -> str:
    """Render a :class:`SystemHealth` snapshot as a CLI status report.

    Kept deliberately separate from :func:`get_system_health` so the
    structured data can later be reused by Olive Web or an API without
    reformatting text.
    """
    system = health.component("System")
    system_state = system.state.value if system is not None else HealthState.ERROR.value

    lines = [
        "OLIVE AI",
        "",
        f"SYSTEM STATUS: {system_state}",
        f"Environment: {health.environment}",
        f"Data mode: {health.data_mode}",
        "",
    ]

    for component in health.components:
        if component.name == "System":
            continue
        state_label = component.state.value.replace("_", " ")
        lines.append(f"{component.name}: {state_label}")

    return "\n".join(lines)
