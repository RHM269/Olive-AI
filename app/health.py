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


class HealthState(str, Enum):
    """The possible states of an Olive component.

    ``NOT_IMPLEMENTED`` and ``NOT_CONFIGURED`` are deliberately
    distinct: a component that does not exist yet (e.g. the futures
    domain in Phase 1) is ``NOT_IMPLEMENTED``, while a component that
    exists but is missing required configuration (e.g. a data provider
    with no API key, in a later phase) is ``NOT_CONFIGURED``.
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


# Components not yet implemented in Phase 1, with the phase that will
# build them. Kept as simple (name, phase) pairs rather than scattering
# magic strings through get_system_health().
_NOT_YET_IMPLEMENTED: tuple[tuple[str, str], ...] = (
    ("Futures domain", "Phase 2"),
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


def get_system_health(settings: Settings) -> SystemHealth:
    """Build Olive's current, truthful system health snapshot.

    Phase 1 only implements the application itself and its
    configuration layer, so only ``System`` and ``Configuration`` are
    reported as operational. Every other subsystem is honestly
    reported as ``NOT_IMPLEMENTED`` -- Olive has no futures domain, no
    market data, no features, no strategies, no predictions, no
    signals, no backtesting, no paper trading, no alerts, and no web
    interface yet.
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
