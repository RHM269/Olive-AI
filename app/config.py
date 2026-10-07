"""Central, typed configuration for Olive AI.

This module defines Olive's configuration contract and loads it from
environment variables (optionally via a local ``.env`` file) with safe,
explicit defaults suitable for local development. Importing this module
performs no I/O and no validation on its own -- settings are only read
and validated when :func:`load_settings` is called.

No market-data credentials or secrets are required to load a valid
Settings instance. Phase 1 does not talk to any external service.

Phase 3 hardening note: this module now also resolves Olive's historical
market-data configuration (``OLIVE_HISTORICAL_PROVIDER``,
``DATABENTO_API_KEY``, ``OLIVE_HISTORICAL_DATA_DIR``,
``OLIVE_HISTORICAL_NETWORK_ENABLED``,
``OLIVE_HISTORICAL_MAX_REQUEST_COST_USD``). Defaults are deliberately
fail-closed: an unconfigured provider, network access disabled, and a
zero spending cap -- loading settings, or any amount of importing,
testing, or running ``main.py``, can never by itself cause a paid
provider request. ``databento_api_key`` is stored with
``field(repr=False)`` so the secret can never appear in
``repr(settings)``, logs that print a settings object, or any other
default string rendering; :meth:`Settings.historical_config_summary`
is the one sanctioned way to describe historical configuration in
status output, and it never includes the key itself, only whether one
is present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path


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


class HistoricalProviderKind(str, Enum):
    """Which historical market-data provider Olive is configured to use.

    ``UNCONFIGURED`` is the only safe default. Selecting ``DATABENTO``
    here does not, by itself, authorize any network call or spending --
    see ``OLIVE_HISTORICAL_NETWORK_ENABLED`` and
    ``OLIVE_HISTORICAL_MAX_REQUEST_COST_USD`` below, both of which
    default closed.
    """

    UNCONFIGURED = "unconfigured"
    DATABENTO = "databento"


DEFAULT_APP_NAME = "Olive AI"
DEFAULT_ENVIRONMENT = Environment.DEVELOPMENT
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_DATA_MODE = DataMode.DEVELOPMENT
DEFAULT_HISTORICAL_PROVIDER = HistoricalProviderKind.UNCONFIGURED
DEFAULT_HISTORICAL_NETWORK_ENABLED = False
DEFAULT_HISTORICAL_MAX_REQUEST_COST_USD = Decimal("0")

# app/config.py -> app -> project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HISTORICAL_DATA_DIR = _PROJECT_ROOT / "data" / "historical"

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    """Olive AI's resolved, immutable application settings."""

    app_name: str
    environment: Environment
    log_level: str
    data_mode: DataMode

    # -- Phase 3: historical market-data configuration -----------------
    historical_provider: HistoricalProviderKind = DEFAULT_HISTORICAL_PROVIDER
    # repr=False: this value must never appear in repr(settings), an
    # f-string that interpolates the settings object, a log line, or
    # any other default string rendering. See historical_config_summary.
    databento_api_key: str = field(default="", repr=False)
    historical_data_dir: Path = DEFAULT_HISTORICAL_DATA_DIR
    historical_network_enabled: bool = DEFAULT_HISTORICAL_NETWORK_ENABLED
    historical_max_request_cost_usd: Decimal = DEFAULT_HISTORICAL_MAX_REQUEST_COST_USD

    def __post_init__(self) -> None:
        """Validate and canonicalize the Phase 3 historical-data fields
        so that DIRECT construction of ``Settings`` (bypassing
        :func:`load_settings` -- a test, or any other caller building
        one by hand) is exactly as safe as going through the loader.

        Phase 3.1 correction: independent review constructed
        ``Settings(..., historical_network_enabled="false")`` -- a
        truthy non-empty *string* that ``if not settings.historical_network_enabled``
        evaluates as enabled, silently bypassing Olive's network kill
        switch. ``Settings`` previously trusted its own type hints
        with no runtime check at all (unlike every other Phase 2/3
        domain object, which validates itself in ``__post_init__``).
        This closes that gap for every Phase 3 field, mirroring the
        bool/type-hint-violation discipline already applied throughout
        ``app.futures``/``app.data``. Olive's original four Phase 1
        fields are deliberately left as they were (the long-standing
        "trusted through the loader" Phase 1 convention) -- only the
        Phase 3 additions are hardened here.
        """
        if not isinstance(self.historical_provider, HistoricalProviderKind):
            raise ConfigurationError(
                f"Settings.historical_provider must be a HistoricalProviderKind, got "
                f"{self.historical_provider!r} ({type(self.historical_provider).__name__})"
            )

        if not isinstance(self.databento_api_key, str):
            raise ConfigurationError(
                f"Settings.databento_api_key must be a str, got {self.databento_api_key!r} "
                f"({type(self.databento_api_key).__name__})"
            )

        # bool is an int subtype, and here also -- in spirit -- a str
        # subtype trap: a non-empty string ("false", "0") is truthy in
        # Python, so accepting "str-that-looks-boolean" here would
        # silently defeat the kill switch this field exists to be.
        # Only an actual bool is ever accepted.
        if not isinstance(self.historical_network_enabled, bool):
            raise ConfigurationError(
                f"Settings.historical_network_enabled must be an actual bool (never a string, "
                f"int, or anything else truthy/falsy-looking), got "
                f"{self.historical_network_enabled!r} ({type(self.historical_network_enabled).__name__}). "
                f"A string such as \"false\" is truthy in Python and would silently bypass "
                f"Olive's historical-data network kill switch."
            )

        historical_max_request_cost_usd = self.historical_max_request_cost_usd
        if isinstance(historical_max_request_cost_usd, bool) or not isinstance(
            historical_max_request_cost_usd, Decimal
        ):
            raise ConfigurationError(
                f"Settings.historical_max_request_cost_usd must be a Decimal (never bool/int/"
                f"float/str), got {historical_max_request_cost_usd!r} "
                f"({type(historical_max_request_cost_usd).__name__})"
            )
        if not historical_max_request_cost_usd.is_finite():
            raise ConfigurationError(
                f"Settings.historical_max_request_cost_usd must be finite (not NaN/Infinity); "
                f"got {historical_max_request_cost_usd}"
            )
        if historical_max_request_cost_usd < 0:
            raise ConfigurationError(
                f"Settings.historical_max_request_cost_usd must not be negative; got "
                f"{historical_max_request_cost_usd}"
            )

        historical_data_dir = self.historical_data_dir
        if isinstance(historical_data_dir, str):
            if not historical_data_dir.strip():
                raise ConfigurationError("Settings.historical_data_dir must not be an empty string")
            historical_data_dir = Path(historical_data_dir)
        elif not isinstance(historical_data_dir, Path):
            raise ConfigurationError(
                f"Settings.historical_data_dir must be a Path or non-empty str, got "
                f"{historical_data_dir!r} ({type(historical_data_dir).__name__})"
            )

        # Phase 3.1 §3 "preferred" policy: a RELATIVE configured path is
        # resolved against Olive's project root, never the process's
        # current working directory -- identical configuration must
        # point to the same place regardless of where `python main.py`
        # or pytest was launched from. An absolute path is kept exactly
        # as given (never silently rewritten).
        if not historical_data_dir.is_absolute():
            historical_data_dir = (_PROJECT_ROOT / historical_data_dir).resolve()
        object.__setattr__(self, "historical_data_dir", historical_data_dir)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def has_databento_api_key(self) -> bool:
        """Whether a (non-empty) Databento API key is configured.

        Deliberately does not reveal the key itself -- only its
        presence/absence, which is all status/health reporting ever
        needs to know.
        """
        return bool(self.databento_api_key)

    def historical_config_summary(self) -> str:
        """A log/status-safe one-line description of historical
        configuration that never includes the API key itself.

        Use this (never an f-string built by hand elsewhere, and never
        ``repr(settings)``/``str(settings)``) anywhere Olive describes
        its historical configuration to a human or a log.
        """
        return (
            f"provider={self.historical_provider.value}, "
            f"databento_api_key={'configured' if self.has_databento_api_key else 'not set'}, "
            f"network_enabled={self.historical_network_enabled}, "
            f"max_request_cost_usd={self.historical_max_request_cost_usd}, "
            f"data_dir={self.historical_data_dir}"
        )


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


def _parse_historical_provider(raw: str | None) -> HistoricalProviderKind:
    if raw is None or raw.strip() == "":
        return DEFAULT_HISTORICAL_PROVIDER
    normalized = raw.strip().lower()
    try:
        return HistoricalProviderKind(normalized)
    except ValueError as exc:
        valid = ", ".join(member.value for member in HistoricalProviderKind)
        raise ConfigurationError(
            f"Invalid OLIVE_HISTORICAL_PROVIDER={raw!r}. Must be one of: {valid}"
        ) from exc


def _parse_historical_network_enabled(raw: str | None) -> bool:
    if raw is None or raw.strip() == "":
        return DEFAULT_HISTORICAL_NETWORK_ENABLED
    normalized = raw.strip().lower()
    if normalized in _TRUE_STRINGS:
        return True
    if normalized in _FALSE_STRINGS:
        return False
    raise ConfigurationError(
        f"Invalid OLIVE_HISTORICAL_NETWORK_ENABLED={raw!r}. "
        f"Must be one of: {sorted(_TRUE_STRINGS | _FALSE_STRINGS)}"
    )


def _parse_historical_max_request_cost_usd(raw: str | None) -> Decimal:
    if raw is None or raw.strip() == "":
        return DEFAULT_HISTORICAL_MAX_REQUEST_COST_USD
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ConfigurationError(
            f"Invalid OLIVE_HISTORICAL_MAX_REQUEST_COST_USD={raw!r}: must be a decimal number"
        ) from exc
    if not value.is_finite():
        raise ConfigurationError(
            f"Invalid OLIVE_HISTORICAL_MAX_REQUEST_COST_USD={raw!r}: must be finite"
        )
    if value < 0:
        raise ConfigurationError(
            f"Invalid OLIVE_HISTORICAL_MAX_REQUEST_COST_USD={raw!r}: must not be negative"
        )
    return value


def _parse_historical_data_dir(raw: str | None) -> Path:
    if raw is None or raw.strip() == "":
        return DEFAULT_HISTORICAL_DATA_DIR
    try:
        return Path(raw.strip())
    except TypeError as exc:
        raise ConfigurationError(
            f"Invalid OLIVE_HISTORICAL_DATA_DIR={raw!r}: must be a path-like string"
        ) from exc


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

    historical_provider = _parse_historical_provider(os.environ.get("OLIVE_HISTORICAL_PROVIDER"))
    # Deliberately not .strip()'d beyond whitespace trimming and never
    # logged/validated-by-format here -- Olive does not inspect, parse,
    # or echo back the key itself, only whether it is present.
    databento_api_key = (os.environ.get("DATABENTO_API_KEY") or "").strip()
    historical_data_dir = _parse_historical_data_dir(os.environ.get("OLIVE_HISTORICAL_DATA_DIR"))
    historical_network_enabled = _parse_historical_network_enabled(
        os.environ.get("OLIVE_HISTORICAL_NETWORK_ENABLED")
    )
    historical_max_request_cost_usd = _parse_historical_max_request_cost_usd(
        os.environ.get("OLIVE_HISTORICAL_MAX_REQUEST_COST_USD")
    )

    return Settings(
        app_name=app_name,
        environment=environment,
        log_level=log_level,
        data_mode=data_mode,
        historical_provider=historical_provider,
        databento_api_key=databento_api_key,
        historical_data_dir=historical_data_dir,
        historical_network_enabled=historical_network_enabled,
        historical_max_request_cost_usd=historical_max_request_cost_usd,
    )
