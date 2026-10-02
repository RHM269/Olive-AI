"""Regular CME Globex session schedule and contract-lifecycle state.

Implements the REGULAR WEEKLY trading schedule for NQ/MNQ in
``America/Chicago`` time, plus a mapping from a session timestamp to
its futures trade date, and a check for whether a specific expiring
contract is still within its trading life.

Important truthfulness note (see docs/futures_domain.md): this module
evaluates the *regular weekly schedule* only. It does not know about
exchange holidays, emergency closures, or special schedules -- it is
not a complete, authoritative CME trading-calendar service. Callers
must not treat ``SessionState.OPEN`` as a guarantee that CME is
definitely open on a given date.

Phase 2.3 hardening note: a third external audit found that
``contract_state_at`` took its ``contract``/``cycle_calendar``
parameters on faith -- passing the wrong object type leaked a raw
``AttributeError`` the first time the function touched one of their
attributes. It now validates both up front with the shared
``app.futures.models._require_contract`` and
``app.futures.calendar._require_cycle_calendar`` validators.
``is_contract_within_trading_life`` inherits this fix for free, since
it delegates directly to ``contract_state_at``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from app.futures.calendar import CycleDateCalendar, _require_cycle_calendar, final_trading_timestamp
from app.futures.models import (
    FuturesContract,
    InvalidDatetimeError,
    NaiveDatetimeError,
    _require_contract,
)

CHICAGO_TZ = ZoneInfo("America/Chicago")

_MAINTENANCE_START = time(16, 0)
_MAINTENANCE_END = time(17, 0)
_FRIDAY_CLOSE = time(16, 0)
_SUNDAY_OPEN = time(17, 0)

# Python's Monday=0 ... Sunday=6
_MONDAY, _TUESDAY, _WEDNESDAY, _THURSDAY, _FRIDAY, _SATURDAY, _SUNDAY = range(7)


class SessionState(str, Enum):
    """The regular-weekly-schedule state of the CME Globex NQ/MNQ session."""

    OPEN = "OPEN"
    MAINTENANCE = "MAINTENANCE"
    WEEKEND_CLOSED = "WEEKEND_CLOSED"


class ContractLifecycleState(str, Enum):
    """Whether a specific quarterly contract is still tradable.

    Phase 2 only distinguishes TRADING vs. EXPIRED: Olive does not yet
    track a contract's listing date, so there is no meaningful "not
    listed yet" (FUTURE) state to compute. The enum member is reserved
    for a later phase if that information becomes available.
    """

    TRADING = "TRADING"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class TradeDateResult:
    """The futures trade date implied by a session timestamp, if any.

    ``trade_date`` is ``None`` when the market is in maintenance or
    weekend-closed according to the regular schedule -- Olive returns
    an explicit "no trade date" result rather than a misleading guess.
    """

    trade_date: date | None
    session_state: SessionState


def _require_aware_chicago(timestamp: object) -> datetime:
    """Validate ``timestamp`` is an aware ``datetime`` and convert to Central Time.

    This is the single choke point every public function in this
    module (``session_state_at``, ``trade_date_for``,
    ``contract_state_at``) routes through, so a malformed caller input
    never leaks a raw ``AttributeError`` from accessing ``.tzinfo`` on
    something that isn't a datetime at all (e.g. a plain string).

    Raises :class:`InvalidDatetimeError` if ``timestamp`` is not a
    ``datetime`` at all, or :class:`NaiveDatetimeError` (a narrower
    subclass of it) if it is a ``datetime`` but lacks timezone
    information -- Olive never assumes a naive timestamp means Central
    Time.
    """
    if not isinstance(timestamp, datetime):
        raise InvalidDatetimeError(
            f"Session/contract-lifecycle calculations require a datetime, got {type(timestamp).__name__}"
        )
    if timestamp.tzinfo is None:
        raise NaiveDatetimeError(
            "Session/contract-lifecycle calculations require a timezone-aware datetime; "
            "received a naive datetime instead."
        )
    return timestamp.astimezone(CHICAGO_TZ)


def session_state_at(timestamp: datetime) -> SessionState:
    """Return the regular-weekly-schedule session state at ``timestamp``.

    Regular schedule (all times America/Chicago):
      Sunday:            < 17:00 WEEKEND_CLOSED, >= 17:00 OPEN
      Monday-Thursday:   [16:00, 17:00) MAINTENANCE, otherwise OPEN
      Friday:            < 16:00 OPEN, >= 16:00 WEEKEND_CLOSED
      Saturday:          WEEKEND_CLOSED (always)
    """
    ct = _require_aware_chicago(timestamp)
    weekday = ct.weekday()
    t = ct.time()

    if weekday == _SATURDAY:
        return SessionState.WEEKEND_CLOSED

    if weekday == _SUNDAY:
        return SessionState.OPEN if t >= _SUNDAY_OPEN else SessionState.WEEKEND_CLOSED

    if weekday == _FRIDAY:
        return SessionState.WEEKEND_CLOSED if t >= _FRIDAY_CLOSE else SessionState.OPEN

    # Monday - Thursday
    if _MAINTENANCE_START <= t < _MAINTENANCE_END:
        return SessionState.MAINTENANCE
    return SessionState.OPEN


def trade_date_for(timestamp: datetime) -> TradeDateResult:
    """Map a session timestamp to its futures trade date.

    During an OPEN session before 17:00 CT, the trade date is the
    calendar date itself. From 17:00 CT onward (the start of the next
    trade date's Globex session), the trade date rolls to the next
    calendar day. During MAINTENANCE or WEEKEND_CLOSED, there is no
    single unambiguous trade date, so ``trade_date`` is ``None``.
    """
    ct = _require_aware_chicago(timestamp)
    state = session_state_at(timestamp)

    if state in (SessionState.MAINTENANCE, SessionState.WEEKEND_CLOSED):
        return TradeDateResult(trade_date=None, session_state=state)

    if ct.time() >= _SUNDAY_OPEN:  # 17:00 CT boundary, same for every weekday
        return TradeDateResult(trade_date=ct.date() + timedelta(days=1), session_state=state)

    return TradeDateResult(trade_date=ct.date(), session_state=state)


def contract_state_at(
    contract: FuturesContract,
    timestamp: datetime,
    cycle_calendar: CycleDateCalendar,
) -> ContractLifecycleState:
    """Determine whether ``contract`` is still tradable at ``timestamp``.

    A contract is EXPIRED at and after its official final-trading
    timestamp (8:30 a.m. Central Time on its expiration date), and
    TRADING at any time before that -- regardless of whether the
    general Globex session happens to be open at that instant.

    Raises :class:`~app.futures.models.InvalidContractError` /
    :class:`~app.futures.models.CycleDateError` if ``contract`` /
    ``cycle_calendar`` is not the right object type.
    """
    contract = _require_contract(contract, context="contract_state_at")
    cycle_calendar = _require_cycle_calendar(cycle_calendar, context="contract_state_at")
    ct = _require_aware_chicago(timestamp)
    cycle = cycle_calendar.get(contract.year, int(contract.month))
    final_ts = final_trading_timestamp(cycle.expiration)

    if ct >= final_ts:
        return ContractLifecycleState.EXPIRED
    return ContractLifecycleState.TRADING


def is_contract_within_trading_life(
    contract: FuturesContract,
    timestamp: datetime,
    cycle_calendar: CycleDateCalendar,
) -> bool:
    """True if ``contract`` has not yet reached its final trading timestamp."""
    return contract_state_at(contract, timestamp, cycle_calendar) is ContractLifecycleState.TRADING
