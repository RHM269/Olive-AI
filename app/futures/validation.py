"""Olive PRODUCTION futures-domain integrity validation (Phase 2.4).

Distinguishes two different questions that this package answers at two
different layers:

- **Generic structural validity** (``app.futures.models``,
  ``app.futures.registry``, ``app.futures.calendar``): "Is this a
  well-formed, internally self-consistent futures instrument / registry
  / cycle-date calendar?" A generic registry entry with
  ``multiplier=2``, ``tick_size=0.25``, ``tick_value=0.50`` is perfectly
  valid by this standard -- ``0.25 * 2 == 0.50``, the arithmetic checks
  out. This layer intentionally has no opinion on *which* product a
  given root symbol is supposed to describe, because
  ``FuturesInstrumentRegistry`` / ``CycleDateCalendar`` are deliberately
  generic (see their own module docstrings) -- usable for tests, for a
  future instrument, or for an entirely different market.
- **Olive PRODUCTION domain integrity** (this module): "Is this
  *specifically* the real NQ/MNQ product Olive is designed to trade,
  and does Olive's calendar still contain the source-backed official
  coverage Phase 2 shipped with?" A fourth external audit demonstrated
  that a structurally valid but factually WRONG NQ definition
  (``multiplier=2``, ``tick_size=0.25``, ``tick_value=0.50``,
  ``contract_months=[3]``) sailed straight through every check in
  ``app.health._check_futures_domain()`` before this module existed --
  the registry loaded, the roots were exactly ``{"NQ", "MNQ"}``, and
  the calendar loaded. Nothing checked that the *NQ entry itself* still
  described the real E-mini Nasdaq-100 future. Likewise, a cycle-date
  calendar truncated down to only 2025's official rows is a perfectly
  valid ``CycleDateCalendar`` (its own cross-validation only requires
  internal self-consistency, not any particular amount of coverage) --
  but it silently drops Olive's required 2026-2028 source-backed
  baseline, including the June 2026 holiday-exception anchor, with no
  signal to the health report.

This module does not reimplement any domain arithmetic or object
validation that ``app.futures.models`` / ``app.futures.registry`` /
``app.futures.calendar`` already perform (tick-value cross-checks,
quarter-month coercion, generic mapping/key validation, and so on). It
answers a narrower, later question: given an already-structurally-valid
registry and calendar, do they actually describe Olive's required
PRODUCTION NQ/MNQ domain? It is deliberately reusable -- by
``app.health``, and by any future Phase 3+ service that first needs to
establish that Olive's domain foundation is trustworthy before trusting
it with real money.

Phase 2.5 hardening note: an internal QA process correction found that
``validate_olive_tradable_registry`` / ``validate_olive_cycle_calendar``
/ ``validate_olive_futures_domain`` -- despite being documented above
as public, reusable entry points -- took their ``registry``/``calendar``
arguments entirely on faith. A caller passing ``None``, a primitive, or
even a *different* Olive domain object (e.g. a ``CycleDateCalendar``
where a registry was expected) leaked a raw ``AttributeError`` instead
of a domain error -- exactly the failure pattern Phase 2.3 had just
established must never happen at a public futures-domain boundary, now
repeated one correction later in a brand-new module. Each function
below now validates its parameters first, via ``_require_registry``
(``app.futures.registry``) and ``_require_cycle_calendar``
(``app.futures.calendar``) -- the same shared validators already used
elsewhere in the package, not new duplicate logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from app.futures.calendar import CycleDateCalendar, _require_cycle_calendar
from app.futures.models import (
    ContractMonth,
    CycleDateSource,
    FuturesDomainError,
    FuturesInstrument,
    SettlementType,
)
from app.futures.registry import FuturesInstrumentRegistry, _require_registry


class ProductionDomainIntegrityError(FuturesDomainError):
    """Raised when an already-structurally-valid futures-domain object
    does not actually satisfy Olive's required PRODUCTION NQ/MNQ domain
    facts.

    This is distinct from the generic ``FuturesConfigurationError`` /
    ``CycleDateError`` raised by ``app.futures.models`` /
    ``app.futures.registry`` / ``app.futures.calendar`` themselves: those
    fire when an object is not even internally self-consistent (e.g.
    ``tick_value != tick_size * multiplier``). This fires when an object
    IS internally self-consistent but still doesn't describe what Olive
    actually requires in production -- a factually wrong NQ/MNQ
    definition, or a cycle-date calendar missing Olive's required
    source-backed coverage.
    """


# -- Olive's required PRODUCTION tradable universe ---------------------------
#
# Deliberately distinct from `FuturesInstrumentRegistry`, which is a
# generic loader capable of holding any validly-configured instrument
# (useful for tests and future extensibility -- see
# app.futures.registry's module docstring). A config file that loads
# successfully but contains an unexpected extra root (e.g. "ES"), or is
# missing one of NQ/MNQ, must never be reported as a healthy Olive
# futures domain. This check previously lived in app.health directly;
# it now lives here so the one "is this Olive's production domain?"
# question has exactly one home.

REQUIRED_TRADABLE_ROOTS: frozenset[str] = frozenset({"NQ", "MNQ"})


@dataclass(frozen=True)
class _CanonicalInstrumentSpec:
    """Olive's canonical Phase 2 product facts for one tradable root.

    Financial/economic identity -- not presentation strings -- is what
    this validates. ``display_name`` is deliberately NOT part of this
    spec: see the module-level note below this class for why.
    """

    root_symbol: str
    exchange: str
    underlying: str
    currency: str
    multiplier: Decimal
    tick_size: Decimal
    tick_value: Decimal
    settlement_type: SettlementType
    contract_months: frozenset[ContractMonth]


# Design decision on display_name (Phase 2.4, §8 of the corrective
# brief): the current configured display names ("E-mini Nasdaq-100
# Futures", "Micro E-mini Nasdaq-100 Futures") are accurate and stable,
# but they are presentation metadata, not part of the financial
# contract -- a harmless capitalization or wording change (e.g. "E-Mini"
# vs. "E-mini") must never, by itself, make an otherwise-correct NQ/MNQ
# definition report as a broken production domain. The financial
# identity fields below (root symbol, exchange, underlying, currency,
# multiplier, tick size, tick value, settlement type, quarterly months)
# are what Olive's trade economics and contract identity actually
# depend on, so those are what this module enforces exactly.
# `display_name` is intentionally left unvalidated here.

_NQ_SPEC = _CanonicalInstrumentSpec(
    root_symbol="NQ",
    exchange="CME",
    underlying="Nasdaq-100 Index",
    currency="USD",
    multiplier=Decimal("20"),
    tick_size=Decimal("0.25"),
    tick_value=Decimal("5.00"),
    settlement_type=SettlementType.CASH,
    contract_months=frozenset(
        {ContractMonth.MARCH, ContractMonth.JUNE, ContractMonth.SEPTEMBER, ContractMonth.DECEMBER}
    ),
)

_MNQ_SPEC = _CanonicalInstrumentSpec(
    root_symbol="MNQ",
    exchange="CME",
    underlying="Nasdaq-100 Index",
    currency="USD",
    multiplier=Decimal("2"),
    tick_size=Decimal("0.25"),
    tick_value=Decimal("0.50"),
    settlement_type=SettlementType.CASH,
    contract_months=frozenset(
        {ContractMonth.MARCH, ContractMonth.JUNE, ContractMonth.SEPTEMBER, ContractMonth.DECEMBER}
    ),
)

_CANONICAL_SPECS: tuple[_CanonicalInstrumentSpec, ...] = (_NQ_SPEC, _MNQ_SPEC)


def _validate_instrument_matches_spec(instrument: FuturesInstrument, spec: _CanonicalInstrumentSpec) -> None:
    """Compare an already-structurally-valid instrument against Olive's
    canonical production spec for its root symbol.

    Every mismatch is collected (rather than failing on the first one)
    so a single error message reports everything wrong at once --
    useful when, as in the exact external-audit reproduction, several
    fields were changed together.
    """
    mismatches: list[str] = []

    if instrument.root_symbol != spec.root_symbol:
        mismatches.append(f"root_symbol: expected {spec.root_symbol!r}, got {instrument.root_symbol!r}")
    if instrument.exchange != spec.exchange:
        mismatches.append(f"exchange: expected {spec.exchange!r}, got {instrument.exchange!r}")
    if instrument.underlying != spec.underlying:
        mismatches.append(f"underlying: expected {spec.underlying!r}, got {instrument.underlying!r}")
    if instrument.currency != spec.currency:
        mismatches.append(f"currency: expected {spec.currency!r}, got {instrument.currency!r}")
    # Exact Decimal comparisons -- Decimal.__eq__ compares numeric
    # value, not representation, so "5.00" and "5.0" are equal here
    # (as they should be); only a genuine economic difference mismatches.
    if instrument.multiplier != spec.multiplier:
        mismatches.append(f"multiplier: expected {spec.multiplier}, got {instrument.multiplier}")
    if instrument.tick_size != spec.tick_size:
        mismatches.append(f"tick_size: expected {spec.tick_size}, got {instrument.tick_size}")
    if instrument.tick_value != spec.tick_value:
        mismatches.append(f"tick_value: expected {spec.tick_value}, got {instrument.tick_value}")
    if instrument.settlement_type is not spec.settlement_type:
        mismatches.append(
            f"settlement_type: expected {spec.settlement_type!r}, got {instrument.settlement_type!r}"
        )

    actual_months = frozenset(instrument.contract_months)
    if actual_months != spec.contract_months:
        missing = sorted(m.value for m in spec.contract_months - actual_months)
        unexpected = sorted(m.value for m in actual_months - spec.contract_months)
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if unexpected:
            parts.append(f"unexpected {unexpected}")
        mismatches.append(f"contract_months: {'; '.join(parts)}")

    if mismatches:
        raise ProductionDomainIntegrityError(
            f"{spec.root_symbol}: configured instrument does not match Olive's canonical "
            f"Phase 2 production specification ({'; '.join(mismatches)})."
        )


def validate_olive_tradable_registry(registry: FuturesInstrumentRegistry) -> None:
    """Validate that ``registry`` is exactly Olive's required PRODUCTION
    tradable universe -- not merely a structurally valid registry.

    Two independent things must both hold:

    1. The registry's roots must be *exactly* ``{"NQ", "MNQ"}`` -- no
       missing, no unexpected extras (absorbed here from the health
       layer, which previously checked this itself; that invariant now
       has exactly one home).
    2. Each of the NQ and MNQ entries must match Olive's canonical
       Phase 2 financial/economic specification exactly (see
       ``_CanonicalInstrumentSpec`` above) -- a registry can be
       internally self-consistent (``tick_value == tick_size *
       multiplier``) while still not describing the real product.

    Raises :class:`ProductionDomainIntegrityError` if either check
    fails. Never raises for, and has no opinion about, any OTHER
    generically-valid registry (e.g. a test registry holding an
    unrelated instrument) -- this function is specifically about
    Olive's own required production universe, not a global constraint
    on what :class:`FuturesInstrumentRegistry` may ever hold.

    Raises :class:`app.futures.models.InvalidRegistryError` (Phase 2.5)
    if ``registry`` is not actually a :class:`FuturesInstrumentRegistry`
    at all -- including a *different* Olive domain object such as a
    :class:`CycleDateCalendar` -- instead of leaking a raw
    ``AttributeError`` the first time this function touches ``.roots``.
    """
    registry = _require_registry(registry, context="validate_olive_tradable_registry")

    actual_roots = set(registry.roots)
    if actual_roots != REQUIRED_TRADABLE_ROOTS:
        missing = sorted(REQUIRED_TRADABLE_ROOTS - actual_roots)
        unexpected = sorted(actual_roots - REQUIRED_TRADABLE_ROOTS)
        parts = []
        if missing:
            parts.append(f"missing: {', '.join(missing)}")
        if unexpected:
            parts.append(f"unexpected: {', '.join(unexpected)}")
        raise ProductionDomainIntegrityError(
            "Tradable registry does not match Olive's Phase 2 production universe "
            f"{{{', '.join(sorted(REQUIRED_TRADABLE_ROOTS))}}} ({'; '.join(parts)})."
        )

    for spec in _CANONICAL_SPECS:
        instrument = registry.get(spec.root_symbol)
        _validate_instrument_matches_spec(instrument, spec)


# -- Olive's required official cycle-date baseline ---------------------------
#
# Phase 2's source-backed CME quarterly expiration/roll table covers
# 2025-2028 (see config/calendar/cme_equity_index_cycle_dates.json and
# docs/futures_domain.md §3). This is the one place that baseline is
# recorded for PRODUCTION validation purposes -- not scattered across
# multiple modules. A future, legitimate update that ADDS source-backed
# years (2029, 2030, ...) must never have to delete or edit this mapping
# to stay valid; see `validate_olive_cycle_calendar` below, which checks
# this mapping is a SUBSET of what the live calendar has, never that the
# live calendar equals exactly this mapping.

OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES: Mapping[tuple[int, int], tuple[date, date]] = MappingProxyType(
    {
        (2025, 3): (date(2025, 3, 21), date(2025, 3, 17)),
        (2025, 6): (date(2025, 6, 20), date(2025, 6, 16)),
        (2025, 9): (date(2025, 9, 19), date(2025, 9, 15)),
        (2025, 12): (date(2025, 12, 19), date(2025, 12, 15)),
        (2026, 3): (date(2026, 3, 20), date(2026, 3, 16)),
        # The headline Phase 2 holiday exception: official expiration
        # (2026-06-18, a Thursday) differs from the naive third-Friday
        # rule (2026-06-19) -- see docs/futures_domain.md §3. Must never
        # silently fall back to CALCULATED_NOMINAL or drift to a
        # different date.
        (2026, 6): (date(2026, 6, 18), date(2026, 6, 15)),
        (2026, 9): (date(2026, 9, 18), date(2026, 9, 14)),
        (2026, 12): (date(2026, 12, 18), date(2026, 12, 14)),
        (2027, 3): (date(2027, 3, 19), date(2027, 3, 15)),
        (2027, 6): (date(2027, 6, 18), date(2027, 6, 14)),
        (2027, 9): (date(2027, 9, 17), date(2027, 9, 13)),
        (2027, 12): (date(2027, 12, 17), date(2027, 12, 13)),
        (2028, 3): (date(2028, 3, 17), date(2028, 3, 13)),
        (2028, 6): (date(2028, 6, 16), date(2028, 6, 12)),
        (2028, 9): (date(2028, 9, 15), date(2028, 9, 11)),
        (2028, 12): (date(2028, 12, 15), date(2028, 12, 11)),
    }
)

# Derived, not hand-duplicated, so the "which years are required" answer
# can never drift out of sync with the row-level mapping above.
REQUIRED_BASELINE_YEARS: frozenset[int] = frozenset(year for year, _month in OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES)


def validate_olive_cycle_calendar(calendar: CycleDateCalendar) -> None:
    """Validate that ``calendar`` still contains Olive's required
    PRODUCTION source-backed official coverage -- not merely a
    structurally valid calendar.

    Two independent things must both hold:

    1. ``calendar.covered_years`` must be a SUPERSET of Olive's
       required baseline years (2025-2028) -- never required to equal
       that set exactly, so a legitimate future update that adds 2029,
       2030, ... remains valid without touching this function.
    2. Every required ``(year, month)`` row in
       ``OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES`` must still be present,
       still ``OFFICIAL`` (not fallen back to ``CALCULATED_NOMINAL``),
       and still carry its exact required ``expiration``/``roll``
       dates -- this is what catches a calendar that still declares
       full 2025-2028 coverage but has had an individual anchor (e.g.
       the June 2026 holiday exception) silently corrupted to a
       different, still-structurally-valid date.

    Raises :class:`ProductionDomainIntegrityError` if either check
    fails. Never raises for, and has no opinion about, any OTHER
    generically-valid calendar (e.g. a test calendar covering an
    unrelated year range) -- this function is specifically about
    Olive's own required baseline, not a global constraint on what
    :class:`CycleDateCalendar` may ever represent.

    Raises an appropriate :class:`FuturesDomainError` subclass
    (Phase 2.5, via the shared ``_require_cycle_calendar`` validator
    already used by ``app.futures.roll`` / ``app.futures.sessions``)
    if ``calendar`` is not actually a :class:`CycleDateCalendar` at
    all -- including a *different* Olive domain object such as a
    :class:`FuturesInstrumentRegistry` -- instead of leaking a raw
    ``AttributeError`` the first time this function touches
    ``.covered_years``.
    """
    calendar = _require_cycle_calendar(calendar, context="validate_olive_cycle_calendar")

    covered = set(calendar.covered_years)
    missing_years = sorted(REQUIRED_BASELINE_YEARS - covered)
    if missing_years:
        raise ProductionDomainIntegrityError(
            "Cycle-date calendar is missing Olive's required Phase 2 source-backed coverage "
            f"for year(s) {missing_years} (required baseline: {sorted(REQUIRED_BASELINE_YEARS)}). "
            "Future official years may be ADDED without breaking this check; required baseline "
            "years may never be removed."
        )

    mismatches: list[str] = []
    for (year, month), (expected_expiration, expected_roll) in OLIVE_REQUIRED_OFFICIAL_CYCLE_DATES.items():
        if not calendar.has_official(year, month):
            mismatches.append(f"{year}-{month:02d}: required OFFICIAL anchor is no longer OFFICIAL")
            continue

        cycle = calendar.get(year, month)
        if cycle.source is not CycleDateSource.OFFICIAL:
            # Defensive: has_official() already implies this, but a
            # production-integrity check should never trust a single
            # code path to be the only thing standing between it and a
            # silently-demoted anchor.
            mismatches.append(f"{year}-{month:02d}: source is {cycle.source!r}, expected OFFICIAL")
            continue
        if cycle.expiration != expected_expiration or cycle.roll != expected_roll:
            mismatches.append(
                f"{year}-{month:02d}: expected expiration={expected_expiration} roll={expected_roll}, "
                f"got expiration={cycle.expiration} roll={cycle.roll}"
            )

    if mismatches:
        raise ProductionDomainIntegrityError(
            "Cycle-date calendar no longer matches Olive's required Phase 2 source-backed "
            f"anchors: {'; '.join(mismatches)}"
        )


def validate_olive_futures_domain(registry: FuturesInstrumentRegistry, calendar: CycleDateCalendar) -> None:
    """The single entry point Olive's health check (and any future
    Phase 3+ service) should call to establish that the futures domain
    is not just structurally valid, but actually Olive's required
    PRODUCTION NQ/MNQ domain.

    Equivalent to calling :func:`validate_olive_tradable_registry` and
    :func:`validate_olive_cycle_calendar` in sequence; kept as one
    function so callers that only care about "is the whole domain
    trustworthy?" have exactly one thing to call.

    Parameter-type validation (Phase 2.5) is intentionally not
    duplicated here -- it is inherited from the two functions above,
    each of which validates its own single parameter first. This means
    a caller who accidentally swaps the two arguments (passing a
    ``CycleDateCalendar`` as ``registry``, or a
    ``FuturesInstrumentRegistry`` as ``calendar``) still fails with a
    clear domain error from whichever check runs first, rather than a
    raw ``AttributeError`` or a second, redundant type check living
    here as well.
    """
    validate_olive_tradable_registry(registry)
    validate_olive_cycle_calendar(calendar)
