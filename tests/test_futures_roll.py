"""Tests for app.futures.roll: customary calendar-based lead-contract resolution.

Fixed 2026 dates throughout (source-backed roll dates: Mar 16, Jun 15,
Sep 14, Dec 14 -- see config/calendar/cme_equity_index_cycle_dates.json).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.futures.calendar import CycleDateError, load_cycle_date_calendar
from app.futures.models import InvalidDateError, InvalidInstrumentError
from app.futures.registry import load_instrument_registry
from app.futures.roll import (
    SessionClosedError,
    calendar_lead_contract,
    calendar_lead_contract_at,
    is_post_roll,
    nearest_unexpired_contract,
)

CHICAGO_TZ = ZoneInfo("America/Chicago")


@pytest.fixture(scope="module")
def registry():
    return load_instrument_registry()


@pytest.fixture(scope="module")
def cycle_calendar():
    return load_cycle_date_calendar()


@pytest.fixture
def nq(registry):
    return registry.get("NQ")


@pytest.fixture
def mnq(registry):
    return registry.get("MNQ")


# -- nearest_unexpired_contract ------------------------------------------------------


def test_nearest_unexpired_contract_mid_quarter(nq, cycle_calendar):
    # August 13, 2026: June contract (expired 2026-06-18) is gone;
    # September (expires 2026-09-18) is nearest unexpired.
    result = nearest_unexpired_contract(nq, date(2026, 8, 13), cycle_calendar)

    assert result.display_code == "NQU6"


def test_nearest_unexpired_contract_on_expiration_date(nq, cycle_calendar):
    # On the expiration date itself, that contract is still "nearest unexpired"
    # from a pure date standpoint (session-level cutoff is handled separately
    # in app.futures.sessions, not here).
    result = nearest_unexpired_contract(nq, date(2026, 9, 18), cycle_calendar)

    assert result.display_code == "NQU6"


def test_nearest_unexpired_contract_day_after_expiration(nq, cycle_calendar):
    result = nearest_unexpired_contract(nq, date(2026, 9, 19), cycle_calendar)

    assert result.display_code == "NQZ6"


# -- is_post_roll ---------------------------------------------------------------------


def test_is_post_roll_boundary(nq, cycle_calendar):
    cycle = cycle_calendar.get(2026, 9)

    assert is_post_roll(date(2026, 9, 13), cycle) is False
    assert is_post_roll(date(2026, 9, 14), cycle) is True
    assert is_post_roll(date(2026, 9, 15), cycle) is True


# -- calendar_lead_contract: September 2026 roll (source-backed roll = Sep 14) --------


def test_calendar_lead_before_september_roll(nq):
    cal = load_cycle_date_calendar()

    lead = calendar_lead_contract(nq, date(2026, 9, 13), cal)

    assert lead.display_code == "NQU6"


def test_calendar_lead_on_september_roll_date(nq):
    cal = load_cycle_date_calendar()

    lead = calendar_lead_contract(nq, date(2026, 9, 14), cal)

    assert lead.display_code == "NQZ6"


def test_calendar_lead_after_september_roll(nq):
    cal = load_cycle_date_calendar()

    lead = calendar_lead_contract(nq, date(2026, 9, 15), cal)

    assert lead.display_code == "NQZ6"


def test_calendar_lead_mnq_mirrors_nq_september_roll(mnq):
    cal = load_cycle_date_calendar()

    before = calendar_lead_contract(mnq, date(2026, 9, 13), cal)
    after = calendar_lead_contract(mnq, date(2026, 9, 14), cal)

    assert before.display_code == "MNQU6"
    assert after.display_code == "MNQZ6"


# -- December -> March year-end roll ----------------------------------------------------


def test_calendar_lead_december_to_march_year_end_roll(nq):
    cal = load_cycle_date_calendar()

    before = calendar_lead_contract(nq, date(2026, 12, 13), cal)
    at_roll = calendar_lead_contract(nq, date(2026, 12, 14), cal)

    assert before.display_code == "NQZ6"
    assert at_roll.display_code == "NQH7"
    assert at_roll.year == 2027


def test_calendar_lead_mnq_december_to_march_year_end_roll(mnq):
    cal = load_cycle_date_calendar()

    at_roll = calendar_lead_contract(mnq, date(2026, 12, 14), cal)

    assert at_roll.display_code == "MNQH7"


# -- calendar_lead_contract_at: timestamp-based convenience wrapper -------------------


def test_calendar_lead_at_timestamp_pre_roll(nq):
    cal = load_cycle_date_calendar()
    # Sunday 2026-09-13 at 18:00 CT -> trade date Monday 2026-09-14 -> post-roll already.
    # Use a weekday daytime timestamp instead for an unambiguous pre-roll trade date.
    ts = datetime(2026, 9, 11, 10, 0, tzinfo=CHICAGO_TZ)  # Friday 2026-09-11, 10:00 CT

    lead = calendar_lead_contract_at(nq, ts, cal)

    assert lead.display_code == "NQU6"


def test_calendar_lead_at_timestamp_post_roll(nq):
    cal = load_cycle_date_calendar()
    ts = datetime(2026, 9, 14, 10, 0, tzinfo=CHICAGO_TZ)  # Monday 2026-09-14, 10:00 CT

    lead = calendar_lead_contract_at(nq, ts, cal)

    assert lead.display_code == "NQZ6"


def test_calendar_lead_at_timestamp_evening_session_rolls_trade_date(nq):
    cal = load_cycle_date_calendar()
    # Sunday 2026-09-13, 18:00 CT -> trade date is Monday 2026-09-14 -> post-roll.
    ts = datetime(2026, 9, 13, 18, 0, tzinfo=CHICAGO_TZ)

    lead = calendar_lead_contract_at(nq, ts, cal)

    assert lead.display_code == "NQZ6"


def test_calendar_lead_at_raises_during_maintenance(nq):
    cal = load_cycle_date_calendar()
    # Tuesday 2026-09-15, 16:30 CT -> maintenance window, no trade date.
    ts = datetime(2026, 9, 15, 16, 30, tzinfo=CHICAGO_TZ)

    with pytest.raises(SessionClosedError):
        calendar_lead_contract_at(nq, ts, cal)


def test_calendar_lead_at_raises_on_weekend(nq):
    cal = load_cycle_date_calendar()
    # Saturday -- always weekend closed.
    ts = datetime(2026, 9, 12, 12, 0, tzinfo=CHICAGO_TZ)

    with pytest.raises(SessionClosedError):
        calendar_lead_contract_at(nq, ts, cal)


# ============================================================================
# Phase 2.2 adversarial regression tests (second external audit findings)
# ============================================================================


def test_nearest_unexpired_contract_rejects_string_date(nq, cycle_calendar):
    with pytest.raises(InvalidDateError):
        nearest_unexpired_contract(nq, "2026-09-14", cycle_calendar)


def test_nearest_unexpired_contract_rejects_datetime_input(nq, cycle_calendar):
    """A datetime is deliberately rejected (not silently truncated to a
    date) for this date-only parameter -- callers should pass a plain
    date, not a timestamp."""
    with pytest.raises(InvalidDateError):
        nearest_unexpired_contract(nq, datetime(2026, 9, 14, 10, 0, tzinfo=CHICAGO_TZ), cycle_calendar)


def test_calendar_lead_contract_rejects_string_date(nq, cycle_calendar):
    with pytest.raises(InvalidDateError):
        calendar_lead_contract(nq, "2026-09-14", cycle_calendar)


def test_is_post_roll_rejects_string_date(cycle_calendar):
    cycle = cycle_calendar.get(2026, 9)

    with pytest.raises(InvalidDateError):
        is_post_roll("2026-09-14", cycle)


def test_is_post_roll_rejects_datetime_input(cycle_calendar):
    cycle = cycle_calendar.get(2026, 9)

    with pytest.raises(InvalidDateError):
        is_post_roll(datetime(2026, 9, 14, 10, 0, tzinfo=CHICAGO_TZ), cycle)


def test_is_post_roll_still_accepts_plain_date(cycle_calendar):
    cycle = cycle_calendar.get(2026, 9)

    assert is_post_roll(date(2026, 9, 14), cycle) is True
    assert is_post_roll(date(2026, 9, 13), cycle) is False


# ============================================================================
# Phase 2.3 adversarial regression tests (third external audit findings)
# ============================================================================
#
# Every public function in this module must validate its
# instrument/cycle_calendar/cycle parameters up front -- passing the
# wrong object type must never leak a raw AttributeError.


def test_nearest_unexpired_contract_rejects_non_instrument(cycle_calendar):
    with pytest.raises(InvalidInstrumentError):
        nearest_unexpired_contract("bad", date(2026, 9, 1), cycle_calendar)


def test_nearest_unexpired_contract_rejects_non_calendar(nq):
    with pytest.raises(CycleDateError):
        nearest_unexpired_contract(nq, date(2026, 9, 1), "bad")


def test_is_post_roll_rejects_non_cycle_dates():
    with pytest.raises(CycleDateError):
        is_post_roll(date(2026, 9, 14), "bad")


def test_calendar_lead_contract_rejects_non_instrument(cycle_calendar):
    with pytest.raises(InvalidInstrumentError):
        calendar_lead_contract("bad", date(2026, 9, 1), cycle_calendar)


def test_calendar_lead_contract_rejects_non_calendar(nq):
    with pytest.raises(CycleDateError):
        calendar_lead_contract(nq, date(2026, 9, 1), "bad")


def test_calendar_lead_contract_at_rejects_non_instrument(cycle_calendar):
    ts = datetime(2026, 9, 11, 10, 0, tzinfo=CHICAGO_TZ)
    with pytest.raises(InvalidInstrumentError):
        calendar_lead_contract_at("bad", ts, cycle_calendar)


def test_calendar_lead_contract_at_rejects_non_calendar(nq):
    ts = datetime(2026, 9, 11, 10, 0, tzinfo=CHICAGO_TZ)
    with pytest.raises(CycleDateError):
        calendar_lead_contract_at(nq, ts, "bad")


def test_roll_functions_never_leak_attribute_error_for_wrong_types(nq, cycle_calendar):
    for fn, args in (
        (nearest_unexpired_contract, (None, date(2026, 9, 1), cycle_calendar)),
        (nearest_unexpired_contract, (nq, date(2026, 9, 1), None)),
        (is_post_roll, (date(2026, 9, 1), None)),
        (calendar_lead_contract, (None, date(2026, 9, 1), cycle_calendar)),
        (calendar_lead_contract, (nq, date(2026, 9, 1), None)),
    ):
        try:
            fn(*args)
        except (InvalidInstrumentError, CycleDateError, InvalidDateError):
            pass
        except AttributeError:
            pytest.fail(f"{fn.__name__} leaked a raw AttributeError for a bad object type")
