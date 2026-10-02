"""Tests for app.futures.registry: instrument loading and lookup."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.futures.models import FuturesConfigurationError, FuturesInstrument, SettlementType, UnknownInstrumentError
from app.futures.registry import DEFAULT_INSTRUMENT_CONFIG_PATH, FuturesInstrumentRegistry, load_instrument_registry


# -- Real configuration file tests -------------------------------------------------


def test_real_config_loads_nq_and_mnq():
    registry = load_instrument_registry()

    assert registry.contains("NQ")
    assert registry.contains("MNQ")
    assert len(registry) == 2


def test_real_config_only_contains_nq_mnq():
    registry = load_instrument_registry()

    assert set(registry.roots) == {"NQ", "MNQ"}


def test_get_is_case_insensitive():
    registry = load_instrument_registry()

    assert registry.get("nq").root_symbol == "NQ"
    assert registry.get("Nq").root_symbol == "NQ"


def test_get_unknown_root_raises():
    registry = load_instrument_registry()

    with pytest.raises(UnknownInstrumentError):
        registry.get("ES")


def test_contains_unknown_root_is_false():
    registry = load_instrument_registry()

    assert registry.contains("ES") is False
    assert "ES" not in registry


def test_all_returns_every_instrument():
    registry = load_instrument_registry()

    roots = {instrument.root_symbol for instrument in registry.all()}

    assert roots == {"NQ", "MNQ"}


def test_default_config_path_exists():
    assert DEFAULT_INSTRUMENT_CONFIG_PATH.exists()


# -- Malformed configuration tests (via a test-only tmp_path config file) ---------


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "instruments.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _valid_nq_entry(**overrides) -> dict:
    entry = {
        "root_symbol": "NQ",
        "display_name": "E-mini Nasdaq-100 Futures",
        "exchange": "CME",
        "underlying": "Nasdaq-100 Index",
        "currency": "USD",
        "multiplier": "20",
        "tick_size": "0.25",
        "tick_value": "5.00",
        "settlement_type": "CASH",
        "contract_months": [3, 6, 9, 12],
    }
    entry.update(overrides)
    return entry


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(tmp_path / "does_not_exist.json")


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_empty_instruments_list_raises(tmp_path):
    path = _write_config(tmp_path, {"instruments": []})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_duplicate_root_symbols_rejected(tmp_path):
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(), _valid_nq_entry()]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_invalid_tick_size_rejected(tmp_path):
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(tick_size="0")]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_tick_value_mismatch_rejected(tmp_path):
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(tick_value="99.00")]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_numeric_economics_field_rejected_not_string(tmp_path):
    # Economics fields must be JSON strings (not numbers) to avoid
    # floating-point precision issues.
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(multiplier=20)]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_unsupported_contract_month_rejected(tmp_path):
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(contract_months=[1, 2])]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_unknown_settlement_type_rejected(tmp_path):
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(settlement_type="FUTURE_DELIVERY")]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_missing_required_string_field_rejected(tmp_path):
    entry = _valid_nq_entry()
    del entry["exchange"]
    path = _write_config(tmp_path, {"instruments": [entry]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_non_list_instruments_rejected(tmp_path):
    path = _write_config(tmp_path, {"instruments": "NQ"})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_valid_custom_config_loads(tmp_path):
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry()]})

    registry = load_instrument_registry(path)

    assert registry.contains("NQ")
    assert len(registry) == 1


# ============================================================================
# Phase 2.2 adversarial regression tests (second external audit findings)
# ============================================================================

# -- Non-finite Decimal strings in JSON configuration ------------------------


@pytest.mark.parametrize("field", ["multiplier", "tick_size", "tick_value"])
@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_decimal_string_rejected(tmp_path, field, bad_value):
    """'NaN'/'Infinity'/'-Infinity' are valid Decimal-constructible
    strings (Decimal(value) does not raise InvalidOperation for them),
    so this must be caught by FuturesInstrument's own finite-value
    validation, not the registry's Decimal-parsing try/except -- and it
    must surface as a clean FuturesConfigurationError either way, never
    a raw decimal exception."""
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(**{field: bad_value})]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_non_finite_multiplier_and_tick_size_together_rejected(tmp_path):
    """The exact second-audit example: multiplier=1, tick_size=Infinity,
    tick_value=Infinity was previously accepted (Infinity * 1 == Infinity
    even satisfied the tick_value cross-check)."""
    path = _write_config(
        tmp_path,
        {"instruments": [_valid_nq_entry(multiplier="1", tick_size="Infinity", tick_value="Infinity")]},
    )

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


# -- FuturesInstrumentRegistry direct construction hardening -----------------


def _nq_instrument() -> FuturesInstrument:
    return FuturesInstrument(
        root_symbol="NQ",
        display_name="E-mini Nasdaq-100 Futures",
        exchange="CME",
        underlying="Nasdaq-100 Index",
        currency="USD",
        multiplier=Decimal("20"),
        tick_size=Decimal("0.25"),
        tick_value=Decimal("5.00"),
        settlement_type=SettlementType.CASH,
        contract_months=(3, 6, 9, 12),
    )


def test_registry_direct_construction_rejects_non_instrument_value():
    with pytest.raises(FuturesConfigurationError):
        FuturesInstrumentRegistry({"NQ": "not-an-instrument"})


def test_registry_direct_construction_rejects_key_root_mismatch():
    nq = _nq_instrument()
    with pytest.raises(FuturesConfigurationError):
        FuturesInstrumentRegistry({"WRONG": nq})


def test_registry_direct_construction_rejects_non_mapping():
    with pytest.raises(FuturesConfigurationError):
        FuturesInstrumentRegistry(["NQ"])


def test_registry_direct_construction_rejects_empty_key():
    nq = _nq_instrument()
    with pytest.raises(FuturesConfigurationError):
        FuturesInstrumentRegistry({"": nq})


def test_registry_direct_construction_valid_generic_registry_still_works():
    """The registry must remain usable for a generic, non-NQ/MNQ
    instrument -- Olive's production 'only NQ/MNQ' policy is enforced
    separately by app.health, not by this class."""
    nq = _nq_instrument()
    registry = FuturesInstrumentRegistry({"NQ": nq})

    assert registry.contains("NQ")
    assert registry.get("nq").root_symbol == "NQ"
    assert len(registry) == 1


# -- Registry lookup must never leak AttributeError for non-string input ----


@pytest.mark.parametrize("bad_value", ["", "   ", None, 123, True])
def test_registry_get_never_leaks_attribute_error(bad_value):
    registry = load_instrument_registry()
    with pytest.raises(UnknownInstrumentError):
        registry.get(bad_value)


@pytest.mark.parametrize("bad_value", ["", "   ", None, 123, True])
def test_registry_contains_never_leaks_attribute_error(bad_value):
    registry = load_instrument_registry()
    # "" and "   " are real (if unknown) strings -- they return False.
    # None/123/True are not strings at all -- contains() returns False
    # for these too, by design (see FuturesInstrumentRegistry.contains).
    assert registry.contains(bad_value) is False


# ============================================================================
# Phase 2.3 adversarial regression tests (third external audit findings)
# ============================================================================

# -- JSON contract-month must be a true integer -------------------------------


def test_json_contract_month_float_rejected(tmp_path):
    """The exact third-audit bug: ContractMonth(3.0) succeeds (Python's
    IntEnum lookup treats a float equal to a member's value as a match),
    so a JSON config with a float contract month like 3.0 previously slid
    through as if it were the true int 3."""
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(contract_months=[3.0, 6, 9, 12])]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


@pytest.mark.parametrize("bad_month", [True, False, "3", None, [], {}, 3.5])
def test_json_contract_month_rejects_non_int_types(tmp_path, bad_month):
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(contract_months=[bad_month, 6, 9, 12])]})

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(path)


def test_json_contract_month_still_accepts_true_integers(tmp_path):
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry(contract_months=[3, 6, 9, 12])]})

    registry = load_instrument_registry(path)

    assert registry.get("NQ").contract_months


# -- load_instrument_registry config_path hardening ---------------------------


def test_load_instrument_registry_accepts_str_path(tmp_path):
    """The exact third-audit bug: a plain str path previously leaked a raw
    AttributeError the first time .exists() was called on it."""
    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry()]})

    registry = load_instrument_registry(str(path))

    assert registry.contains("NQ")


def test_load_instrument_registry_str_path_missing_file_raises_domain_error(tmp_path):
    missing = str(tmp_path / "does-not-exist.json")

    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(missing)


def test_load_instrument_registry_accepts_os_pathlike(tmp_path):
    import os

    path = _write_config(tmp_path, {"instruments": [_valid_nq_entry()]})

    class _PathLike(os.PathLike):
        def __init__(self, inner: Path):
            self._inner = inner

        def __fspath__(self) -> str:
            return str(self._inner)

    registry = load_instrument_registry(_PathLike(path))

    assert registry.contains("NQ")


@pytest.mark.parametrize("bad_value", [123, True, 1.5, object()])
def test_load_instrument_registry_rejects_non_path_like_type(bad_value):
    """A wrong TYPE entirely (not even a potential path) must raise a
    domain error, never a raw AttributeError/TypeError."""
    with pytest.raises(FuturesConfigurationError):
        load_instrument_registry(bad_value)


def test_load_instrument_registry_none_still_means_default_path():
    registry = load_instrument_registry(None)

    assert registry.contains("NQ")
    assert registry.contains("MNQ")
