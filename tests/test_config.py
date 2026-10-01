"""Tests for app.config.

These tests must never depend on a developer's local .env file, so
every call to load_settings() below passes load_dotenv_file=False and
every environment variable under test is explicitly cleared first via
monkeypatch (which restores os.environ automatically after each test).
"""

from __future__ import annotations

import pytest

from app.config import (
    ConfigurationError,
    DataMode,
    Environment,
    Settings,
    load_settings,
)

_OLIVE_ENV_VARS = ("APP_NAME", "ENVIRONMENT", "LOG_LEVEL", "OLIVE_DATA_MODE")


def _clear_olive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _OLIVE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_default_settings_are_safe_for_development(monkeypatch):
    _clear_olive_env(monkeypatch)

    settings = load_settings(load_dotenv_file=False)

    assert settings.app_name == "Olive AI"
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.log_level == "INFO"
    assert settings.data_mode is DataMode.DEVELOPMENT


def test_environment_variables_override_defaults(monkeypatch):
    _clear_olive_env(monkeypatch)
    monkeypatch.setenv("APP_NAME", "Olive AI Test")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("OLIVE_DATA_MODE", "demo")

    settings = load_settings(load_dotenv_file=False)

    assert settings.app_name == "Olive AI Test"
    assert settings.environment is Environment.TEST
    assert settings.log_level == "DEBUG"
    assert settings.data_mode is DataMode.DEMO


def test_log_level_is_case_insensitive(monkeypatch):
    _clear_olive_env(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "warning")

    settings = load_settings(load_dotenv_file=False)

    assert settings.log_level == "WARNING"


def test_invalid_environment_raises_configuration_error(monkeypatch):
    _clear_olive_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")

    with pytest.raises(ConfigurationError):
        load_settings(load_dotenv_file=False)


def test_invalid_log_level_raises_configuration_error(monkeypatch):
    _clear_olive_env(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "VERY_LOUD")

    with pytest.raises(ConfigurationError):
        load_settings(load_dotenv_file=False)


def test_invalid_data_mode_raises_configuration_error(monkeypatch):
    _clear_olive_env(monkeypatch)
    monkeypatch.setenv("OLIVE_DATA_MODE", "fully_autonomous_live_trading")

    with pytest.raises(ConfigurationError):
        load_settings(load_dotenv_file=False)


def test_load_settings_requires_no_secrets(monkeypatch):
    _clear_olive_env(monkeypatch)

    # Must succeed with zero environment variables set -- Phase 1 has
    # no market-data provider and needs no credentials to start.
    settings = load_settings(load_dotenv_file=False)

    assert isinstance(settings, Settings)


def test_settings_is_immutable(monkeypatch):
    _clear_olive_env(monkeypatch)
    settings = load_settings(load_dotenv_file=False)

    with pytest.raises(Exception):
        settings.app_name = "mutated"  # type: ignore[misc]


def test_is_production_property(monkeypatch):
    _clear_olive_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")

    settings = load_settings(load_dotenv_file=False)

    assert settings.is_production is True

    _clear_olive_env(monkeypatch)
    dev_settings = load_settings(load_dotenv_file=False)

    assert dev_settings.is_production is False
