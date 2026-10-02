"""Tests for app.futures.sessions: regular Globex schedule, trade-date
mapping, timezone/DST handling, and contract-lifecycle (expiration) state.

All timestamps are fixed and explicit (never datetime.now()). The
representative week used throughout is 2026-09-14 (Monday) through
2026-09-20 (Sunday), which conveniently also matches the source-backed
September 2026 roll (Sep 14) and expiration (Sep 18) dates used in
test_futures_roll.py and test_futures_calendar.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.futures.calendar import CycleDateError, load_cycle_date_calendar
from app.futures.models import ContractMonth, InvalidContractError, InvalidDatetimeError, NaiveDatetimeError
from app.futures.registry import load_instrument_registry
from app.futures.sessions import (
    ContractLifecycleState,
    SessionState,
    TradeDateResult,
    contract_state_at,
    is_contract_within_trading_life,
    session_state_at,
    trade_date_for,
)

CHICAGO_TZ = ZoneInfo("America/Chicago")


def _ct(year, month, day, hour, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=CHICAGO_TZ)


@pytest.fixture(scope="module")
def cycle_calendar():
    return load_cycle_date_calendar()


@pytest.fixture(scope="module")
def nq():
    return load_instrument_registry().get("NQ")


# -- Weekday maintenance boundary (Monday-Thursday, 16:00-17:00 CT) -----------------


@pytest.mark.parametrize("day", [14, 15, 16, 17])  # Mon, Tue, Wed, Thu of the reference week
def test_weekday_pre_maintenance_is_open(day):
    assert session_state_at(_ct(2026, 9, day, 15, 59, 59)) is SessionState.OPEN


@pytest.mark.parametrize("day", [14, 15, 16, 17])
def test_weekday_maintenance_start_boundary(day):
    assert session_state_at(_ct(2026, 9, day, 16, 0, 0)) is SessionState.MAINTENANCE


@pytest.mark.parametrize("day", [14, 15, 16, 17])
def test_weekday_maintenance_end_boundary(day):
    assert session_state_at(_ct(2026, 9, day, 16, 59, 59)) is SessionState.MAINTENANCE
    assert session_state_at(_ct(2026, 9, day, 17, 0, 0)) is SessionState.OPEN


# -- Friday close boundary ----------------------------------------------------------


def test_friday_pre_close_is_open():
    assert session_state_at(_ct(2026, 9, 18, 15, 59, 59)) is SessionState.OPEN


def test_friday_close_boundary():
    assert session_state_at(_ct(2026, 9, 18, 16, 0, 0)) is SessionState.WEEKEND_CLOSED


def test_friday_evening_is_weekend_closed():
    assert session_state_at(_ct(2026, 9, 18, 20, 0, 0)) is SessionState.WEEKEND_CLOSED


# -- Saturday ------------------------------------------------------------------------


def test_saturday_always_closed():
    assert session_state_at(_ct(2026, 9, 19, 0, 0, 0)) is SessionState.WEEKEND_CLOSED
    assert session_state_at(_ct(2026, 9, 19, 12, 0, 0)) is SessionState.WEEKEND_CLOSED
    assert session_state_at(_ct(2026, 9, 19, 23, 59, 59)) is SessionState.WEEKEND_CLOSED


# -- Sunday reopen boundary ----------------------------------------------------------


def test_sunday_pre_open_is_weekend_closed():
    assert session_state_at(_ct(2026, 9, 20, 16, 59, 59)) is SessionState.WEEKEND_CLOSED


def test_sunday_open_boundary():
    assert session_state_at(_ct(2026, 9, 20, 17, 0, 0)) is SessionState.OPEN


def test_sunday_late_evening_is_open():
    assert session_state_at(_ct(2026, 9, 20, 23, 0, 0)) is SessionState.OPEN


# -- Naive datetime rejection ---------------------------------------------------------


def test_naive_datetime_rejected_by_session_state_at():
    naive = datetime(2026, 9, 14, 10, 0, 0)

    with pytest.raises(NaiveDatetimeError):
        session_state_at(naive)


def test_naive_datetime_rejected_by_trade_date_for():
    naive = datetime(2026, 9, 14, 10, 0, 0)

    with pytest.raises(NaiveDatetimeError):
        trade_date_for(naive)


# -- Phase 2.2: non-datetime input must never leak AttributeError ---------------------


def test_non_datetime_rejected_by_session_state_at():
    with pytest.raises(InvalidDatetimeError):
        session_state_at("2026-09-14")


def test_non_datetime_rejected_by_trade_date_for():
    with pytest.raises(InvalidDatetimeError):
        trade_date_for("2026-09-14")


def test_none_rejected_by_session_state_at():
    with pytest.raises(InvalidDatetimeError):
        session_state_at(None)


def test_naive_datetime_error_is_also_an_invalid_datetime_error():
    """NaiveDatetimeError is the narrower case (a real datetime that's
    merely missing tzinfo) -- it should still be catchable as the
    broader InvalidDatetimeError."""
    naive = datetime(2026, 9, 14, 10, 0, 0)

    with pytest.raises(InvalidDatetimeError):
        session_state_at(naive)


def test_utc_input_is_accepted_and_converted():
    # 2026-09-14 20:00 UTC == 2026-09-14 15:00 CDT (UTC-5 in September).
    utc_ts = datetime(2026, 9, 14, 20, 0, 0, tzinfo=timezone.utc)

    assert session_state_at(utc_ts) is SessionState.OPEN


# -- Trade date mapping ----------------------------------------------------------------


def test_trade_date_sunday_evening_maps_to_monday():
    result = trade_date_for(_ct(2026, 9, 13, 18, 0, 0))  # Sunday 18:00 CT

    assert result == TradeDateResult(trade_date=_date(2026, 9, 14), session_state=SessionState.OPEN)


def test_trade_date_monday_morning_maps_to_monday():
    result = trade_date_for(_ct(2026, 9, 14, 10, 0, 0))

    assert result.trade_date == _date(2026, 9, 14)
    assert result.session_state is SessionState.OPEN


def test_trade_date_monday_evening_maps_to_tuesday():
    result = trade_date_for(_ct(2026, 9, 14, 18, 0, 0))

    assert result.trade_date == _date(2026, 9, 15)


def test_trade_date_during_maintenance_is_none():
    result = trade_date_for(_ct(2026, 9, 15, 16, 30, 0))

    assert result.trade_date is None
    assert result.session_state is SessionState.MAINTENANCE


def test_trade_date_during_weekend_is_none():
    result = trade_date_for(_ct(2026, 9, 19, 12, 0, 0))

    assert result.trade_date is None
    assert result.session_state is SessionState.WEEKEND_CLOSED


def _date(year, month, day):
    from datetime import date

    return date(year, month, day)


# -- DST transition robustness (2026: spring forward Mar 8, fall back Nov 1) -----------


def test_session_state_correct_across_spring_forward_sunday():
    # 2026-03-08 is a Sunday and the U.S. spring-forward DST transition day.
    before_open = _ct(2026, 3, 8, 16, 59, 59)
    at_open = _ct(2026, 3, 8, 17, 0, 0)

    assert session_state_at(before_open) is SessionState.WEEKEND_CLOSED
    assert session_state_at(at_open) is SessionState.OPEN


def test_session_state_correct_across_fall_back_sunday():
    # 2026-11-01 is a Sunday and the U.S. fall-back DST transition day.
    before_open = _ct(2026, 11, 1, 16, 59, 59)
    at_open = _ct(2026, 11, 1, 17, 0, 0)

    assert session_state_at(before_open) is SessionState.WEEKEND_CLOSED
    assert session_state_at(at_open) is SessionState.OPEN


def test_utc_offset_differs_across_dst_transition():
    # Confirms Olive uses real zoneinfo DST rules, not a fixed UTC offset:
    # 17:00 CT is 22:00 UTC in winter (CST, UTC-6) and 22:00 UTC in summer
    # would actually be 17:00 CDT (UTC-5) -- the offsets differ.
    winter = _ct(2026, 1, 15, 17, 0, 0)
    summer = _ct(2026, 7, 15, 17, 0, 0)

    assert winter.utcoffset() != summer.utcoffset()
    # Both are still correctly evaluated as OPEN at the same wall-clock boundary.
    assert session_state_at(winter) is SessionState.OPEN
    assert session_state_at(summer) is SessionState.OPEN


# -- Contract lifecycle / expiration tradability ----------------------------------------


def test_contract_trading_before_final_trading_timestamp(nq, cycle_calendar):
    contract = nq.quarter_contract(2026, ContractMonth.SEPTEMBER)
    just_before = _ct(2026, 9, 18, 8, 29, 59)

    assert contract_state_at(contract, just_before, cycle_calendar) is ContractLifecycleState.TRADING
    assert is_contract_within_trading_life(contract, just_before, cycle_calendar) is True


def test_contract_expired_at_final_trading_timestamp(nq, cycle_calendar):
    contract = nq.quarter_contract(2026, ContractMonth.SEPTEMBER)
    at_termination = _ct(2026, 9, 18, 8, 30, 0)

    assert contract_state_at(contract, at_termination, cycle_calendar) is ContractLifecycleState.EXPIRED
    assert is_contract_within_trading_life(contract, at_termination, cycle_calendar) is False


def test_contract_expired_after_final_trading_timestamp(nq, cycle_calendar):
    contract = nq.quarter_contract(2026, ContractMonth.SEPTEMBER)
    after = _ct(2026, 9, 18, 8, 31, 0)

    assert contract_state_at(contract, after, cycle_calendar) is ContractLifecycleState.EXPIRED


def test_later_contract_still_trading_after_nearer_one_expires(nq, cycle_calendar):
    expired_contract = nq.quarter_contract(2026, ContractMonth.SEPTEMBER)
    later_contract = nq.quarter_contract(2026, ContractMonth.DECEMBER)
    after_september_expiration = _ct(2026, 9, 18, 9, 0, 0)

    assert contract_state_at(expired_contract, after_september_expiration, cycle_calendar) is ContractLifecycleState.EXPIRED
    assert contract_state_at(later_contract, after_september_expiration, cycle_calendar) is ContractLifecycleState.TRADING


def test_contract_state_requires_aware_timestamp(nq, cycle_calendar):
    contract = nq.quarter_contract(2026, ContractMonth.SEPTEMBER)
    naive = datetime(2026, 9, 18, 8, 30, 0)

    with pytest.raises(NaiveDatetimeError):
        contract_state_at(contract, naive, cycle_calendar)


def test_contract_state_at_rejects_non_datetime(nq, cycle_calendar):
    contract = nq.quarter_contract(2026, ContractMonth.SEPTEMBER)

    with pytest.raises(InvalidDatetimeError):
        contract_state_at(contract, "2026-09-18", cycle_calendar)


# ============================================================================
# Phase 2.3 adversarial regression tests (third external audit findings)
# ============================================================================


def test_contract_state_at_rejects_non_contract(cycle_calendar):
    """The exact third-audit bug: contract_state_at('bad', ...) previously
    leaked a raw AttributeError the first time it accessed contract.year."""
    aware = datetime(2026, 9, 18, 8, 30, 0, tzinfo=timezone.utc)

    with pytest.raises(InvalidContractError):
        contract_state_at("bad", aware, cycle_calendar)


def test_contract_state_at_rejects_non_calendar(nq):
    contract = nq.quarter_contract(2026, ContractMonth.SEPTEMBER)
    aware = datetime(2026, 9, 18, 8, 30, 0, tzinfo=timezone.utc)

    with pytest.raises(CycleDateError):
        contract_state_at(contract, aware, "bad")


def test_is_contract_within_trading_life_inherits_contract_type_validation(cycle_calendar):
    """is_contract_within_trading_life delegates straight to
    contract_state_at, so it must inherit the same validation with no
    separate fix needed."""
    aware = datetime(2026, 9, 18, 8, 30, 0, tzinfo=timezone.utc)

    with pytest.raises(InvalidContractError):
        is_contract_within_trading_life("bad", aware, cycle_calendar)


def test_is_contract_within_trading_life_inherits_calendar_type_validation(nq):
    contract = nq.quarter_contract(2026, ContractMonth.SEPTEMBER)
    aware = datetime(2026, 9, 18, 8, 30, 0, tzinfo=timezone.utc)

    with pytest.raises(CycleDateError):
        is_contract_within_trading_life(contract, aware, "bad")


def test_contract_state_at_never_leaks_attribute_error_for_wrong_types(nq, cycle_calendar):
    aware = datetime(2026, 9, 18, 8, 30, 0, tzinfo=timezone.utc)
    for args in (("bad", aware, cycle_calendar), (nq.quarter_contract(2026, 9), aware, "bad")):
        try:
            contract_state_at(*args)
        except (InvalidContractError, CycleDateError):
            pass
        except AttributeError:
            pytest.fail("contract_state_at leaked a raw AttributeError for a bad object type")
