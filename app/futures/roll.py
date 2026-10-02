"""Customary, calendar-based quarterly contract roll resolution.

Phase 2 has no volume or open-interest data, so Olive cannot truthfully
determine which contract is actually most liquid right now. Everything
in this module computes a deterministic CALENDAR / CUSTOMARY result --
named accordingly (``calendar_lead_contract``, never
``actual_active_contract``) -- based on CME's customary U.S.
equity-index roll convention: the Monday prior to the third Friday of
the expiration month, after which the next quarterly contract becomes
the customary lead.

A later phase (real-time market data) may enhance active-contract
selection with real volume/open-interest; this module must not be
mistaken for that.

Phase 2.3 hardening note: a third external audit found that every
public function here took its ``instrument``/``cycle_calendar``/
``cycle`` parameters on faith -- passing the wrong object type leaked
a raw ``AttributeError`` the first time the function touched one of
the object's attributes. Every function below now validates its
domain-object parameters up front with the shared
``app.futures.models._require_instrument`` / ``_require_cycle_dates``
and ``app.futures.calendar._require_cycle_calendar`` validators
(``trade_date``/``timestamp`` parameters were already validated as of
Phase 2.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.futures.calendar import CycleDateCalendar, _require_cycle_calendar
from app.futures.contracts import next_contract
from app.futures.models import (
    ContractCycleDates,
    ContractMonth,
    FuturesContract,
    FuturesDomainError,
    FuturesInstrument,
    InvalidDateError,
    _require_cycle_dates,
    _require_instrument,
    _require_plain_date,
)
from app.futures.sessions import trade_date_for

# Safety cap for the forward quarter scan in nearest_unexpired_contract --
# comfortably more than enough (10 years) to find a result, and prevents
# an unbounded loop if calendar data is ever corrupted.
_MAX_QUARTERS_SCANNED = 40


class SessionClosedError(FuturesDomainError):
    """Raised when a calendar-lead lookup is attempted from a timestamp

    that falls in a maintenance or weekend-closed session, where no
    single unambiguous trade date exists. Callers in that situation
    should supply an explicit ``trade_date`` instead of a timestamp.
    """


def _require_trade_date(value: object, *, context: str) -> date:
    """Validate that ``value`` is a plain ``date`` for a date-only trade-date parameter.

    ``datetime`` is deliberately rejected here even though it
    subclasses ``date``: silently accepting one and discarding its
    time-of-day would be a surprising, easy-to-miss behavior change for
    a caller who passed a timestamp by mistake. Use
    ``app.futures.sessions.trade_date_for`` to derive an explicit trade
    date from a timestamp first, then pass that ``date`` here. A
    non-date value (a string, ``None``, etc.) raises
    :class:`InvalidDateError` rather than leaking a raw
    ``AttributeError`` from accessing ``.year``/comparisons on it.

    Phase 2.3: this now delegates to the shared, centralized
    ``app.futures.models._require_plain_date`` (the same validator
    ``ContractCycleDates`` and ``nominal_customary_roll`` use), rather
    than duplicating the same ``isinstance``/``type`` checks a second
    time in this module.
    """
    return _require_plain_date(value, context=context)


def nearest_unexpired_contract(
    instrument: FuturesInstrument,
    trade_date: date,
    cycle_calendar: CycleDateCalendar,
) -> FuturesContract:
    """The soonest quarterly contract for ``instrument`` not yet expired as of ``trade_date``.

    Scans forward through ``instrument``'s quarterly contract months,
    starting a year before ``trade_date``, and returns the first one
    whose official/calculated expiration date is on or after
    ``trade_date``.

    Raises :class:`~app.futures.models.InvalidDateError` if
    ``trade_date`` is not a plain ``date`` (a ``datetime`` is
    deliberately rejected -- see :func:`_require_trade_date`). Raises
    :class:`~app.futures.models.InvalidInstrumentError` /
    :class:`~app.futures.models.CycleDateError` if ``instrument`` /
    ``cycle_calendar`` is not the right object type.
    """
    instrument = _require_instrument(instrument, context="nearest_unexpired_contract")
    trade_date = _require_trade_date(trade_date, context="nearest_unexpired_contract")
    cycle_calendar = _require_cycle_calendar(cycle_calendar, context="nearest_unexpired_contract")
    months_sorted = sorted(int(m) for m in instrument.contract_months)
    year = trade_date.year - 1
    scanned = 0

    while scanned < _MAX_QUARTERS_SCANNED:
        for month in months_sorted:
            scanned += 1
            cycle = cycle_calendar.get(year, month)
            if cycle.expiration >= trade_date:
                return instrument.quarter_contract(year, ContractMonth(month))
            if scanned >= _MAX_QUARTERS_SCANNED:
                break
        year += 1

    raise FuturesDomainError(
        f"Could not find an unexpired {instrument.root_symbol} contract within "
        f"{_MAX_QUARTERS_SCANNED} quarters of {trade_date}"
    )


def is_post_roll(trade_date: date, cycle: ContractCycleDates) -> bool:
    """True if ``trade_date`` is on or after a contract cycle's customary roll date.

    Raises :class:`~app.futures.models.InvalidDateError` if
    ``trade_date`` is not a plain ``date`` (see
    :func:`_require_trade_date`). Raises
    :class:`~app.futures.models.CycleDateError` if ``cycle`` is not a
    :class:`~app.futures.models.ContractCycleDates`.
    """
    trade_date = _require_trade_date(trade_date, context="is_post_roll")
    cycle = _require_cycle_dates(cycle, context="is_post_roll")
    return trade_date >= cycle.roll


def calendar_lead_contract(
    instrument: FuturesInstrument,
    trade_date: date,
    cycle_calendar: CycleDateCalendar,
) -> FuturesContract:
    """The customary, calendar-based lead contract for ``trade_date``.

    Before the nearest unexpired contract's customary roll date, the
    lead is that nearest contract itself. On or after its roll date,
    the lead becomes the next quarterly contract out -- even though
    the nearer contract may still be technically tradable until its
    own expiration. This mirrors CME's customary roll convention; it
    is a calendar convention, not evidence of current liquidity.

    Raises :class:`~app.futures.models.InvalidDateError` if
    ``trade_date`` is not a plain ``date`` (see
    :func:`_require_trade_date`). Raises
    :class:`~app.futures.models.InvalidInstrumentError` /
    :class:`~app.futures.models.CycleDateError` if ``instrument`` /
    ``cycle_calendar`` is not the right object type.
    """
    instrument = _require_instrument(instrument, context="calendar_lead_contract")
    trade_date = _require_trade_date(trade_date, context="calendar_lead_contract")
    cycle_calendar = _require_cycle_calendar(cycle_calendar, context="calendar_lead_contract")
    nearest = nearest_unexpired_contract(instrument, trade_date, cycle_calendar)
    nearest_cycle = cycle_calendar.get(nearest.year, int(nearest.month))

    if is_post_roll(trade_date, nearest_cycle):
        return next_contract(nearest, instrument)
    return nearest


def calendar_lead_contract_at(
    instrument: FuturesInstrument,
    timestamp: datetime,
    cycle_calendar: CycleDateCalendar,
) -> FuturesContract:
    """Convenience wrapper: derive the trade date from a session timestamp first.

    Raises :class:`SessionClosedError` if ``timestamp`` falls during
    maintenance or a weekend closure, where no unambiguous trade date
    exists -- callers in that situation should compute and pass an
    explicit trade date via :func:`calendar_lead_contract` instead.
    ``instrument``/``cycle_calendar`` type validation is inherited from
    :func:`calendar_lead_contract`, which this delegates to.
    """
    result = trade_date_for(timestamp)
    if result.trade_date is None:
        raise SessionClosedError(
            f"Cannot determine a calendar lead contract at {timestamp.isoformat()}: "
            f"session state is {result.session_state.value}, with no unambiguous trade date. "
            "Pass an explicit trade_date to calendar_lead_contract() instead."
        )
    return calendar_lead_contract(instrument, result.trade_date, cycle_calendar)
