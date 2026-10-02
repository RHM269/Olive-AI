"""Tests for app.futures.calendar: expiration/roll calendar and provenance.

All dates are fixed, hard-coded calendar dates (never datetime.now()) so
these tests remain deterministic indefinitely. Weekday assertions were
independently verified against Python's own date arithmetic before
being written here.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.futures.calendar import (
    DEFAULT_CYCLE_DATE_CONFIG_PATH,
    CycleDateCalendar,
    _require_cycle_calendar,
    final_trading_timestamp,
    load_cycle_date_calendar,
    nominal_customary_roll,
    nominal_third_friday,
)
from app.futures.models import ContractCycleDates, CycleDateError, CycleDateSource, FuturesConfigurationError, InvalidDateError


# -- Nominal third-Friday calculation ----------------------------------------------


@pytest.mark.parametrize(
    "year,month,expected",
    [
        (2026, 9, date(2026, 9, 18)),
        (2026, 12, date(2026, 12, 18)),
        (2026, 6, date(2026, 6, 19)),  # the nominal date -- NOT the official one, see below
        (2027, 3, date(2027, 3, 19)),
    ],
)
def test_nominal_third_friday(year, month, expected):
    result = nominal_third_friday(year, month)

    assert result == expected
    assert result.weekday() == 4  # Friday


def test_nominal_customary_roll_is_monday_of_same_week():
    third_friday = date(2026, 9, 18)

    roll = nominal_customary_roll(third_friday)

    assert roll == date(2026, 9, 14)
    assert roll.weekday() == 0  # Monday


# -- Official vs. calculated-nominal provenance ------------------------------------


def test_default_config_path_exists():
    assert DEFAULT_CYCLE_DATE_CONFIG_PATH.exists()


def test_real_calendar_loads():
    cal = load_cycle_date_calendar()

    assert cal.as_of
    assert 2026 in cal.covered_years


def test_official_date_used_when_available():
    cal = load_cycle_date_calendar()

    cycle = cal.get(2026, 9)

    assert cycle.source is CycleDateSource.OFFICIAL
    assert cycle.expiration == date(2026, 9, 18)
    assert cycle.roll == date(2026, 9, 14)


def test_june_2026_holiday_exception():
    """The headline Phase 2 correctness requirement: nominal != official for June 2026."""
    cal = load_cycle_date_calendar()

    nominal = nominal_third_friday(2026, 6)
    official = cal.get(2026, 6)

    assert nominal == date(2026, 6, 19)
    assert official.expiration == date(2026, 6, 18)
    assert official.source is CycleDateSource.OFFICIAL
    assert nominal != official.expiration


def test_has_official_reports_coverage_accurately():
    cal = load_cycle_date_calendar()

    assert cal.has_official(2026, 9) is True
    assert cal.has_official(2031, 9) is False


def test_fallback_to_calculated_nominal_outside_official_table():
    cal = load_cycle_date_calendar()

    # 2031 is well outside the source-backed table (2025-2028).
    cycle = cal.get(2031, 9)

    assert cycle.source is CycleDateSource.CALCULATED_NOMINAL
    assert cycle.expiration == nominal_third_friday(2031, 9)
    assert cycle.roll == nominal_customary_roll(cycle.expiration)


def test_all_official_years_present():
    cal = load_cycle_date_calendar()

    for year in (2025, 2026, 2027, 2028):
        for month in (3, 6, 9, 12):
            cycle = cal.get(year, month)
            assert cycle.source is CycleDateSource.OFFICIAL, f"{year}-{month:02d} should be official"


# -- Final trading timestamp --------------------------------------------------------


def test_final_trading_timestamp_is_timezone_aware_0830_central():
    ts = final_trading_timestamp(date(2026, 9, 18))

    assert ts.tzinfo is not None
    assert ts.hour == 8
    assert ts.minute == 30
    assert ts.year == 2026 and ts.month == 9 and ts.day == 18
    assert str(ts.tzinfo) == "America/Chicago"


def test_final_trading_timestamp_never_naive():
    ts = final_trading_timestamp(date(2026, 12, 18))

    assert ts.utcoffset() is not None


# -- Malformed configuration handling (test-only tmp_path files) -------------------


def _write_calendar(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "cycle_dates.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _valid_payload(**overrides) -> dict:
    payload = {
        "source": "test fixture",
        "as_of": "2026-01-01",
        "covered_years": [2026],
        # Phase 2.1: `covered_years` must have full quarterly (3/6/9/12)
        # OFFICIAL coverage for every year it claims, so the "valid" base
        # fixture needs all four 2026 quarters, not just September.
        "dates": [
            {"year": 2026, "month": 3, "expiration": "2026-03-20", "roll": "2026-03-16"},
            {"year": 2026, "month": 6, "expiration": "2026-06-18", "roll": "2026-06-15"},
            {"year": 2026, "month": 9, "expiration": "2026-09-18", "roll": "2026-09-14"},
            {"year": 2026, "month": 12, "expiration": "2026-12-18", "roll": "2026-12-14"},
        ],
    }
    payload.update(overrides)
    return payload


def test_missing_calendar_file_raises(tmp_path):
    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(tmp_path / "missing.json")


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_missing_as_of_rejected(tmp_path):
    payload = _valid_payload()
    del payload["as_of"]
    path = _write_calendar(tmp_path, payload)

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_empty_dates_rejected(tmp_path):
    path = _write_calendar(tmp_path, _valid_payload(dates=[]))

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_roll_after_expiration_rejected(tmp_path):
    payload = _valid_payload(
        dates=[{"year": 2026, "month": 9, "expiration": "2026-09-14", "roll": "2026-09-18"}]
    )
    path = _write_calendar(tmp_path, payload)

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_duplicate_cycle_entry_rejected(tmp_path):
    entry = {"year": 2026, "month": 9, "expiration": "2026-09-18", "roll": "2026-09-14"}
    path = _write_calendar(tmp_path, _valid_payload(dates=[entry, dict(entry)]))

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_unsupported_month_rejected(tmp_path):
    payload = _valid_payload(
        dates=[{"year": 2026, "month": 7, "expiration": "2026-07-18", "roll": "2026-07-14"}]
    )
    path = _write_calendar(tmp_path, payload)

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_invalid_date_string_rejected(tmp_path):
    payload = _valid_payload(
        dates=[{"year": 2026, "month": 9, "expiration": "not-a-date", "roll": "2026-09-14"}]
    )
    path = _write_calendar(tmp_path, payload)

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_valid_custom_calendar_loads(tmp_path):
    path = _write_calendar(tmp_path, _valid_payload())

    cal = load_cycle_date_calendar(path)

    cycle = cal.get(2026, 9)
    assert cycle.source is CycleDateSource.OFFICIAL
    assert cycle.expiration == date(2026, 9, 18)


# ============================================================================
# Phase 2.1 adversarial regression tests (external review findings)
# ============================================================================


@pytest.mark.parametrize("bad_month", [1, 2, 4, 5, 7, 8, 10, 11])
def test_calendar_get_rejects_non_quarterly_month(bad_month):
    """CycleDateCalendar.get() must explicitly reject January and every
    other non-quarterly month -- NQ/MNQ only ever have March/June/
    September/December contracts, so a month like January is not merely
    "uncovered", it is domain-invalid and must fail closed with
    CycleDateError rather than silently falling back to a calculated
    nominal date for a contract that can never exist.
    """
    cal = load_cycle_date_calendar()

    with pytest.raises(CycleDateError):
        cal.get(2026, bad_month)


@pytest.mark.parametrize("bad_month", [1, 7])
def test_calendar_has_official_rejects_non_quarterly_month(bad_month):
    cal = load_cycle_date_calendar()

    with pytest.raises(CycleDateError):
        cal.has_official(2026, bad_month)


def test_malformed_as_of_rejected(tmp_path):
    """A calendar file whose 'as_of' is not a real ISO date (not merely
    missing) must fail closed rather than being stored as an opaque,
    unvalidated string that later code might trust.
    """
    payload = _valid_payload(as_of="banana")
    path = _write_calendar(tmp_path, payload)

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_covered_years_with_incomplete_quarterly_coverage_rejected(tmp_path):
    """'covered_years' claiming a year must be backed by OFFICIAL entries
    for all four quarters (3, 6, 9, 12) of that year -- a calendar that
    claims 2026 coverage but only supplies September must not load, since
    it would silently misrepresent March/June/December as "covered" when
    they are not actually present.
    """
    payload = _valid_payload(
        covered_years=[2026],
        dates=[{"year": 2026, "month": 9, "expiration": "2026-09-18", "roll": "2026-09-14"}],
    )
    path = _write_calendar(tmp_path, payload)

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_official_entry_year_not_in_covered_years_rejected(tmp_path):
    """An OFFICIAL date entry for a year that 'covered_years' never
    declares must be rejected -- declared coverage must not omit actual
    authoritative data, since downstream code may rely on
    'covered_years' alone to decide whether official data exists.
    """
    payload = _valid_payload(
        covered_years=[2026],
        dates=[
            {"year": 2026, "month": 3, "expiration": "2026-03-20", "roll": "2026-03-16"},
            {"year": 2026, "month": 6, "expiration": "2026-06-18", "roll": "2026-06-15"},
            {"year": 2026, "month": 9, "expiration": "2026-09-18", "roll": "2026-09-14"},
            {"year": 2026, "month": 12, "expiration": "2026-12-18", "roll": "2026-12-14"},
            # 2027 September entry present, but 2027 is not in covered_years.
            {"year": 2027, "month": 9, "expiration": "2027-09-17", "roll": "2027-09-13"},
        ],
    )
    path = _write_calendar(tmp_path, payload)

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


def test_official_entry_month_year_mismatch_rejected(tmp_path):
    """The exact adversarial mismatch from the external review: an entry
    whose declared (year, month) does not match its own expiration date
    (e.g. tagged as September but dated in June) must be rejected rather
    than silently filed under the wrong cycle key.
    """
    payload = _valid_payload(
        dates=[
            {"year": 2026, "month": 9, "expiration": "2026-06-18", "roll": "2026-06-15"},
        ]
    )
    path = _write_calendar(tmp_path, payload)

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(path)


# ============================================================================
# Phase 2.2 adversarial regression tests (second external audit findings)
# ============================================================================

# -- final_trading_timestamp input validation --------------------------------


def test_final_trading_timestamp_rejects_string():
    with pytest.raises(InvalidDateError):
        final_trading_timestamp("2026-09-18")


def test_final_trading_timestamp_rejects_none():
    with pytest.raises(InvalidDateError):
        final_trading_timestamp(None)


# -- Calendar year input validation (get / has_official / nominal_third_friday) --


def test_calendar_get_rejects_bool_year():
    cal = load_cycle_date_calendar()
    with pytest.raises(FuturesConfigurationError):
        cal.get(True, 3)


def test_calendar_get_rejects_string_year():
    cal = load_cycle_date_calendar()
    with pytest.raises(FuturesConfigurationError):
        cal.get("2026", 3)


def test_calendar_has_official_rejects_bool_year():
    cal = load_cycle_date_calendar()
    with pytest.raises(FuturesConfigurationError):
        cal.has_official(True, 3)


# -- Direct CycleDateCalendar construction hardening -------------------------


def _official_cycle(year, month, expiration, roll):
    return ContractCycleDates(
        year=year, month=month, expiration=expiration, roll=roll, source=CycleDateSource.OFFICIAL
    )


def _full_2026_official():
    """A complete, mutually-consistent 2026 OFFICIAL quarterly set, keyed
    correctly -- the known-good baseline for the direct-construction
    tests below, each of which corrupts exactly one aspect of it."""
    return {
        (2026, 3): _official_cycle(2026, 3, date(2026, 3, 20), date(2026, 3, 16)),
        (2026, 6): _official_cycle(2026, 6, date(2026, 6, 18), date(2026, 6, 15)),
        (2026, 9): _official_cycle(2026, 9, date(2026, 9, 18), date(2026, 9, 14)),
        (2026, 12): _official_cycle(2026, 12, date(2026, 12, 18), date(2026, 12, 14)),
    }


def test_direct_construction_accepts_valid_calendar():
    cal = CycleDateCalendar(
        official_dates=_full_2026_official(),
        as_of="2026-10-01",
        covered_years=(2026,),
        source="test fixture",
    )

    cycle = cal.get(2026, 9)
    assert cycle.source is CycleDateSource.OFFICIAL
    assert cycle.expiration == date(2026, 9, 18)


def test_direct_construction_rejects_invalid_as_of():
    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=_full_2026_official(),
            as_of="banana",
            covered_years=(2026,),
            source="test fixture",
        )


def test_direct_construction_rejects_empty_source():
    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=_full_2026_official(),
            as_of="2026-10-01",
            covered_years=(2026,),
            source="",
        )


def test_direct_construction_rejects_out_of_range_covered_years():
    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=_full_2026_official(),
            as_of="2026-10-01",
            covered_years=(999,),
            source="test fixture",
        )


def test_direct_construction_rejects_missing_quarterly_coverage():
    official = _full_2026_official()
    del official[(2026, 3)]  # only 3 of the 4 required quarters remain

    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=official,
            as_of="2026-10-01",
            covered_years=(2026,),
            source="test fixture",
        )


def test_direct_construction_rejects_official_key_year_mismatch():
    """The exact second-audit example: a dict key of (2026, 9) whose
    *value* actually holds December's dates."""
    official = _full_2026_official()
    official[(2026, 9)] = _official_cycle(2026, 12, date(2026, 12, 18), date(2026, 12, 14))

    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=official,
            as_of="2026-10-01",
            covered_years=(2026,),
            source="test fixture",
        )


def test_direct_construction_rejects_official_key_month_mismatch():
    """Key says September, but the value's own declared month is June."""
    official = _full_2026_official()
    official[(2026, 9)] = _official_cycle(2026, 6, date(2026, 6, 18), date(2026, 6, 15))

    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=official,
            as_of="2026-10-01",
            covered_years=(2026,),
            source="test fixture",
        )


def test_direct_construction_rejects_non_contract_cycle_dates_value():
    official = _full_2026_official()
    official[(2026, 3)] = "not-a-contract-cycle-dates"

    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=official,
            as_of="2026-10-01",
            covered_years=(2026,),
            source="test fixture",
        )


def test_direct_construction_rejects_non_official_source_value():
    """A CALCULATED_NOMINAL value has no business being stored in the
    official_dates mapping -- that mapping represents source-backed data
    only."""
    official = _full_2026_official()
    official[(2026, 3)] = ContractCycleDates(
        year=2026, month=3, expiration=date(2026, 3, 20), roll=date(2026, 3, 16),
        source=CycleDateSource.CALCULATED_NOMINAL,
    )

    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=official,
            as_of="2026-10-01",
            covered_years=(2026,),
            source="test fixture",
        )


def test_direct_construction_rejects_official_year_outside_covered_years():
    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates=_full_2026_official(),
            as_of="2026-10-01",
            covered_years=(2025,),  # 2026 data exists but isn't declared covered
            source="test fixture",
        )


def test_direct_construction_rejects_non_mapping_official_dates():
    with pytest.raises(CycleDateError):
        CycleDateCalendar(
            official_dates="not-a-mapping",
            as_of="2026-10-01",
            covered_years=(2026,),
            source="test fixture",
        )


# -- CycleDateCalendar internal immutability ---------------------------------


def test_calendar_official_dates_not_mutable_through_caller_reference():
    """Mutating the dict the caller originally passed in (or the
    calendar's own internal mapping) after construction must not change
    what the calendar actually returns -- it stores an immutable copy,
    not the caller's live dict."""
    official = _full_2026_official()
    cal = CycleDateCalendar(
        official_dates=official,
        as_of="2026-10-01",
        covered_years=(2026,),
        source="test fixture",
    )

    # Mutate the caller's original dict after construction (no
    # reconstruction happens here, so this is just a plain dict write --
    # it must not be visible to the already-built `cal`).
    december_cycle = official[(2026, 12)]
    official[(2026, 9)] = december_cycle
    assert cal.get(2026, 9).expiration == date(2026, 9, 18), "calendar truth changed via caller's dict"

    # The calendar's own internal mapping must also reject direct mutation.
    with pytest.raises(TypeError):
        cal._official[(2026, 9)] = december_cycle


# ============================================================================
# Phase 2.3 adversarial regression tests (third external audit findings)
# ============================================================================

# -- load_cycle_date_calendar config_path hardening ---------------------------


def test_load_cycle_date_calendar_accepts_str_path(tmp_path):
    """The exact third-audit bug: a plain str path previously leaked a raw
    AttributeError the first time .exists() was called on it."""
    path = _write_calendar(tmp_path, _valid_payload())

    cal = load_cycle_date_calendar(str(path))

    assert cal.get(2026, 9).expiration == date(2026, 9, 18)


def test_load_cycle_date_calendar_str_path_missing_file_raises_domain_error(tmp_path):
    missing = str(tmp_path / "does-not-exist.json")

    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(missing)


def test_load_cycle_date_calendar_accepts_os_pathlike(tmp_path):
    import os

    path = _write_calendar(tmp_path, _valid_payload())

    class _PathLike(os.PathLike):
        def __init__(self, inner: Path):
            self._inner = inner

        def __fspath__(self) -> str:
            return str(self._inner)

    cal = load_cycle_date_calendar(_PathLike(path))

    assert cal.get(2026, 9).expiration == date(2026, 9, 18)


@pytest.mark.parametrize("bad_value", [123, True, 1.5, object()])
def test_load_cycle_date_calendar_rejects_non_path_like_type(bad_value):
    """A wrong TYPE entirely (not even a potential path) must raise a
    domain error, never a raw AttributeError/TypeError."""
    with pytest.raises(CycleDateError):
        load_cycle_date_calendar(bad_value)


def test_load_cycle_date_calendar_none_still_means_default_path():
    cal = load_cycle_date_calendar(None)

    assert cal.get(2026, 9).expiration == date(2026, 9, 18)


# -- _require_cycle_calendar shared validator ----------------------------------


def test_require_cycle_calendar_accepts_cycle_date_calendar():
    cal = load_cycle_date_calendar()
    assert _require_cycle_calendar(cal, context="test") is cal


@pytest.mark.parametrize("bad_value", [None, "calendar", 123, object()])
def test_require_cycle_calendar_rejects_non_calendar(bad_value):
    with pytest.raises(CycleDateError):
        _require_cycle_calendar(bad_value, context="test")
