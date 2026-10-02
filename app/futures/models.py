"""Core typed domain objects for Olive AI's futures domain.

Defines the futures-domain error hierarchy, the quarterly contract
month / CME month-code vocabulary, the settlement/cycle-date-source
enums, the pure nominal-calendar helpers (``nominal_third_friday`` /
``nominal_customary_roll``), and the three central immutable domain
objects: :class:`FuturesInstrument` (a tradable product definition,
e.g. NQ), :class:`FuturesContract` (one specific quarterly contract of
that product, e.g. NQ December 2026), and :class:`ContractCycleDates`
(the expiration/roll dates for one contract cycle).

This module performs no I/O. It defines types and validates values
passed to it -- it does not read configuration files or the clock.

Phase 2.1 hardening note: Python type hints are not runtime-enforced,
so every one of these dataclasses validates and normalizes its own
inputs in ``__post_init__`` rather than trusting callers to have gone
through a "nicer" entry point (e.g. the JSON registry loader). After
successful construction, the invariants documented on each class hold
unconditionally -- e.g. ``isinstance(contract.month, ContractMonth)``
is always true for any live ``FuturesContract``.

Phase 2.2 hardening note: a second external audit found that
non-finite ``Decimal`` economics (``NaN``/``Infinity``/``-Infinity``)
could still be constructed, that caller-supplied point/price values
could leak ``decimal.InvalidOperation`` through the tick/dollar
conversion helpers, and that ``nominal_third_friday`` /
``nominal_customary_roll`` (moved here from ``app.futures.calendar``
so :class:`ContractCycleDates` can enforce the exact customary-roll
invariant regardless of construction path -- see its docstring) did
not validate their own inputs. All of that is hardened below.

Phase 2.3 hardening note: a third audit found that ``datetime`` (which
subclasses ``date``) could still slip into date-only fields
(``ContractCycleDates.expiration``/``.roll``, and
``nominal_customary_roll``'s argument), producing cross-type
comparison ``TypeError``s or silently wrong results; that
``FuturesInstrument.contract_months`` was iterated without first
checking it was actually an iterable container (``True`` raised a raw
``TypeError``); and that extreme-but-finite ``Decimal`` economics
(e.g. ``Decimal("1E+999999")``) could overflow the active arithmetic
context and leak ``decimal.Overflow``. A new shared
``_require_plain_date`` helper, a contract_months container check, and
narrow ``DecimalException`` handling around the economics arithmetic
fix all three.

Phase 2.5 hardening note: an internal QA process correction found that
the three new PRODUCTION validation entry points added in Phase 2.4
(``app.futures.validation.validate_olive_tradable_registry`` /
``validate_olive_cycle_calendar`` / ``validate_olive_futures_domain``)
had not been brought under the same "every public parameter is
validated before use" discipline this module established for
``instrument``/``contract``/``cycle_dates`` parameters elsewhere in
the package -- a malformed or wrong-kind ``registry`` argument leaked
a raw ``AttributeError`` instead of a domain error. The new
:class:`InvalidRegistryError` below, and the ``_require_registry``
validator in ``app.futures.registry`` (which mirrors this module's
``_require_instrument`` / ``_require_contract`` /
``_require_cycle_dates`` exactly), close that gap. No change to this
module's own classes or invariants was needed.
"""

from __future__ import annotations

import calendar as _stdlib_calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, InvalidOperation
from enum import Enum, IntEnum


class FuturesDomainError(Exception):
    """Base class for all Olive futures-domain errors.

    Public futures-domain functions should raise a subclass of this
    (never a bare `ValueError`/`KeyError`/`TypeError`/`AttributeError`/
    stdlib exception such as `calendar.IllegalMonthError` or
    `decimal.InvalidOperation`) for any input a caller could reasonably
    be expected to get wrong. This makes invalid-domain input fail
    predictably across the whole package.
    """


class FuturesConfigurationError(FuturesDomainError):
    """Raised when futures instrument configuration is invalid."""


class UnknownInstrumentError(FuturesDomainError):
    """Raised when an unrecognized instrument root symbol is requested."""


class InvalidContractMonthError(FuturesDomainError):
    """Raised when a contract month is not valid for a given instrument,
    or is not a recognized quarterly month at all."""


class ContractInstrumentMismatchError(FuturesDomainError):
    """Raised when a contract-navigation helper is asked to operate on a
    :class:`FuturesContract` that does not belong to the supplied
    :class:`FuturesInstrument`.

    Contract navigation (``next_contract`` / ``previous_contract``)
    must never silently reinterpret a contract under a different
    instrument (e.g. advancing an NQ contract "as if" it were MNQ).
    """


class InvalidInstrumentError(FuturesDomainError):
    """Raised when a value that should be a :class:`FuturesInstrument` is not one.

    Every public function across the futures domain that takes an
    ``instrument`` parameter (``next_contract``, ``quarter_contract``,
    the tick/price helpers, the roll resolvers, ...) validates it with
    this error rather than letting a caller's mistake (e.g. passing a
    plain string) leak a raw ``AttributeError`` the first time the
    function touches one of the instrument's attributes.
    """


class InvalidContractError(FuturesDomainError):
    """Raised when a value that should be a :class:`FuturesContract` is not one.

    Mirrors :class:`InvalidInstrumentError` for the ``contract``
    parameter of contract-navigation helpers (``next_contract`` /
    ``previous_contract``) and session/lifecycle functions
    (``contract_state_at``).
    """


class InvalidRegistryError(FuturesDomainError):
    """Raised when a value that should be a
    :class:`app.futures.registry.FuturesInstrumentRegistry` is not one.

    Mirrors :class:`InvalidInstrumentError` / :class:`InvalidContractError`
    for the ``registry`` parameter of the Phase 2.4 production-domain
    validation entry points (``app.futures.validation``). Not defined in
    ``app.futures.registry`` itself only so every "wrong Olive domain
    object" error lives in this module's error hierarchy alongside its
    siblings; the validator that raises it (``_require_registry``) lives
    with :class:`app.futures.registry.FuturesInstrumentRegistry` instead,
    matching the existing ``_require_cycle_calendar`` /
    ``app.futures.calendar.CycleDateCalendar`` precedent.
    """


class CycleDateError(FuturesDomainError):
    """Raised when quarterly cycle-date calendar data is invalid or missing."""


class TickAlignmentError(FuturesDomainError):
    """Raised when a price value does not align exactly to an instrument's tick size."""


class InvalidTickCountError(FuturesDomainError):
    """Raised when a tick count is not a true integer (e.g. a fractional
    value, or a bool masquerading as one). Negative integers are valid
    -- the constraint is "must be an integer," not "must be positive."
    """


class InvalidPointValueError(FuturesDomainError):
    """Raised when a point/price value supplied to a tick/dollar
    conversion helper is not a finite, real numeric value.

    Accepts the same caller convenience the economics helpers always
    have (``Decimal``, ``int``, ``float``, or a numeric ``str``), but
    rejects ``bool``, ``None``, non-numeric strings, and any
    non-finite value (``NaN``, ``Infinity``, ``-Infinity``). Never
    leaks ``decimal.InvalidOperation`` or a bare ``ValueError``.
    """


class InvalidDateError(FuturesDomainError):
    """Raised when a value that should be a plain ``datetime.date`` is
    not one -- e.g. a string, ``None``, or (for a date-only API) a
    ``datetime`` whose time-of-day would otherwise be silently
    discarded if it were accepted.
    """


class InvalidDatetimeError(FuturesDomainError):
    """Raised when a value that should be a ``datetime.datetime`` is not
    a datetime at all (e.g. a string or ``None``).

    See :class:`NaiveDatetimeError` for the narrower case of an actual
    datetime that is simply missing timezone information.
    """


class NaiveDatetimeError(InvalidDatetimeError):
    """Raised when a timezone-naive datetime is supplied where an aware one is required.

    Olive never guesses that a naive timestamp means Central Time (or any
    other zone); callers must supply timezone-aware datetimes.
    """


class SettlementType(str, Enum):
    """How a futures contract is settled at expiration."""

    CASH = "CASH"


class CycleDateSource(str, Enum):
    """Provenance of a quarterly contract's expiration/roll dates.

    ``OFFICIAL`` means Olive has an explicit, source-backed record for
    that (year, month) pair (see config/calendar/cme_equity_index_cycle_dates.json).
    ``CALCULATED_NOMINAL`` means Olive derived the date using the
    normal "third Friday" quarterly rule as a fallback, and is NOT
    claiming exchange-authoritative holiday handling for it.
    """

    OFFICIAL = "OFFICIAL"
    CALCULATED_NOMINAL = "CALCULATED_NOMINAL"


class ContractMonth(IntEnum):
    """The quarterly contract months NQ/MNQ trade, by calendar month number."""

    MARCH = 3
    JUNE = 6
    SEPTEMBER = 9
    DECEMBER = 12


# CME month codes for the standard quarterly cycle.
CME_QUARTERLY_MONTH_CODES: dict[ContractMonth, str] = {
    ContractMonth.MARCH: "H",
    ContractMonth.JUNE: "M",
    ContractMonth.SEPTEMBER: "U",
    ContractMonth.DECEMBER: "Z",
}

# The canonical set of valid quarterly month *values* (plain ints), used
# for runtime validation outside the ContractMonth enum itself (e.g. the
# cycle-date calendar, which keys on plain (year, month) int pairs).
_QUARTERLY_MONTH_VALUES: frozenset[int] = frozenset(int(m) for m in ContractMonth)
_QUARTERLY_MONTHS: tuple[int, ...] = tuple(sorted(_QUARTERLY_MONTH_VALUES))

_MIN_REASONABLE_YEAR = 1900
_MAX_REASONABLE_YEAR = 2999


def require_quarterly_month(value: object) -> int:
    """Validate that ``value`` is one of Olive's quarterly cycle months (3, 6, 9, 12).

    This is the single definition of "valid quarterly month" shared by
    :class:`ContractCycleDates` and the calendar module
    (``app.futures.calendar``), so there is exactly one place that
    decides what counts as a quarter. Rejects non-integers, bools
    (``True``/``False`` are technically ``int`` in Python but are never
    valid months), and any integer outside ``{3, 6, 9, 12}`` --
    including otherwise-ordinary calendar months like January.

    Raises :class:`CycleDateError`. Returns the validated int unchanged.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value not in _QUARTERLY_MONTH_VALUES:
        raise CycleDateError(
            f"Month must be one of {sorted(_QUARTERLY_MONTH_VALUES)} (the NQ/MNQ quarterly cycle), "
            f"got {value!r}"
        )
    return value


def _require_year(value: object, *, context: str) -> int:
    """Shared runtime year validation for contracts, cycle dates, and
    the nominal-calendar helpers below. Rejects bools (``True``/``False``
    are technically ``int`` in Python, but are never valid years) and
    any non-integer (e.g. a string year) rather than letting a stdlib
    function like ``calendar.monthrange`` raise a confusing
    ``TypeError`` on it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise FuturesConfigurationError(f"{context} year must be an integer, got {value!r}")
    if value < _MIN_REASONABLE_YEAR or value > _MAX_REASONABLE_YEAR:
        raise FuturesConfigurationError(f"{context} year out of reasonable range: {value}")
    return value


def _require_nonempty_str(value: object, *, field_name: str, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuturesConfigurationError(f"{context}: '{field_name}' must be a non-empty string")
    return value.strip()


def _coerce_root_symbol(value: object, *, context: str) -> str:
    """Normalize a root symbol to its canonical form: stripped and upper-cased.

    Rejects anything that isn't a non-empty string (including None,
    numbers, etc.) with a clear domain error rather than letting a
    downstream AttributeError/TypeError leak out.
    """
    if not isinstance(value, str) or not value.strip():
        raise FuturesConfigurationError(f"{context}: root symbol must be a non-empty string, got {value!r}")
    return value.strip().upper()


def _coerce_contract_month(value: object) -> ContractMonth:
    """Normalize ``value`` (a ``ContractMonth`` or a plain int) to a canonical ``ContractMonth``.

    Raises :class:`InvalidContractMonthError` for anything else,
    including bools and non-quarterly integers (e.g. January).
    """
    if isinstance(value, ContractMonth):
        return value
    if isinstance(value, bool):
        raise InvalidContractMonthError(
            f"Contract month must be a quarterly month (3, 6, 9, or 12), got bool {value!r}"
        )
    if isinstance(value, int):
        try:
            return ContractMonth(value)
        except ValueError as exc:
            raise InvalidContractMonthError(
                "Contract month must be one of 3 (March), 6 (June), 9 (September), "
                f"12 (December); got {value!r}"
            ) from exc
    raise InvalidContractMonthError(
        f"Contract month must be a ContractMonth or int, got {type(value).__name__}"
    )


def _require_tick_count(value: object) -> int:
    """Validate that ``value`` is a true integer tick count.

    NQ/MNQ outright tick counts are discrete, so fractional values
    (``1.5``, ``Decimal("1.5")``) are rejected. Bools are rejected even
    though Python treats them as ints. Negative integers ARE valid --
    this validates "is an integer," not "is positive" -- signed
    price/P&L movements are a legitimate use case.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTickCountError(
            f"Tick count must be an integer, got {value!r} ({type(value).__name__})"
        )
    return value


def _coerce_point_value(value: object) -> Decimal:
    """Coerce a caller-supplied point/price value to a finite ``Decimal``.

    This is the single, centralized point-value validator used by both
    :meth:`FuturesInstrument.points_to_ticks` and
    :meth:`FuturesInstrument.points_to_dollars`. Preserves the existing
    caller convenience of passing a ``Decimal``, ``int``, ``float``, or
    numeric ``str`` (e.g. ``Decimal("0.25")``, ``1``, ``1.25``,
    ``"0.25"``, ``-10``), but rejects ``bool``, ``None``, non-numeric
    strings, and any non-finite value (``NaN``, ``Infinity``,
    ``-Infinity``, including their ``float`` forms) with
    :class:`InvalidPointValueError` -- never ``decimal.InvalidOperation``
    or a bare ``ValueError``. Never silently rounds.
    """
    if isinstance(value, bool):
        raise InvalidPointValueError(f"Point value must be numeric, got bool {value!r}")
    if isinstance(value, Decimal):
        coerced = value
    elif isinstance(value, (int, float, str)):
        try:
            coerced = Decimal(str(value))
        except InvalidOperation as exc:
            raise InvalidPointValueError(f"Point value is not a valid number: {value!r}") from exc
    else:
        raise InvalidPointValueError(
            f"Point value must be numeric (Decimal, int, float, or numeric str), got "
            f"{type(value).__name__}"
        )

    if not coerced.is_finite():
        raise InvalidPointValueError(f"Point value must be finite, got {coerced}")
    return coerced


def _require_plain_date(value: object, *, context: str) -> date:
    """Require an exact ``datetime.date`` -- never a ``datetime`` (which
    subclasses ``date``, so a plain ``isinstance(value, date)`` check
    would silently accept one) and never any other value.

    This is the single, centralized "date-only API" validator shared
    by :class:`ContractCycleDates`, :func:`nominal_customary_roll`,
    and ``app.futures.roll``'s ``trade_date`` parameters. Accepting a
    ``datetime`` where a plain ``date`` is required would either
    silently discard its time-of-day (surprising) or -- as the exact
    bug this fixes -- produce a cross-type ``date``/``datetime``
    comparison that raises a raw ``TypeError``.
    """
    if isinstance(value, datetime) or type(value) is not date:
        raise InvalidDateError(
            f"{context} expects a plain date (not a datetime or other value), got {type(value).__name__}"
        )
    return value


# -- Pure nominal-calendar helpers -------------------------------------------
#
# Moved here from app.futures.calendar (which re-exports them for backward
# compatibility) so that ContractCycleDates.__post_init__ below can enforce
# the exact customary-roll invariant regardless of how a ContractCycleDates
# is constructed -- app.futures.calendar imports from this module already,
# so defining them here (rather than there) avoids a circular import.


def nominal_third_friday(year: int, month: int) -> date:
    """Pure, deterministic calculation of a quarter month's third Friday.

    This is the normal quarterly-expiration rule used as a fallback
    when no official CME calendar entry exists for (year, month). It
    does NOT account for exchange holidays -- see
    :class:`CycleDateSource`.

    ``year`` must be a true (non-bool) int in a reasonable range, and
    ``month`` must be one of Olive's quarterly months (3, 6, 9, 12);
    this function intentionally does not compute a "third Friday" for
    arbitrary calendar months, since no internal futures contract may
    be created for a non-quarter month in this domain. Both are
    validated before any date arithmetic is attempted, so a malformed
    caller input (e.g. a bool or a string year) never leaks a raw
    ``TypeError`` from the stdlib ``calendar`` module.
    """
    year = _require_year(year, context="nominal_third_friday")
    month = require_quarterly_month(month)
    _, days_in_month = _stdlib_calendar.monthrange(year, month)
    fridays = [d for d in range(1, days_in_month + 1) if date(year, month, d).weekday() == 4]
    if len(fridays) < 3:
        # Not reachable for any real Gregorian month, but fail loudly
        # rather than silently returning a wrong date if it ever is.
        raise CycleDateError(f"{year}-{month:02d} does not contain three Fridays")
    return date(year, month, fridays[2])


def nominal_customary_roll(nominal_expiration: date) -> date:
    """The customary roll date: the Monday of the same week as ``nominal_expiration``.

    CME's customary U.S. equity-index roll convention is "the Monday
    prior to the third Friday of the expiration month" -- i.e. the
    Monday of that same calendar week.

    ``nominal_expiration`` must be the NOMINAL third Friday itself (as
    returned by :func:`nominal_third_friday`), not an OFFICIAL
    expiration date that may have shifted off that Friday due to an
    exchange holiday (e.g. June 2026's official Thursday expiration).
    Passing anything other than an actual Friday raises
    :class:`CycleDateError` rather than silently computing a
    nonsensical "roll date" that might not even be a Monday.

    ``nominal_expiration`` must be a plain ``date`` -- a ``datetime``
    is deliberately rejected (via :class:`InvalidDateError`, raised by
    the shared :func:`_require_plain_date` validator) even though it
    subclasses ``date``: this is a date-domain helper, and silently
    accepting a ``datetime`` previously produced a nonsensical
    ``datetime``-typed "roll date" (with a stray time-of-day carried
    through the subtraction) instead of a plain ``date``.
    """
    nominal_expiration = _require_plain_date(nominal_expiration, context="nominal_customary_roll")
    if nominal_expiration.weekday() != 4:
        raise CycleDateError(
            f"nominal_customary_roll expects a NOMINAL third-Friday expiration date; got "
            f"{nominal_expiration} ({nominal_expiration.strftime('%A')}). If you have an OFFICIAL "
            "expiration date instead, compute the nominal Friday first via "
            "nominal_third_friday(year, month) and pass that here."
        )
    # Friday (weekday 4) minus 4 days lands on the Monday (weekday 0) of the same week.
    return nominal_expiration - timedelta(days=4)


@dataclass(frozen=True)
class FuturesContract:
    """One specific quarterly futures contract (e.g. NQ December 2026).

    Internal identity is unambiguous (full root symbol + four-digit
    year + numeric month) precisely so abbreviated years never create
    cross-decade ambiguity. ``display_code`` produces the customary
    single-digit-year vendor-style code (e.g. ``NQZ6``) for display
    purposes only.

    Invariants enforced by ``__post_init__`` (true for every
    successfully constructed instance, regardless of caller):

    - ``root_symbol`` is a canonical (stripped, upper-cased) non-empty string.
    - ``year`` is a true (non-bool) int within a reasonable range.
    - ``month`` is always a ``ContractMonth`` member -- a raw int such
      as ``12`` is normalized to ``ContractMonth.DECEMBER``; anything
      that is not a valid quarterly month (e.g. ``1`` for January)
      raises :class:`InvalidContractMonthError` rather than being
      silently accepted.
    """

    root_symbol: str
    year: int
    month: ContractMonth

    def __post_init__(self) -> None:
        # frozen=True normally blocks attribute assignment; object.__setattr__
        # is the documented escape hatch for normalizing fields exactly once,
        # here, during construction.
        object.__setattr__(self, "root_symbol", _coerce_root_symbol(self.root_symbol, context="FuturesContract"))
        object.__setattr__(self, "year", _require_year(self.year, context="FuturesContract"))
        object.__setattr__(self, "month", _coerce_contract_month(self.month))

    @property
    def identity(self) -> str:
        """Unambiguous internal identity, e.g. ``NQ-2026-12``."""
        return f"{self.root_symbol}-{self.year:04d}-{int(self.month):02d}"

    @property
    def month_code(self) -> str:
        """The single-letter CME month code, e.g. ``Z`` for December."""
        return CME_QUARTERLY_MONTH_CODES[self.month]

    @property
    def display_code(self) -> str:
        """Customary single-digit-year vendor-style code, e.g. ``NQZ6``.

        Uses only the last digit of the year, matching common vendor
        display conventions. This is a DISPLAY format, not a parseable
        unambiguous identity -- use ``identity`` for that.
        """
        return f"{self.root_symbol}{self.month_code}{self.year % 10}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.display_code


@dataclass(frozen=True)
class FuturesInstrument:
    """A tradable futures product definition (e.g. NQ or MNQ).

    All monetary/economic fields use :class:`decimal.Decimal` to avoid
    floating-point artifacts in tick/price/dollar conversions that
    later phases (backtesting, paper trading) will rely on.

    This object validates and normalizes its own inputs regardless of
    how it was constructed -- the JSON registry loader
    (``app.futures.registry``) performs *additional* configuration-file
    checks (e.g. rejecting JSON numbers in favor of strings for
    economics fields), but it is not the only way a
    ``FuturesInstrument`` can come into existence, so the invariants
    below hold unconditionally:

    - ``root_symbol`` is canonical (stripped, upper-cased).
    - ``display_name`` / ``exchange`` / ``underlying`` / ``currency``
      are non-empty strings.
    - ``multiplier`` / ``tick_size`` / ``tick_value`` are always
      finite, strictly positive ``Decimal`` values -- ``NaN``,
      ``Infinity``, and ``-Infinity`` are rejected with
      :class:`FuturesConfigurationError`, never a raw
      ``decimal.InvalidOperation`` (which Python raises if a
      non-finite ``Decimal`` is compared with ``<=`` directly).
    - ``settlement_type`` is always a ``SettlementType`` member (a
      recognized string value is coerced; anything else is rejected).
    - ``contract_months`` is always a tuple of canonical ``ContractMonth``
      members (raw ints are normalized the same way ``FuturesContract``
      normalizes its own ``month``).

    Note: this class defines what a *generic* futures instrument looks
    like. It does not by itself restrict Olive to trading only NQ/MNQ,
    nor does it check that a given instrument actually matches Olive's
    canonical NQ/MNQ financial specification -- that Phase 2 production
    policy is enforced separately by
    ``app.futures.validation.validate_olive_tradable_registry`` against
    the *loaded registry's* roots and instruments, not by this class.
    ``app.health`` consumes that validator's result; it does not
    perform this check itself. See ``app.futures.registry``'s module
    docstring for the generic-registry-vs-tradable-universe distinction.
    """

    root_symbol: str
    display_name: str
    exchange: str
    underlying: str
    currency: str
    multiplier: Decimal
    tick_size: Decimal
    tick_value: Decimal
    settlement_type: SettlementType
    contract_months: tuple[ContractMonth, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "root_symbol", _coerce_root_symbol(self.root_symbol, context="FuturesInstrument")
        )
        object.__setattr__(
            self,
            "display_name",
            _require_nonempty_str(self.display_name, field_name="display_name", context=self.root_symbol),
        )
        object.__setattr__(
            self, "exchange", _require_nonempty_str(self.exchange, field_name="exchange", context=self.root_symbol)
        )
        object.__setattr__(
            self,
            "underlying",
            _require_nonempty_str(self.underlying, field_name="underlying", context=self.root_symbol),
        )
        object.__setattr__(
            self, "currency", _require_nonempty_str(self.currency, field_name="currency", context=self.root_symbol)
        )

        # Order matters: `.is_finite()` is checked BEFORE any `<=`
        # comparison. Comparing a non-finite Decimal (NaN in particular)
        # with `<=` raises decimal.InvalidOperation directly -- `or`
        # short-circuits so that comparison is never reached once
        # `not value.is_finite()` is already True.
        if not isinstance(self.multiplier, Decimal) or not self.multiplier.is_finite() or self.multiplier <= 0:
            raise FuturesConfigurationError(
                f"{self.root_symbol}: multiplier must be a finite, positive Decimal, got {self.multiplier!r}"
            )
        if not isinstance(self.tick_size, Decimal) or not self.tick_size.is_finite() or self.tick_size <= 0:
            raise FuturesConfigurationError(
                f"{self.root_symbol}: tick_size must be a finite, positive Decimal, got {self.tick_size!r}"
            )
        if not isinstance(self.tick_value, Decimal) or not self.tick_value.is_finite() or self.tick_value <= 0:
            raise FuturesConfigurationError(
                f"{self.root_symbol}: tick_value must be a finite, positive Decimal, got {self.tick_value!r}"
            )

        # Each economics field is individually finite (checked above), but
        # multiplying two extreme-but-finite Decimals (e.g. 1E+999999 each)
        # can still overflow the active Decimal arithmetic context and
        # raise decimal.Overflow directly -- never let that escape as a
        # raw stdlib exception.
        try:
            derived_tick_value = self.tick_size * self.multiplier
            tick_value_matches = derived_tick_value == self.tick_value
        except DecimalException as exc:
            raise FuturesConfigurationError(
                f"{self.root_symbol}: tick_size ({self.tick_size}) * multiplier ({self.multiplier}) "
                f"could not be computed ({type(exc).__name__}) -- economics values are too extreme "
                "to validate"
            ) from exc
        if not tick_value_matches:
            raise FuturesConfigurationError(
                f"{self.root_symbol}: configured tick_value {self.tick_value} does not match "
                f"tick_size ({self.tick_size}) * multiplier ({self.multiplier}) = {derived_tick_value}"
            )

        settlement = self.settlement_type
        if isinstance(settlement, str) and not isinstance(settlement, SettlementType):
            try:
                settlement = SettlementType(settlement)
            except ValueError as exc:
                valid = ", ".join(member.value for member in SettlementType)
                raise FuturesConfigurationError(
                    f"{self.root_symbol}: unknown settlement_type {settlement!r}. Must be one of: {valid}"
                ) from exc
        if not isinstance(settlement, SettlementType):
            raise FuturesConfigurationError(
                f"{self.root_symbol}: settlement_type must be a SettlementType, got {type(settlement).__name__}"
            )
        object.__setattr__(self, "settlement_type", settlement)

        # Validate the CONTAINER itself before iterating it. `bool` and
        # `int` are not iterable at all (iterating `True` raises a raw
        # TypeError); a `str`/`Mapping` iterates but not over usable
        # per-month values. Only an actual list/tuple of individual
        # month values is acceptable here.
        if isinstance(self.contract_months, (bool, str, bytes)) or not isinstance(
            self.contract_months, (list, tuple)
        ):
            raise FuturesConfigurationError(
                f"{self.root_symbol}: contract_months must be a list or tuple of contract months, "
                f"got {type(self.contract_months).__name__}"
            )
        if not self.contract_months:
            raise FuturesConfigurationError(f"{self.root_symbol}: at least one contract month is required")
        normalized_months = tuple(_coerce_contract_month(m) for m in self.contract_months)
        if len(set(normalized_months)) != len(normalized_months):
            raise FuturesConfigurationError(f"{self.root_symbol}: duplicate contract months in configuration")
        object.__setattr__(self, "contract_months", normalized_months)

    # -- Contract construction -------------------------------------------------

    def quarter_contract(self, year: int, month: ContractMonth) -> FuturesContract:
        """Build a :class:`FuturesContract` for this instrument.

        Raises :class:`InvalidContractMonthError` if ``month`` is not
        one of this instrument's supported quarterly contract months
        (or is not a valid quarterly month at all).
        """
        month = _coerce_contract_month(month)
        if month not in self.contract_months:
            raise InvalidContractMonthError(
                f"{self.root_symbol} does not trade contract month {month.name} ({int(month)})"
            )
        return FuturesContract(root_symbol=self.root_symbol, year=year, month=month)

    # -- Tick / price / dollar economics ---------------------------------------

    def points_to_ticks(self, points: Decimal) -> int:
        """Convert a price move in index points to an integer tick count.

        Raises :class:`InvalidPointValueError` if ``points`` is not a
        finite numeric value (rejects bool, ``None``, non-numeric
        strings, ``NaN``, ``Infinity``, ``-Infinity``), and
        :class:`TickAlignmentError` if ``points`` does not align
        exactly to this instrument's tick size; Olive never silently
        rounds price/tick conversions.
        """
        points = _coerce_point_value(points)
        try:
            ratio = points / self.tick_size
            aligned = ratio == ratio.to_integral_value()
        except DecimalException as exc:
            raise InvalidPointValueError(
                f"Point value {points} could not be converted to ticks ({type(exc).__name__}) -- "
                "value is too extreme for this instrument's tick size"
            ) from exc
        if not aligned:
            raise TickAlignmentError(
                f"{points} points is not aligned to {self.root_symbol}'s tick size of {self.tick_size}"
            )
        return int(ratio)

    def ticks_to_points(self, ticks: int) -> Decimal:
        """Convert an integer tick count to a price move in index points.

        Raises :class:`InvalidTickCountError` if ``ticks`` is not a true
        integer (fractional values and bools are rejected; negative
        integers are valid).
        """
        ticks = _require_tick_count(ticks)
        try:
            return self.tick_size * Decimal(ticks)
        except DecimalException as exc:
            raise InvalidTickCountError(
                f"Tick count {ticks} could not be converted to points ({type(exc).__name__}) -- "
                "value is too extreme to compute"
            ) from exc

    def points_to_dollars(self, points: Decimal) -> Decimal:
        """Convert a price move in index points to a dollar value.

        Raises :class:`InvalidPointValueError` if ``points`` is not a
        finite numeric value, for the same reason as
        :meth:`points_to_ticks`.
        """
        points = _coerce_point_value(points)
        try:
            return points * self.multiplier
        except DecimalException as exc:
            raise InvalidPointValueError(
                f"Point value {points} could not be converted to dollars ({type(exc).__name__}) -- "
                "value is too extreme to compute"
            ) from exc

    def ticks_to_dollars(self, ticks: int) -> Decimal:
        """Convert an integer tick count to a dollar value.

        Raises :class:`InvalidTickCountError` if ``ticks`` is not a true
        integer (fractional values and bools are rejected; negative
        integers are valid).
        """
        ticks = _require_tick_count(ticks)
        try:
            return Decimal(ticks) * self.tick_value
        except DecimalException as exc:
            raise InvalidTickCountError(
                f"Tick count {ticks} could not be converted to dollars ({type(exc).__name__}) -- "
                "value is too extreme to compute"
            ) from exc


@dataclass(frozen=True)
class ContractCycleDates:
    """The expiration and roll dates for one quarterly contract cycle.

    ``__post_init__`` enforces that this object can never represent an
    internally-impossible contract cycle: ``expiration`` and ``roll``
    must actually fall within the declared ``year``/``month``, ``roll``
    must be strictly before ``expiration``, and ``month`` must be one
    of Olive's quarterly months. Note that ``expiration`` is
    deliberately NOT required to fall on a Friday -- exchange holidays
    can (and, per the June 2026 exception, do) move the actual
    expiration off the nominal third Friday.

    Phase 2.2 hardening: ``roll`` must be the exact customary roll
    Monday for this cycle's NOMINAL third Friday -- i.e.
    ``nominal_customary_roll(nominal_third_friday(year, month))`` --
    not merely "some Monday before expiration." This is deliberately
    anchored to the NOMINAL Friday, not the (possibly
    holiday-shifted) OFFICIAL ``expiration``: for June 2026, the
    official expiration is Thursday 2026-06-18, but the customary roll
    (2026-06-15) is still computed from the nominal Friday
    (2026-06-19), not from the official Thursday. Deriving the roll by
    blindly subtracting four days from ``expiration`` would therefore
    be wrong for that cycle.

    Phase 2.3 hardening: ``expiration`` and ``roll`` must be *plain*
    ``date`` objects -- a ``datetime`` (which subclasses ``date``) is
    rejected rather than silently accepted, because comparing a plain
    ``date`` against a ``datetime`` directly (e.g. ``self.roll >=
    self.expiration`` below) raises a raw, confusing ``TypeError`` if
    the two are mixed.
    """

    year: int
    month: int
    expiration: date
    roll: date
    source: CycleDateSource

    def __post_init__(self) -> None:
        year = _require_year(self.year, context="ContractCycleDates")
        month = require_quarterly_month(self.month)

        expiration = _require_plain_date(self.expiration, context="ContractCycleDates.expiration")
        roll = _require_plain_date(self.roll, context="ContractCycleDates.roll")
        object.__setattr__(self, "expiration", expiration)
        object.__setattr__(self, "roll", roll)

        if self.expiration.year != year or self.expiration.month != month:
            raise CycleDateError(
                f"ContractCycleDates expiration {self.expiration} does not belong to the declared "
                f"contract cycle {year}-{month:02d}"
            )
        if self.roll.year != year or self.roll.month != month:
            raise CycleDateError(
                f"ContractCycleDates roll date {self.roll} does not belong to the declared "
                f"contract cycle {year}-{month:02d}"
            )
        if self.roll >= self.expiration:
            raise CycleDateError(
                f"ContractCycleDates roll date {self.roll} must be before expiration date {self.expiration}"
            )

        expected_roll = nominal_customary_roll(nominal_third_friday(year, month))
        if self.roll != expected_roll:
            raise CycleDateError(
                f"ContractCycleDates roll date {self.roll} is not the customary roll Monday for the "
                f"nominal third Friday of {year}-{month:02d} (expected {expected_roll}) -- the "
                "customary U.S. equity-index roll convention is always anchored to the NOMINAL "
                "third-Friday week, even in a cycle (like June 2026) where the OFFICIAL expiration "
                "itself has shifted off that nominal Friday due to an exchange holiday."
            )

        if not isinstance(self.source, CycleDateSource):
            raise CycleDateError(
                f"ContractCycleDates.source must be a CycleDateSource, got {type(self.source).__name__}"
            )

        object.__setattr__(self, "year", year)
        object.__setattr__(self, "month", month)


# -- Shared public-API object validators -------------------------------------
#
# Phase 2.3 hardening: every public function across the futures domain that
# takes one of these domain objects as a parameter (app.futures.contracts,
# app.futures.roll, app.futures.sessions) validates it with one of these
# three helpers, so a caller's mistake (e.g. passing a plain string where a
# FuturesInstrument was expected) raises a predictable domain error instead
# of an AttributeError the first time the function touches one of the
# object's attributes. Defined here, after the three classes themselves,
# so the isinstance checks reference already-defined types.


def _require_instrument(value: object, *, context: str) -> FuturesInstrument:
    if not isinstance(value, FuturesInstrument):
        raise InvalidInstrumentError(
            f"{context} expects a FuturesInstrument, got {type(value).__name__}"
        )
    return value


def _require_contract(value: object, *, context: str) -> FuturesContract:
    if not isinstance(value, FuturesContract):
        raise InvalidContractError(
            f"{context} expects a FuturesContract, got {type(value).__name__}"
        )
    return value


def _require_cycle_dates(value: object, *, context: str) -> ContractCycleDates:
    if not isinstance(value, ContractCycleDates):
        raise CycleDateError(
            f"{context} expects a ContractCycleDates, got {type(value).__name__}"
        )
    return value
