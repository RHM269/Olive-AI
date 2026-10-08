"""Phase 4 tests: app.config.Settings must validate and canonicalize
its own real-time-data fields in __post_init__ exactly as safely as
the Phase 3 historical fields -- DIRECT construction (bypassing
load_settings()) is exactly as safe as going through the loader --
and load_settings() must parse the new OLIVE_LIVE_* environment
variables with the same safe-default, fail-closed discipline.

Mirrors tests/test_historical_data_config.py's structure and the
specific "bool-as-int"/"truthy string" defect class it exists to catch
-- OLIVE_LIVE_NETWORK_ENABLED is a SEPARATE kill switch from
OLIVE_HISTORICAL_NETWORK_ENABLED and must be exactly as hard to bypass.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.config import (
    ConfigurationError,
    DataMode,
    Environment,
    HistoricalProviderKind,
    LiveProviderKind,
    Settings,
    load_settings,
)

_OLIVE_LIVE_ENV_VARS = (
    "OLIVE_LIVE_PROVIDER",
    "OLIVE_LIVE_NETWORK_ENABLED",
    "OLIVE_LIVE_STALE_THRESHOLD_SECONDS",
    "OLIVE_LIVE_RECONNECT_MAX_ATTEMPTS",
    "OLIVE_LIVE_RECONNECT_BASE_DELAY_SECONDS",
    "OLIVE_LIVE_RECONNECT_MAX_DELAY_SECONDS",
    "DATABENTO_API_KEY",
    "APP_NAME",
    "ENVIRONMENT",
    "LOG_LEVEL",
    "OLIVE_DATA_MODE",
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _OLIVE_LIVE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def make_kwargs(**overrides) -> dict:
    base = dict(
        app_name="t",
        environment=Environment.DEVELOPMENT,
        log_level="INFO",
        data_mode=DataMode.DEVELOPMENT,
        historical_provider=HistoricalProviderKind.UNCONFIGURED,
        databento_api_key="",
        historical_data_dir=Path("/tmp/olive-live-config-test"),
        historical_network_enabled=False,
        historical_max_request_cost_usd=Decimal("0"),
        live_provider=LiveProviderKind.UNCONFIGURED,
        live_network_enabled=False,
        live_stale_threshold_seconds=Decimal("10"),
        live_reconnect_max_attempts=5,
        live_reconnect_base_delay_seconds=Decimal("1"),
        live_reconnect_max_delay_seconds=Decimal("30"),
    )
    base.update(overrides)
    return base


# -- live_provider ------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [None, "databento", "unconfigured", 1, True, object()])
def test_live_provider_rejects_non_enum_values(bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(live_provider=bad_value))


def test_live_provider_accepts_both_members():
    assert Settings(**make_kwargs(live_provider=LiveProviderKind.UNCONFIGURED)).live_provider is (
        LiveProviderKind.UNCONFIGURED
    )
    assert Settings(**make_kwargs(live_provider=LiveProviderKind.DATABENTO)).live_provider is (
        LiveProviderKind.DATABENTO
    )


def test_live_provider_is_a_distinct_enum_from_historical_provider():
    """Phase 4's own stated design decision: the two enums must stay
    independent types, even though they share member values today."""
    assert LiveProviderKind is not HistoricalProviderKind
    assert not issubclass(LiveProviderKind, HistoricalProviderKind)
    assert not issubclass(HistoricalProviderKind, LiveProviderKind)


# -- live_network_enabled: the critical kill-switch-bypass finding, replayed --


@pytest.mark.parametrize(
    "bad_value",
    ["false", "true", "0", "1", "no", "yes", "", "   ", 0, 1, 2, -1, None, 0.0, 1.0, "False"],
)
def test_live_network_enabled_rejects_every_non_bool_value(bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(live_network_enabled=bad_value))


def test_live_network_enabled_accepts_real_bools():
    off = Settings(**make_kwargs(live_network_enabled=False))
    assert off.live_network_enabled is False
    on = Settings(**make_kwargs(live_network_enabled=True))
    assert on.live_network_enabled is True


def test_live_network_enabled_is_independent_of_historical_network_enabled():
    """Enabling one kill switch must never flip the other."""
    settings = Settings(**make_kwargs(historical_network_enabled=True, live_network_enabled=False))
    assert settings.historical_network_enabled is True
    assert settings.live_network_enabled is False

    settings2 = Settings(**make_kwargs(historical_network_enabled=False, live_network_enabled=True))
    assert settings2.historical_network_enabled is False
    assert settings2.live_network_enabled is True


# -- live_stale_threshold_seconds / reconnect delay fields: shared validator --


@pytest.mark.parametrize(
    "field_name",
    ["live_stale_threshold_seconds", "live_reconnect_base_delay_seconds", "live_reconnect_max_delay_seconds"],
)
@pytest.mark.parametrize("bad_value", [None, "10", 10, True, False, 1.5, object(), [], {}])
def test_positive_decimal_timing_fields_reject_non_decimal_types(field_name, bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(**{field_name: bad_value}))


@pytest.mark.parametrize(
    "field_name",
    ["live_stale_threshold_seconds", "live_reconnect_base_delay_seconds", "live_reconnect_max_delay_seconds"],
)
@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_positive_decimal_timing_fields_reject_non_finite(field_name, bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(**{field_name: bad_value}))


@pytest.mark.parametrize(
    "field_name",
    ["live_stale_threshold_seconds", "live_reconnect_base_delay_seconds", "live_reconnect_max_delay_seconds"],
)
@pytest.mark.parametrize("bad_value", [Decimal("0"), Decimal("-1")])
def test_positive_decimal_timing_fields_reject_zero_and_negative(field_name, bad_value):
    """Unlike historical_max_request_cost_usd (which legitimately
    allows zero), these three Phase 4 timing fields require STRICTLY
    positive values -- a zero stale threshold or zero-length backoff
    delay is not meaningful."""
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(**{field_name: bad_value}))


def test_positive_decimal_timing_fields_accept_positive_decimal():
    settings = Settings(
        **make_kwargs(
            live_stale_threshold_seconds=Decimal("5.5"),
            live_reconnect_base_delay_seconds=Decimal("0.5"),
            live_reconnect_max_delay_seconds=Decimal("60"),
        )
    )
    assert settings.live_stale_threshold_seconds == Decimal("5.5")
    assert settings.live_reconnect_base_delay_seconds == Decimal("0.5")
    assert settings.live_reconnect_max_delay_seconds == Decimal("60")


# -- live_reconnect_max_attempts: true-int, bool-as-int trap ------------------


@pytest.mark.parametrize("bad_value", [None, "5", 5.0, True, False, object(), [], {}])
def test_reconnect_max_attempts_rejects_non_true_int(bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(live_reconnect_max_attempts=bad_value))


def test_reconnect_max_attempts_rejects_negative():
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(live_reconnect_max_attempts=-1))


def test_reconnect_max_attempts_accepts_zero_and_positive_int():
    assert Settings(**make_kwargs(live_reconnect_max_attempts=0)).live_reconnect_max_attempts == 0
    assert Settings(**make_kwargs(live_reconnect_max_attempts=10)).live_reconnect_max_attempts == 10


# -- cross-field: max_delay >= base_delay -------------------------------------


def test_max_delay_less_than_base_delay_rejected():
    with pytest.raises(ConfigurationError):
        Settings(
            **make_kwargs(
                live_reconnect_base_delay_seconds=Decimal("10"),
                live_reconnect_max_delay_seconds=Decimal("5"),
            )
        )


def test_max_delay_equal_to_base_delay_accepted():
    settings = Settings(
        **make_kwargs(
            live_reconnect_base_delay_seconds=Decimal("10"),
            live_reconnect_max_delay_seconds=Decimal("10"),
        )
    )
    assert settings.live_reconnect_max_delay_seconds == Decimal("10")


def test_max_delay_greater_than_base_delay_accepted():
    settings = Settings(
        **make_kwargs(
            live_reconnect_base_delay_seconds=Decimal("1"),
            live_reconnect_max_delay_seconds=Decimal("30"),
        )
    )
    assert settings.live_reconnect_max_delay_seconds == Decimal("30")


# -- databento_api_key is reused, never duplicated ----------------------------


def test_databento_api_key_is_shared_between_historical_and_live():
    settings = Settings(
        **make_kwargs(
            historical_provider=HistoricalProviderKind.DATABENTO,
            live_provider=LiveProviderKind.DATABENTO,
            databento_api_key="shared-key",
        )
    )
    assert settings.databento_api_key == "shared-key"
    assert settings.has_databento_api_key is True


# -- live_config_summary(): secret-safe status rendering ----------------------


def test_live_config_summary_never_includes_the_api_key():
    settings = Settings(**make_kwargs(databento_api_key="super-secret-value"))
    summary = settings.live_config_summary()
    assert "super-secret-value" not in summary
    assert "not set" not in summary or settings.databento_api_key == ""


def test_live_config_summary_reports_configured_when_key_present():
    settings = Settings(**make_kwargs(databento_api_key="super-secret-value"))
    summary = settings.live_config_summary()
    assert "databento_api_key=configured" in summary
    assert "super-secret-value" not in summary


def test_live_config_summary_reports_not_set_when_key_absent():
    settings = Settings(**make_kwargs(databento_api_key=""))
    summary = settings.live_config_summary()
    assert "databento_api_key=not set" in summary


def test_api_key_never_appears_in_settings_repr():
    settings = Settings(**make_kwargs(databento_api_key="super-secret-value"))
    assert "super-secret-value" not in repr(settings)


# -- load_settings(): environment-variable parsing ----------------------------


def test_load_settings_defaults_are_fail_closed(clean_env):
    settings = load_settings(load_dotenv_file=False)
    assert settings.live_provider is LiveProviderKind.UNCONFIGURED
    assert settings.live_network_enabled is False
    assert settings.live_stale_threshold_seconds == Decimal("10")
    assert settings.live_reconnect_max_attempts == 5
    assert settings.live_reconnect_base_delay_seconds == Decimal("1")
    assert settings.live_reconnect_max_delay_seconds == Decimal("30")


def test_load_settings_parses_valid_live_provider(clean_env):
    clean_env.setenv("OLIVE_LIVE_PROVIDER", "databento")
    settings = load_settings(load_dotenv_file=False)
    assert settings.live_provider is LiveProviderKind.DATABENTO


def test_load_settings_rejects_invalid_live_provider(clean_env):
    clean_env.setenv("OLIVE_LIVE_PROVIDER", "not-a-real-provider")
    with pytest.raises(ConfigurationError):
        load_settings(load_dotenv_file=False)


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("yes", True), ("on", True),
                                           ("false", False), ("0", False), ("no", False), ("off", False)])
def test_load_settings_parses_valid_live_network_enabled(clean_env, raw, expected):
    clean_env.setenv("OLIVE_LIVE_NETWORK_ENABLED", raw)
    settings = load_settings(load_dotenv_file=False)
    assert settings.live_network_enabled is expected


def test_load_settings_rejects_malformed_live_network_enabled(clean_env):
    clean_env.setenv("OLIVE_LIVE_NETWORK_ENABLED", "maybe")
    with pytest.raises(ConfigurationError):
        load_settings(load_dotenv_file=False)


def test_load_settings_parses_valid_stale_threshold(clean_env):
    clean_env.setenv("OLIVE_LIVE_STALE_THRESHOLD_SECONDS", "15.5")
    settings = load_settings(load_dotenv_file=False)
    assert settings.live_stale_threshold_seconds == Decimal("15.5")


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-5", "NaN", "Infinity"])
def test_load_settings_rejects_malformed_stale_threshold(clean_env, raw):
    clean_env.setenv("OLIVE_LIVE_STALE_THRESHOLD_SECONDS", raw)
    with pytest.raises(ConfigurationError):
        load_settings(load_dotenv_file=False)


def test_load_settings_parses_valid_reconnect_max_attempts(clean_env):
    clean_env.setenv("OLIVE_LIVE_RECONNECT_MAX_ATTEMPTS", "7")
    settings = load_settings(load_dotenv_file=False)
    assert settings.live_reconnect_max_attempts == 7


@pytest.mark.parametrize("raw", ["not-an-int", "1.5", "-1", "true", ""])
def test_load_settings_rejects_malformed_reconnect_max_attempts(clean_env, raw):
    if raw == "":
        # Empty string legitimately means "unset" -> falls back to
        # default, not an error; excluded from the malformed set.
        clean_env.delenv("OLIVE_LIVE_RECONNECT_MAX_ATTEMPTS", raising=False)
        settings = load_settings(load_dotenv_file=False)
        assert settings.live_reconnect_max_attempts == 5
        return
    clean_env.setenv("OLIVE_LIVE_RECONNECT_MAX_ATTEMPTS", raw)
    with pytest.raises(ConfigurationError):
        load_settings(load_dotenv_file=False)


def test_load_settings_parses_valid_reconnect_delays(clean_env):
    clean_env.setenv("OLIVE_LIVE_RECONNECT_BASE_DELAY_SECONDS", "2")
    clean_env.setenv("OLIVE_LIVE_RECONNECT_MAX_DELAY_SECONDS", "45")
    settings = load_settings(load_dotenv_file=False)
    assert settings.live_reconnect_base_delay_seconds == Decimal("2")
    assert settings.live_reconnect_max_delay_seconds == Decimal("45")


def test_load_settings_rejects_max_delay_below_base_delay_from_env(clean_env):
    clean_env.setenv("OLIVE_LIVE_RECONNECT_BASE_DELAY_SECONDS", "50")
    clean_env.setenv("OLIVE_LIVE_RECONNECT_MAX_DELAY_SECONDS", "10")
    with pytest.raises(ConfigurationError):
        load_settings(load_dotenv_file=False)


def test_load_settings_reuses_databento_api_key_for_both_subsystems(clean_env):
    clean_env.setenv("DATABENTO_API_KEY", "a-real-key")
    clean_env.setenv("OLIVE_HISTORICAL_PROVIDER", "databento")
    clean_env.setenv("OLIVE_LIVE_PROVIDER", "databento")
    settings = load_settings(load_dotenv_file=False)
    assert settings.databento_api_key == "a-real-key"
    assert settings.historical_provider is HistoricalProviderKind.DATABENTO
    assert settings.live_provider is LiveProviderKind.DATABENTO
    # Only one DATABENTO_API_KEY setting exists -- not duplicated per subsystem.
    assert not hasattr(settings, "live_databento_api_key")


def test_load_settings_live_network_enabled_independent_of_historical(clean_env):
    clean_env.setenv("OLIVE_HISTORICAL_NETWORK_ENABLED", "true")
    settings = load_settings(load_dotenv_file=False)
    assert settings.historical_network_enabled is True
    assert settings.live_network_enabled is False
