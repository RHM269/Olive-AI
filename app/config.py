"""Central, typed configuration for Olive AI.

This module defines Olive's configuration contract and loads it from
environment variables (optionally via a local ``.env`` file) with safe,
explicit defaults suitable for local development. Importing this module
performs no I/O and no validation on its own -- settings are only read
and validated when :func:`load_settings` is called.

No market-data credentials or secrets are required to load a valid
Settings instance. Phase 1 does not talk to any external service.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class Environment(str, Enum):
    """The runtime environment Olive AI believes it is running in."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class DataMode(str, Enum):
    """The current source/nature of market data Olive AI is using.

    Phase 1 implements no market-data system at all, so the only
    meaningful value today is ``DEVELOPMENT``. The remaining members
    are declared now so later phases (historical data, replay, live
    streaming) have a stable, already-reviewed vocabulary to grow into.
    """

    DEVELOPMENT = "development"
    DEMO = "demo"
    HISTORICAL = "historical"
    REPLAY = "replay"
    LIVE = "live"


class ConfigurationError(ValueError):
    """Raised when supplied configuration is present but invalid.

    This is distinct from simply using defaults: Olive only raises this
    when a developer/operator has supplied a value that cannot be
    safely interpreted (e.g. ``ENVIRONMENT=staging``), so that bad
    configuration fails loudly instead of silently behaving oddly.
    """


DEFAULT_APP_NAME = "Olive AI"
DEFAULT_ENVIRONMENT = Environment.DEVELOPMENT
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_DATA_MODE = DataMode.DEVELOPMENT

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True)
class Settings:
    """Olive AI's resolved, immutable application settings."""

    app_name: str
    environment: Environment
    log_level: str
    data_mode: DataMode

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


def _parse_environment(raw: str | None) -> Environment:
    if raw is None or raw.strip() == "":
        return DEFAULT_ENVIRONMENT
    normalized = raw.strip().lower()
    try:
        return Environment(normalized)
    except ValueError as exc:
        valid = ", ".join(member.value for member in Environment)
        raise ConfigurationError(
            f"Invalid ENVIRONMENT={raw!r}. Must be one of: {valid}"
        ) from exc


def _parse_data_mode(raw: str | None) -> DataMode:
    if raw is None or raw.strip() == "":
        return DEFAULT_DATA_MODE
    normalized = raw.strip().lower()
    try:
        return DataMode(normalized)
    except ValueError as exc:
        valid = ", ".join(member.value for member in DataMode)
        raise ConfigurationError(
            f"Invalid OLIVE_DATA_MODE={raw!r}. Must be one of: {valid}"
        ) from exc


def _parse_log_level(raw: str | None) -> str:
    if raw is None or raw.strip() == "":
        return DEFAULT_LOG_LEVEL
    normalized = raw.strip().upper()
    if normalized not in _VALID_LOG_LEVELS:
        valid = ", ".join(sorted(_VALID_LOG_LEVELS))
        raise ConfigurationError(
            f"Invalid LOG_LEVEL={raw!r}. Must be one of: {valid}"
        )
    return normalized


def load_settings(load_dotenv_file: bool = True) -> Settings:
    """Resolve :class:`Settings` from the current environment.

    Reads ``APP_NAME``, ``ENVIRONMENT``, ``LOG_LEVEL``, and
    ``OLIVE_DATA_MODE`` from ``os.environ``. Unset variables fall back
    to safe development defaults; a variable that *is* set but cannot
    be parsed raises :class:`ConfigurationError`.

    Args:
        load_dotenv_file: When true (the default), attempt to load a
            local ``.env`` file via ``python-dotenv`` before reading
            ``os.environ``. This never overrides variables already
            present in the environment, and never raises if no
            ``.env`` file exists or ``python-dotenv`` is unavailable.
            Tests should pass ``False`` so they never depend on a
            developer's local ``.env``.
    """
    if load_dotenv_file:
        try:
            from dotenv import load_dotenv

            load_dotenv(override=False)
        except ImportError:
            # python-dotenv is an optional convenience; its absence
            # must never prevent Olive from starting.
            pass

    app_name = os.environ.get("APP_NAME", "").strip() or DEFAULT_APP_NAME
    environment = _parse_environment(os.environ.get("ENVIRONMENT"))
    log_level = _parse_log_level(os.environ.get("LOG_LEVEL"))
    data_mode = _parse_data_mode(os.environ.get("OLIVE_DATA_MODE"))

    return Settings(
        app_name=app_name,
        environment=environment,
        log_level=log_level,
        data_mode=data_mode,
    )
