"""Phase 3.1 regression tests: app.config.Settings must validate and
canonicalize its own Phase 3 historical-data fields in __post_init__, so
DIRECT construction (bypassing load_settings()) is exactly as safe as
going through the loader.

Covers the critical finding from independent review: a Settings object
built with historical_network_enabled="false" (a truthy, non-empty
STRING) previously passed straight through, silently defeating the
network kill switch -- `if not settings.historical_network_enabled`
evaluated False (string is truthy), never blocking the request.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import (
    ConfigurationError,
    DataMode,
    Environment,
    HistoricalProviderKind,
    Settings,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def make_kwargs(**overrides) -> dict:
    base = dict(
        app_name="t",
        environment=Environment.DEVELOPMENT,
        log_level="INFO",
        data_mode=DataMode.DEVELOPMENT,
        historical_provider=HistoricalProviderKind.UNCONFIGURED,
        databento_api_key="",
        historical_data_dir=Path("/tmp/olive-config-test"),
        historical_network_enabled=False,
        historical_max_request_cost_usd=Decimal("0"),
    )
    base.update(overrides)
    return base


# -- the critical kill-switch-bypass finding --------------------------------


@pytest.mark.parametrize(
    "bad_value",
    ["false", "true", "0", "1", "no", "yes", "", "   ", 0, 1, 2, -1, None, 0.0, 1.0, "False"],
)
def test_network_enabled_rejects_every_non_bool_value(bad_value):
    """Settings(..., historical_network_enabled="false") must never
    produce a usable Settings object -- a truthy string must not
    silently bypass the network kill switch."""
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(historical_network_enabled=bad_value))


def test_network_enabled_accepts_real_bools():
    settings_off = Settings(**make_kwargs(historical_network_enabled=False))
    assert settings_off.historical_network_enabled is False
    settings_on = Settings(**make_kwargs(historical_network_enabled=True))
    assert settings_on.historical_network_enabled is True


def test_string_false_cannot_construct_a_settings_object_at_all():
    """The specific independent-review reproduction: constructing
    Settings with the string "false" must raise before a Settings
    instance capable of reaching provider access ever exists."""
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(historical_network_enabled="false"))


# -- historical_provider ------------------------------------------------------


@pytest.mark.parametrize("bad_value", [None, "databento", "unconfigured", 1, True, object()])
def test_historical_provider_rejects_non_enum_values(bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(historical_provider=bad_value))


# -- databento_api_key ---------------------------------------------------------


@pytest.mark.parametrize("bad_value", [None, 123, True, b"bytes-key", object()])
def test_databento_api_key_rejects_non_str_values(bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(databento_api_key=bad_value))


def test_databento_api_key_accepts_empty_and_real_strings():
    assert Settings(**make_kwargs(databento_api_key="")).databento_api_key == ""
    assert Settings(**make_kwargs(databento_api_key="real-key")).databento_api_key == "real-key"


# -- historical_max_request_cost_usd ------------------------------------------


@pytest.mark.parametrize(
    "bad_value", [None, "0", "10.00", 0, 10, True, False, 1.5, object(), [], {}]
)
def test_max_cost_rejects_non_decimal_types(bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(historical_max_request_cost_usd=bad_value))


@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_max_cost_rejects_non_finite_decimal(bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(historical_max_request_cost_usd=bad_value))


def test_max_cost_rejects_negative_decimal():
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(historical_max_request_cost_usd=Decimal("-0.01")))


def test_max_cost_accepts_zero_and_positive_decimal():
    assert Settings(**make_kwargs(historical_max_request_cost_usd=Decimal("0"))).historical_max_request_cost_usd == Decimal("0")
    assert Settings(**make_kwargs(historical_max_request_cost_usd=Decimal("5.25"))).historical_max_request_cost_usd == Decimal("5.25")


# -- historical_data_dir: type validation -------------------------------------


@pytest.mark.parametrize("bad_value", [None, 123, True, [], {}, object()])
def test_historical_data_dir_rejects_malformed_types(bad_value):
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(historical_data_dir=bad_value))


def test_historical_data_dir_rejects_empty_string():
    with pytest.raises(ConfigurationError):
        Settings(**make_kwargs(historical_data_dir=""))


def test_historical_data_dir_accepts_str_and_coerces_to_path():
    settings = Settings(**make_kwargs(historical_data_dir="/tmp/olive-str-path"))
    assert isinstance(settings.historical_data_dir, Path)


# -- historical_data_dir: cwd independence (Phase 3.1 §3) --------------------


def test_relative_historical_data_dir_resolves_against_project_root_not_cwd(monkeypatch, tmp_path):
    """Identical configuration must point to the same place regardless
    of the process's current working directory."""
    other_cwd = tmp_path / "somewhere-else-entirely"
    other_cwd.mkdir()

    monkeypatch.chdir(other_cwd)
    settings = Settings(**make_kwargs(historical_data_dir=Path("data/historical")))

    assert settings.historical_data_dir.is_absolute()
    assert settings.historical_data_dir == (_PROJECT_ROOT / "data" / "historical").resolve()
    assert str(other_cwd) not in str(settings.historical_data_dir)


def test_relative_historical_data_dir_identical_from_different_cwds(monkeypatch, tmp_path):
    cwd_a = tmp_path / "cwd_a"
    cwd_b = tmp_path / "cwd_b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    monkeypatch.chdir(cwd_a)
    settings_a = Settings(**make_kwargs(historical_data_dir=Path("custom/historical")))

    monkeypatch.chdir(cwd_b)
    settings_b = Settings(**make_kwargs(historical_data_dir=Path("custom/historical")))

    assert settings_a.historical_data_dir == settings_b.historical_data_dir


def test_absolute_historical_data_dir_is_kept_exactly(tmp_path):
    absolute = tmp_path / "exact-absolute-path"
    settings = Settings(**make_kwargs(historical_data_dir=absolute))
    assert settings.historical_data_dir == absolute


def test_default_historical_data_dir_is_already_absolute():
    settings = Settings(**make_kwargs())
    assert settings.historical_data_dir.is_absolute()
