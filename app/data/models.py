"""Core typed domain objects for Olive AI's historical market-data layer.

Defines the historical-data error hierarchy, the supported OHLCV
timeframe vocabulary (with its mapping to Databento's schema names),
the data-label vocabulary (so historical data can never be confused
with live/delayed/simulated/demo data), and the two central immutable
domain objects: :class:`HistoricalBarRequest` (what Olive is asking
for) and :class:`HistoricalBar` (one normalized, validated bar of
Olive's own historical record).

This module performs no I/O and talks to no provider. It defines types
and validates values passed to it, exactly as
``app.futures.models`` does for the futures domain -- every dataclass
here validates and normalizes its own inputs in ``__post_init__``
rather than trusting a caller to have gone through a "nicer" entry
point first (a service, a provider adapter, a storage read). After
successful construction, the invariants documented on each class hold
unconditionally.

Architectural note (applying the Phase 2 "generic structural validity
vs. Olive production correctness" distinction to this new subsystem --
see CLAUDE.md): the classes in this module answer only "is this a
well-formed historical-data object?" -- e.g. a :class:`HistoricalBar`
with a well-formed but completely invented root symbol like ``"ZZ"``
is perfectly valid by this module's own standard. They have no opinion
about whether ``"ZZ"`` is actually part of Olive's production tradable
universe. That separate question -- "is this contract one of Olive's
required NQ/MNQ production instruments?" -- is answered by
``app.data.validation.require_olive_tradable_contract``, which
delegates to the *existing* Phase 2 production-validation layer
(``app.futures.validation``) rather than duplicating any canonical
NQ/MNQ fact here.

Phase 2.5 lesson applied: this module's one public helper for
accepting a caller-supplied :class:`HistoricalBarRequest` --
``_require_historical_bar_request`` -- validates its parameter's type
before anything downstream touches its attributes, exactly like
``app.futures.models._require_instrument`` /
``app.futures.registry._require_registry`` do. A Phase 2.4-style gap
(a brand-new public boundary skipping this discipline) is the single
most expensive mistake this project has made twice already; Phase 3
does not repeat it a third time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from enum import Enum
from typing import Optional

from app.futures.models import FuturesContract, FuturesDomainError, _require_contract

# -- Error hierarchy ----------------------------------------------------


class HistoricalDataError(Exception):
    """Base class for all Olive historical-market-data errors.

    Public historical-data functions should raise a subclass of this
    (never a bare ``ValueError``/``KeyError``/``TypeError``/
    ``AttributeError``/``decimal.InvalidOperation``/``OSError``/stdlib
    exception) for any input or provider/storage condition a caller
    could reasonably encounter. This is a separate error root from
    ``app.futures.models.FuturesDomainError`` -- this is a new
    subsystem, not an extension of the futures domain -- but the two
    are deliberately composed, not confused: a futures-domain
    violation (e.g. a contract that fails Olive's production
    validation) is translated at this package's boundary into a
    ``HistoricalDataError`` subclass rather than leaking the futures
    error type into callers that only expect to catch this hierarchy.
    """


class InvalidHistoricalRequestError(HistoricalDataError):
    """Raised when a value that should be a well-formed
    :class:`HistoricalBarRequest` is not one, or when request
    construction is attempted with invalid field values."""


class InvalidHistoricalBarError(HistoricalDataError):
    """Raised when a value cannot form, or does not satisfy the
    invariants of, a well-formed :class:`HistoricalBar`."""


class HistoricalPriceTickMisalignedError(InvalidHistoricalBarError):
    """Raised when a provider-supplied price is not an exact integer
    multiple of the instrument's tick size.

    Olive never silently rounds a misaligned price onto the nearest
    tick -- a misaligned price is evidence of either a unit mismatch
    (e.g. a provider returning prices scaled differently than
    expected) or genuine data corruption, and either way must be
    rejected/flagged rather than quietly "corrected."
    """


class NotOliveTradableContractError(HistoricalDataError):
    """Raised when a structurally valid :class:`FuturesContract` is not
    one of Olive's required PRODUCTION tradable instruments (exactly
    ``{"NQ", "MNQ"}``, matching Olive's canonical Phase 2 financial
    specification) -- the historical-data-layer analogue of
    ``app.futures.validation.ProductionDomainIntegrityError``, raised
    by ``app.data.validation.require_olive_tradable_contract`` (which
    delegates the actual check to that existing Phase 2 layer).
    """


class HistoricalBarContractMismatchError(HistoricalDataError):
    """Raised when a bar's contract/timeframe identity does not match
    the request that is supposed to have produced it, or when a
    provider's own reported symbol does not match the contract Olive
    actually requested -- Olive never stores a bar under the wrong
    contract identity."""


class HistoricalBarProductionEconomicsMismatchError(HistoricalBarContractMismatchError):
    """Raised when a returned bar's ``tick_size`` does not match
    Olive's PRODUCTION-validated :class:`~app.futures.models.FuturesInstrument`
    for the requested contract's root (Phase 3.2).

    This is the historical-data-layer instance of the Phase 2 "generic
    structural validity vs. Olive production correctness" distinction
    (see CLAUDE.md): a bar can be perfectly self-consistent on its own
    (its own ``__post_init__`` already guarantees ``high_ticks *
    tick_size`` etc. form sensible prices) while still using the WRONG
    tick size for the actual NQ/MNQ instrument Olive trades in
    production. A provider adapter, or storage data, is never trusted
    on self-consistency alone -- it must agree with the production
    instrument Olive already validated before any provider call."""


class HistoricalBarProviderProvenanceMismatchError(HistoricalBarContractMismatchError):
    """Raised when a returned bar's own ``provider`` or
    ``provider_raw_symbol`` does not match the provider actually
    servicing this request, or the exact raw symbol Olive derived for
    the requested contract (Phase 3.2).

    Defense in depth at the service boundary against a buggy (or
    malicious) provider adapter returning bars attributed to the wrong
    provider/symbol even though every other field is individually
    well-formed."""


class HistoricalBarOutOfRangeError(HistoricalDataError):
    """Raised when a bar's timestamp falls outside the half-open
    ``[start, end)`` interval of the request that produced it."""


class HistoricalBarConflictError(HistoricalDataError):
    """Raised when two bars share the same canonical key (contract +
    timeframe + ``ts_event``) but disagree on OHLCV content. Olive
    never silently picks a winner (first/last/highest-volume/...) --
    conflicting history is a data-integrity problem to surface, not
    paper over."""


class HistoricalStorageError(HistoricalDataError):
    """Raised for a local Parquet storage failure (I/O, path safety,
    malformed on-disk data, schema-version mismatch)."""


class HistoricalStorageIntegrityError(HistoricalStorageError):
    """Raised when newly-fetched bars conflict with bars already
    durably stored for the same canonical key. Mirrors
    :class:`HistoricalBarConflictError` at the storage boundary."""


class HistoricalProviderError(HistoricalDataError):
    """Base class for historical-data-provider failures, translated at
    the provider-adapter boundary from whatever vendor-specific
    exception a real provider client raises.

    Phase 3.1 hardening: NEVER includes the raw vendor exception's own
    message text (which independent review demonstrated CAN itself
    contain the configured API key, e.g. Databento's documented
    invalid-Basic-auth error shape -- ``"Invalid username in Basic
    auth ('<key>')"``). Every subclass raised by
    ``app.data.providers.databento`` carries only a fixed, generic,
    pre-written message describing the failure CATEGORY (classified
    internally from the vendor exception's status code/message text,
    which is inspected but never echoed), and is raised with the
    vendor exception's chain severed (``from None``) so that even a
    full traceback print can never surface the original vendor text."""


class InvalidHistoricalServiceConfigurationError(HistoricalDataError):
    """Raised when a constructor-time argument to
    ``app.data.service.HistoricalDataService`` or
    ``app.data.providers.factory.build_historical_provider`` is not the
    expected Olive type (a malformed ``provider``/``store``/
    ``registry``/``calendar``/``settings``). Olive-owned, never a raw
    ``TypeError`` -- continuing the standing public-boundary discipline
    (every new public constructor validates its own parameter types
    from the start) to these Phase 3 constructors as well."""


class ProviderNotConfiguredError(HistoricalProviderError):
    """Raised by :class:`app.data.providers.unconfigured.UnconfiguredHistoricalProvider`
    for every operation -- Olive has no real provider selected (or a
    selected provider is missing required configuration, such as an
    API key or an installed client package)."""


class ProviderAuthenticationError(HistoricalProviderError):
    """Raised when the configured provider rejects Olive's credentials."""


class ProviderPermissionError(HistoricalProviderError):
    """Raised when the configured provider rejects the request as
    outside Olive's subscription/entitlement (not a credentials
    problem -- the key is valid, but it is not entitled to this
    dataset/schema/symbol)."""


class ProviderRateLimitError(HistoricalProviderError):
    """Raised when the configured provider reports Olive has exceeded
    its request rate."""


class ProviderTimeoutError(HistoricalProviderError):
    """Raised when a provider request does not complete in time."""


class ProviderUnavailableError(HistoricalProviderError):
    """Raised for any other provider-side failure: an unreachable
    service, an invalid symbol/schema the provider itself rejected, a
    malformed/undecodable response, or data genuinely unavailable for
    the requested window (distinct from a *valid, empty* response --
    see :data:`HistoricalFetchStatus.SUCCESS_EMPTY`)."""


class ProviderResponseIdentityError(HistoricalProviderError):
    """Raised when a provider's own response identifies itself as a
    different contract/symbol than the one Olive explicitly
    requested. Olive never trusts provider response identity without
    checking it -- see docs/historical_data.md "Exact contract /
    symbology policy"."""


class CostEstimationFailedError(HistoricalProviderError):
    """Raised when the configured provider could not produce a cost
    estimate for a request. Olive never proceeds to a paid fetch when
    this happens -- see ``app.data.service.HistoricalDataService``.

    Phase 3.1 note: this also covers a MALFORMED cost response (``NaN``/
    ``Infinity``/negative/non-numeric/bool) -- a provider returning
    nonsense for a cost estimate is a provider-side failure, not a
    caller-input problem, and must be caught by
    ``HistoricalDataService``'s existing cost-estimation except clause
    rather than escaping as an ``InvalidHistoricalRequestError``.
    """


class ProviderDataError(HistoricalProviderError):
    """Raised when a provider's own returned row data fails Olive's
    bar-construction/normalization invariants (a tick-misaligned
    price, invalid volume, malformed timestamp, inconsistent OHLC,
    ...). This is an EXPECTED provider-data-quality failure -- never a
    programming bug -- and is translated at the Databento adapter
    boundary from the underlying structural ``HistoricalDataError`` so
    that ``HistoricalDataService.fetch_and_store`` (which only expects
    ``HistoricalProviderError`` around a provider's ``fetch_bars``
    call) can turn it into a structured ``FAILED`` result instead of
    letting it escape as an uncaught exception."""


class ProviderSymbologyError(HistoricalProviderError):
    """Raised when Databento's free symbology resolution cannot
    confirm that the raw symbol Olive is about to request actually
    identifies the SAME full-year contract Olive asked for.

    Databento's raw symbols reuse single decade digits (``NQZ6`` is
    the raw symbol for December contracts in 2016, 2026, 2036, ...).
    Checking the response's own ``symbol`` field is not sufficient to
    prevent this: a stale/misdirected request for an old contract-year
    date range could return rows that legitimately self-report
    ``symbol="NQZ6"`` while actually belonging to a completely
    different contract than the one Olive's full-year
    ``FuturesContract`` identity means. This error is raised BEFORE any
    paid ``timeseries.get_range`` call whenever symbology resolution is
    missing, ambiguous, partial, or does not match the contract's own
    month -- see ``docs/historical_data.md`` for the full policy."""


# -- Supported timeframe vocabulary --------------------------------------


class HistoricalTimeframe(str, Enum):
    """Olive's supported historical OHLCV aggregation timeframes.

    Deliberately limited to the four Databento OHLCV schemas Phase 3
    implements -- ``MBO``/``MBP-1``/``MBP-10``/order-book
    reconstruction are explicitly out of scope (see
    docs/historical_data.md).
    """

    ONE_SECOND = "ONE_SECOND"
    ONE_MINUTE = "ONE_MINUTE"
    ONE_HOUR = "ONE_HOUR"
    ONE_DAY = "ONE_DAY"

    @property
    def databento_schema(self) -> str:
        """The exact Databento OHLCV schema name for this timeframe.

        Also used, deliberately, as this timeframe's on-disk storage
        segment name (``ohlcv-1m`` rather than ``ONE_MINUTE``) -- see
        docs/historical_data.md "Parquet layout" for the rationale.
        """
        return _TIMEFRAME_TO_DATABENTO_SCHEMA[self]


_TIMEFRAME_TO_DATABENTO_SCHEMA: dict[HistoricalTimeframe, str] = {
    HistoricalTimeframe.ONE_SECOND: "ohlcv-1s",
    HistoricalTimeframe.ONE_MINUTE: "ohlcv-1m",
    HistoricalTimeframe.ONE_HOUR: "ohlcv-1h",
    HistoricalTimeframe.ONE_DAY: "ohlcv-1d",
}


def _coerce_timeframe(value: object) -> HistoricalTimeframe:
    if isinstance(value, HistoricalTimeframe):
        return value
    if isinstance(value, str):
        try:
            return HistoricalTimeframe(value)
        except ValueError:
            pass
    raise InvalidHistoricalRequestError(
        f"timeframe must be a HistoricalTimeframe, got {value!r} ({type(value).__name__})"
    )


# -- Data-label vocabulary ------------------------------------------------


class DataLabel(str, Enum):
    """The provenance label attached to every piece of Olive market
    data. Phase 3 constructs ``HISTORICAL`` only -- the remaining
    members exist so later phases (live/replay/demo data) have a
    stable, already-reviewed vocabulary, exactly mirroring
    ``app.config.DataMode``'s role for Phase 1. :class:`HistoricalBar`
    rejects every value except ``HISTORICAL`` at construction time --
    see its ``__post_init__``.
    """

    HISTORICAL = "HISTORICAL"
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    SIMULATED = "SIMULATED"
    DEMO = "DEMO"


OLIVE_HISTORICAL_BAR_SCHEMA_VERSION = 1
"""Storage schema/version identifier for :class:`HistoricalBar`'s on-disk
Parquet representation (see ``app.data.storage``). Bumped whenever the
set or meaning of stored columns changes, so future code can detect an
incompatible stored format rather than silently misreading it."""


ARROW_INT64_MAX = 2**63 - 1
"""Phase 3.3 §13: the maximum value representable by a signed 64-bit
integer -- the exact type Olive's Parquet storage schema declares for
``open_ticks``/``high_ticks``/``low_ticks``/``close_ticks``/``volume``
(see ``app.data.storage._arrow_schema``'s ``pa.int64()`` columns). A
Python ``int`` has no such bound (``2**80`` constructs without error),
so :class:`HistoricalBar` must enforce this bound itself in
``__post_init__`` -- deferring it to pyarrow serialization would let a
structurally "valid" bar fail only much later, deep inside a storage
write, with a raw/unclear error. This is a STORAGE REPRESENTATION
bound (what Olive's own declared schema can actually hold), not an
economic assumption -- deliberately the full signed-int64 range, never
a smaller arbitrary economic cap Olive invented."""


# -- Request model ---------------------------------------------------------


@dataclass(frozen=True)
class HistoricalBarRequest:
    """A typed, immutable request for one contract's historical bars
    over a half-open UTC time interval.

    Invariants enforced by ``__post_init__`` (true for every
    successfully constructed instance, regardless of caller):

    - ``contract`` is an actual :class:`FuturesContract` (Phase 2
      object), never a raw symbol string -- Olive derives provider
      symbology internally from this, callers never supply one.
    - ``timeframe`` is always a :class:`HistoricalTimeframe` member.
    - ``start``/``end`` are always timezone-AWARE ``datetime.datetime``
      instances (never a naive datetime, and never a plain ``date`` --
      both are rejected, not silently accepted and misinterpreted).
      Both are normalized to UTC internally regardless of the timezone
      supplied (e.g. ``America/Chicago`` input is converted, not
      rejected).
    - ``start < end`` strictly -- an empty or inverted interval is
      rejected rather than silently producing zero results.

    ``start`` is inclusive and ``end`` is exclusive, matching how
    ``app.data.validation``/``app.data.storage`` interpret the
    interval everywhere else in this package.

    Deliberately does NOT validate that ``contract``'s root is one of
    Olive's required PRODUCTION tradable instruments -- that is Olive
    production correctness, not generic structural validity, and is
    enforced separately by
    ``app.data.validation.require_olive_tradable_contract``, which
    ``HistoricalDataService`` calls before any provider interaction.
    See this module's docstring for why that split matters.
    """

    contract: FuturesContract
    timeframe: HistoricalTimeframe
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        # Translate a futures-domain validation failure (a different
        # error hierarchy root -- app.futures.models.FuturesDomainError)
        # into this package's own HistoricalDataError hierarchy. Never
        # let a FuturesDomainError leak through this boundary: a caller
        # of HistoricalBarRequest only expects to catch HistoricalDataError.
        try:
            contract = _require_contract(self.contract, context="HistoricalBarRequest")
        except FuturesDomainError as exc:
            raise InvalidHistoricalRequestError(f"HistoricalBarRequest.contract is invalid: {exc}") from exc
        timeframe = _coerce_timeframe(self.timeframe)
        start = _require_aware_datetime(self.start, field_name="start")
        end = _require_aware_datetime(self.end, field_name="end")

        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)

        if not start_utc < end_utc:
            raise InvalidHistoricalRequestError(
                f"HistoricalBarRequest requires start < end (start is inclusive, end is "
                f"exclusive); got start={start_utc.isoformat()}, end={end_utc.isoformat()}"
            )

        object.__setattr__(self, "contract", contract)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "start", start_utc)
        object.__setattr__(self, "end", end_utc)

    @property
    def root_symbol(self) -> str:
        return self.contract.root_symbol


def _require_aware_datetime(value: object, *, field_name: str) -> datetime:
    """Require a timezone-AWARE ``datetime.datetime`` -- never a plain
    ``date`` (which has no notion of time-of-day or timezone, and
    would silently mean midnight if accepted), never a naive
    ``datetime`` (Olive never guesses what timezone a naive timestamp
    means -- mirrors ``app.futures.sessions``'s existing policy for
    exactly this reason).
    """
    # bool is an int subtype but is never a date/datetime subtype, so no
    # separate bool exclusion is needed here (unlike numeric fields).
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise InvalidHistoricalRequestError(
                f"HistoricalBarRequest.{field_name} must be a timezone-AWARE datetime; got a "
                f"naive datetime ({value!r}). Olive never guesses which timezone a naive "
                f"timestamp means."
            )
        return value
    if isinstance(value, date):
        raise InvalidHistoricalRequestError(
            f"HistoricalBarRequest.{field_name} must be a datetime.datetime, got a plain "
            f"datetime.date ({value!r}), which has no time-of-day or timezone."
        )
    raise InvalidHistoricalRequestError(
        f"HistoricalBarRequest.{field_name} must be a timezone-aware datetime.datetime, got "
        f"{value!r} ({type(value).__name__})"
    )


def _require_historical_bar_request(value: object, *, context: str) -> HistoricalBarRequest:
    """Shared public-API object validator, mirroring
    ``app.futures.models._require_instrument`` /
    ``app.futures.registry._require_registry`` /
    ``app.futures.calendar._require_cycle_calendar`` exactly. Every
    public function in this package that accepts a
    :class:`HistoricalBarRequest` parameter uses this -- a caller's
    mistake (``None``, a raw dict, a different Olive domain object)
    raises a predictable domain error instead of an ``AttributeError``
    the first time the function touches one of the request's
    attributes. See this module's docstring: this is the Phase 2.5
    lesson, applied from the start this time.
    """
    if not isinstance(value, HistoricalBarRequest):
        raise InvalidHistoricalRequestError(
            f"{context} expects a HistoricalBarRequest, got {type(value).__name__}"
        )
    return value


# -- Bar model ---------------------------------------------------------


def _require_true_int(value: object, *, field_name: str) -> int:
    """Require a true ``int`` -- rejects ``bool`` (an ``int`` subtype
    in Python) and any fractional/float/Decimal/string value."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidHistoricalBarError(
            f"HistoricalBar.{field_name} must be a true int (not bool, float, or str); got "
            f"{value!r} ({type(value).__name__})"
        )
    return value


def _require_finite_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise InvalidHistoricalBarError(
            f"HistoricalBar.{field_name} must be a Decimal, not a bool; got {value!r}"
        )
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, (int, str)):
        try:
            candidate = Decimal(value)
        except DecimalException as exc:
            raise InvalidHistoricalBarError(
                f"HistoricalBar.{field_name} could not be parsed as a Decimal: {value!r}"
            ) from exc
    else:
        raise InvalidHistoricalBarError(
            f"HistoricalBar.{field_name} must be a Decimal, int, or numeric str; got "
            f"{value!r} ({type(value).__name__})"
        )
    if not candidate.is_finite():
        raise InvalidHistoricalBarError(
            f"HistoricalBar.{field_name} must be finite (not NaN/Infinity); got {candidate}"
        )
    return candidate


@dataclass(frozen=True)
class HistoricalBar:
    """One normalized, validated historical OHLCV bar of Olive's own
    historical record.

    Prices are stored as signed integer tick counts
    (``open_ticks``/``high_ticks``/``low_ticks``/``close_ticks``), not
    ``float`` or even bare ``Decimal`` -- see
    :attr:`open`/:attr:`high`/:attr:`low`/:attr:`close` for the
    ``Decimal`` price, computed exactly from ``ticks * tick_size``,
    and :meth:`from_decimal_prices` for the one sanctioned way to
    build a bar from provider-supplied Decimal prices (which rejects,
    rather than rounds, a price that is not an exact multiple of
    ``tick_size``).

    Invariants enforced by ``__post_init__`` (true for every
    successfully constructed instance, regardless of caller or
    construction path -- direct construction is exactly as safe as
    going through a provider adapter or a storage read):

    - ``root_symbol`` is a non-empty string.
    - ``contract_year`` is a true (non-bool) int.
    - ``contract_month`` is a true (non-bool) int in ``{3, 6, 9, 12}``
      (Olive's quarterly cycle -- see ``app.futures.models``).
    - ``timeframe`` is a :class:`HistoricalTimeframe` member.
    - ``ts_event`` is a timezone-aware ``datetime`` whose ``tzinfo`` is
      exactly UTC (never naive, never a non-UTC timezone, never a
      plain ``date``) -- Olive's canonical event timestamps are always
      UTC; see docs/historical_data.md "Timezone policy".
    - ``open_ticks``/``high_ticks``/``low_ticks``/``close_ticks`` are
      true ints, each strictly positive (a non-positive price is
      never valid for a real futures trade).
    - ``high_ticks`` is the maximum and ``low_ticks`` is the minimum of
      the four tick values (the standard OHLC ordering invariant).
    - ``volume`` is a true, non-negative int.
    - ``tick_size`` is a finite, strictly positive ``Decimal``.
    - ``data_label`` is exactly :data:`DataLabel.HISTORICAL` -- Phase 3
      has no code path capable of constructing a bar labeled
      ``LIVE``/``DELAYED``/``SIMULATED``/``DEMO``.
    - ``provider``/``dataset``/``provider_raw_symbol`` are non-empty
      strings.

    Deliberately does NOT validate that ``root_symbol`` is one of
    Olive's required PRODUCTION tradable instruments, nor that this
    bar's contract identity matches any particular request -- those
    are Olive production correctness and cross-bar/cross-request
    concerns respectively, both handled by ``app.data.validation``
    (see ``require_olive_tradable_contract`` and
    ``normalize_and_validate_bars``).
    """

    root_symbol: str
    contract_year: int
    contract_month: int
    timeframe: HistoricalTimeframe
    ts_event: datetime
    open_ticks: int
    high_ticks: int
    low_ticks: int
    close_ticks: int
    volume: int
    tick_size: Decimal
    data_label: DataLabel
    provider: str
    dataset: str
    provider_raw_symbol: str
    provider_instrument_id: Optional[str] = None
    schema_version: int = OLIVE_HISTORICAL_BAR_SCHEMA_VERSION

    _QUARTERLY_MONTHS = frozenset({3, 6, 9, 12})

    def __post_init__(self) -> None:
        root_symbol = self.root_symbol
        if not isinstance(root_symbol, str) or not root_symbol.strip():
            raise InvalidHistoricalBarError(
                f"HistoricalBar.root_symbol must be a non-empty string, got {root_symbol!r}"
            )
        root_symbol = root_symbol.strip().upper()

        contract_year = _require_true_int(self.contract_year, field_name="contract_year")
        if not (1900 <= contract_year <= 2300):
            raise InvalidHistoricalBarError(
                f"HistoricalBar.contract_year out of reasonable range: {contract_year}"
            )

        contract_month = _require_true_int(self.contract_month, field_name="contract_month")
        if contract_month not in self._QUARTERLY_MONTHS:
            raise InvalidHistoricalBarError(
                f"HistoricalBar.contract_month must be one of {sorted(self._QUARTERLY_MONTHS)}, "
                f"got {contract_month}"
            )

        timeframe = _coerce_timeframe(self.timeframe)

        ts_event = self.ts_event
        if not isinstance(ts_event, datetime):
            raise InvalidHistoricalBarError(
                f"HistoricalBar.ts_event must be a datetime.datetime, got "
                f"{ts_event!r} ({type(ts_event).__name__})"
            )
        if ts_event.tzinfo is None or ts_event.tzinfo.utcoffset(ts_event) is None:
            raise InvalidHistoricalBarError(
                f"HistoricalBar.ts_event must be timezone-aware, got a naive datetime: {ts_event!r}"
            )
        if ts_event.utcoffset() != timezone.utc.utcoffset(None):
            raise InvalidHistoricalBarError(
                f"HistoricalBar.ts_event must be UTC (offset {timezone.utc.utcoffset(None)}), "
                f"got offset {ts_event.utcoffset()} ({ts_event!r}). Normalize to UTC before "
                f"constructing a HistoricalBar."
            )

        open_ticks = _require_true_int(self.open_ticks, field_name="open_ticks")
        high_ticks = _require_true_int(self.high_ticks, field_name="high_ticks")
        low_ticks = _require_true_int(self.low_ticks, field_name="low_ticks")
        close_ticks = _require_true_int(self.close_ticks, field_name="close_ticks")

        for name, ticks in (
            ("open_ticks", open_ticks),
            ("high_ticks", high_ticks),
            ("low_ticks", low_ticks),
            ("close_ticks", close_ticks),
        ):
            if ticks <= 0:
                raise InvalidHistoricalBarError(
                    f"HistoricalBar.{name} must be strictly positive (a real traded price is "
                    f"never zero or negative); got {ticks}"
                )
            # Phase 3.3 §13 fix: a Python int has no upper bound, but
            # Olive's OWN Parquet storage schema declares this column
            # as a signed 64-bit integer -- a tick count beyond that
            # range can never be durably stored, so it must be
            # rejected here at construction, not discovered later as a
            # pyarrow serialization failure.
            if ticks > ARROW_INT64_MAX:
                raise InvalidHistoricalBarError(
                    f"HistoricalBar.{name} exceeds the maximum value representable by Olive's "
                    f"signed-64-bit Parquet storage schema (ARROW_INT64_MAX={ARROW_INT64_MAX}); "
                    f"got {ticks}"
                )

        if high_ticks != max(open_ticks, high_ticks, low_ticks, close_ticks):
            raise InvalidHistoricalBarError(
                f"HistoricalBar.high_ticks ({high_ticks}) must be >= open ({open_ticks}), "
                f"close ({close_ticks}), and low ({low_ticks})"
            )
        if low_ticks != min(open_ticks, high_ticks, low_ticks, close_ticks):
            raise InvalidHistoricalBarError(
                f"HistoricalBar.low_ticks ({low_ticks}) must be <= open ({open_ticks}), "
                f"close ({close_ticks}), and high ({high_ticks})"
            )

        volume = _require_true_int(self.volume, field_name="volume")
        if volume < 0:
            raise InvalidHistoricalBarError(
                f"HistoricalBar.volume must not be negative; got {volume}"
            )
        # Phase 3.3 §13 fix: same storage-representability bound as
        # the tick-count fields above -- volume is also a pa.int64()
        # column.
        if volume > ARROW_INT64_MAX:
            raise InvalidHistoricalBarError(
                f"HistoricalBar.volume exceeds the maximum value representable by Olive's "
                f"signed-64-bit Parquet storage schema (ARROW_INT64_MAX={ARROW_INT64_MAX}); "
                f"got {volume}"
            )

        tick_size = _require_finite_decimal(self.tick_size, field_name="tick_size")
        if tick_size <= 0:
            raise InvalidHistoricalBarError(
                f"HistoricalBar.tick_size must be strictly positive; got {tick_size}"
            )

        # Phase 3.2 §29 fix: a bar can be individually well-formed (all
        # four required invariants above hold) and yet still make its
        # own `open`/`high`/`low`/`close` properties raise a raw
        # decimal.Overflow/InvalidOperation the first time something
        # actually computes `ticks * tick_size` -- those properties are
        # lazy and were never exercised at construction time. Compute
        # (and discard) every price once here so a bar that cannot
        # produce a finite price fails closed at construction, with an
        # Olive-owned error, rather than lazily and unpredictably later.
        try:
            _ = open_ticks * tick_size
            _ = high_ticks * tick_size
            _ = low_ticks * tick_size
            _ = close_ticks * tick_size
        except DecimalException as exc:
            raise InvalidHistoricalBarError(
                f"HistoricalBar price computation (ticks * tick_size) failed for "
                f"open={open_ticks}, high={high_ticks}, low={low_ticks}, close={close_ticks}, "
                f"tick_size={tick_size}: {exc}"
            ) from exc

        if not isinstance(self.data_label, DataLabel):
            raise InvalidHistoricalBarError(
                f"HistoricalBar.data_label must be a DataLabel, got "
                f"{self.data_label!r} ({type(self.data_label).__name__})"
            )
        if self.data_label is not DataLabel.HISTORICAL:
            raise InvalidHistoricalBarError(
                f"HistoricalBar may only be labeled DataLabel.HISTORICAL in Phase 3; got "
                f"{self.data_label!r}. Olive has no code path that may present historical "
                f"market data as LIVE/DELAYED/SIMULATED/DEMO."
            )

        for field_name in ("provider", "dataset", "provider_raw_symbol"):
            field_value = getattr(self, field_name)
            if not isinstance(field_value, str) or not field_value.strip():
                raise InvalidHistoricalBarError(
                    f"HistoricalBar.{field_name} must be a non-empty string, got {field_value!r}"
                )

        # Phase 3.2 §7 fix: when present, provider identity must be
        # MEANINGFUL -- an empty or whitespace-only string is not a
        # real instrument ID and must never be accepted as if it were
        # one (the previous check only required a string OR None,
        # letting "" through silently).
        if self.provider_instrument_id is not None:
            if not isinstance(self.provider_instrument_id, str) or not self.provider_instrument_id.strip():
                raise InvalidHistoricalBarError(
                    "HistoricalBar.provider_instrument_id must be None or a non-empty, "
                    f"non-whitespace-only string; got {self.provider_instrument_id!r}"
                )

        schema_version = _require_true_int(self.schema_version, field_name="schema_version")
        if schema_version <= 0:
            raise InvalidHistoricalBarError(
                f"HistoricalBar.schema_version must be a positive int; got {schema_version}"
            )

        object.__setattr__(self, "root_symbol", root_symbol)
        object.__setattr__(self, "contract_year", contract_year)
        object.__setattr__(self, "contract_month", contract_month)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "open_ticks", open_ticks)
        object.__setattr__(self, "high_ticks", high_ticks)
        object.__setattr__(self, "low_ticks", low_ticks)
        object.__setattr__(self, "close_ticks", close_ticks)
        object.__setattr__(self, "volume", volume)
        object.__setattr__(self, "tick_size", tick_size)
        object.__setattr__(self, "schema_version", schema_version)

    @property
    def open(self) -> Decimal:
        return self.open_ticks * self.tick_size

    @property
    def high(self) -> Decimal:
        return self.high_ticks * self.tick_size

    @property
    def low(self) -> Decimal:
        return self.low_ticks * self.tick_size

    @property
    def close(self) -> Decimal:
        return self.close_ticks * self.tick_size

    @property
    def contract_identity(self) -> str:
        """Olive's canonical, unambiguous contract identity, e.g.
        ``"NQ-2026-12"`` -- matches ``FuturesContract.identity``."""
        return f"{self.root_symbol}-{self.contract_year:04d}-{self.contract_month:02d}"

    @property
    def canonical_key(self) -> tuple[str, int, int, HistoricalTimeframe, datetime]:
        """The deterministic de-duplication/conflict-detection key:
        contract + timeframe + event timestamp. Two bars sharing this
        key must have identical OHLCV content, or storing both is a
        conflict -- see ``app.data.validation``/``app.data.storage``.
        """
        return (self.root_symbol, self.contract_year, self.contract_month, self.timeframe, self.ts_event)

    def conflicts_with(self, other: "HistoricalBar") -> bool:
        """Whether ``other`` shares this bar's canonical key but
        disagrees on OHLCV content or PROVENANCE (price/volume/label/
        provider/dataset/raw-symbol/provider-instrument-id/schema
        version). Two bars with the same key and IDENTICAL content are
        safe to de-duplicate; anything else is a conflict Olive must
        fail closed on rather than silently resolve.

        Phase 3.1 correction: this comparison previously omitted
        ``provider_instrument_id`` and ``schema_version`` -- two bars
        sharing a canonical key and identical OHLCV but DIFFERENT
        provider instrument IDs (which can legitimately happen if a
        provider's symbol-to-instrument mapping changed) or different
        schema versions were silently treated as an identical
        duplicate and de-duplicated, discarding the disagreement
        instead of surfacing it. Persisted history must never let a
        conflicting provider identity or schema version silently
        disappear through de-duplication.
        """
        # Phase 3.2 §6 fix: this is a PUBLIC domain method -- a
        # malformed `other` (None, a str, a different Olive domain
        # object, ...) must fail closed with an Olive-owned error, not
        # leak a raw AttributeError the moment `.canonical_key` is
        # touched. Applies the standing public-boundary QA discipline
        # to this method too.
        if not isinstance(other, HistoricalBar):
            raise InvalidHistoricalBarError(
                f"HistoricalBar.conflicts_with expects another HistoricalBar, got "
                f"{other!r} ({type(other).__name__})"
            )

        if self.canonical_key != other.canonical_key:
            return False
        return (
            self.open_ticks,
            self.high_ticks,
            self.low_ticks,
            self.close_ticks,
            self.volume,
            self.tick_size,
            self.data_label,
            self.provider,
            self.dataset,
            self.provider_raw_symbol,
            self.provider_instrument_id,
            self.schema_version,
        ) != (
            other.open_ticks,
            other.high_ticks,
            other.low_ticks,
            other.close_ticks,
            other.volume,
            other.tick_size,
            other.data_label,
            other.provider,
            other.dataset,
            other.provider_raw_symbol,
            other.provider_instrument_id,
            other.schema_version,
        )

    @classmethod
    def from_decimal_prices(
        cls,
        *,
        contract: FuturesContract,
        timeframe: object,
        ts_event: datetime,
        open_price: object,
        high_price: object,
        low_price: object,
        close_price: object,
        volume: object,
        tick_size: object,
        provider: str,
        dataset: str,
        provider_raw_symbol: str,
        provider_instrument_id: Optional[str] = None,
        data_label: DataLabel = DataLabel.HISTORICAL,
    ) -> "HistoricalBar":
        """Build a :class:`HistoricalBar` from provider-supplied
        Decimal (or Decimal-convertible) prices, converting to exact
        integer ticks.

        Rejects -- never rounds -- a price that is not an exact
        integer multiple of ``tick_size``: a provider price
        incompatible with the known contract tick size is flagged as
        :class:`HistoricalPriceTickMisalignedError`, not silently
        "corrected" onto the nearest tick. This is the one sanctioned
        way to turn raw provider decimal prices into an Olive
        historical bar; provider adapters should use this rather than
        hand-rolling tick arithmetic.
        """
        # Same boundary-translation requirement as HistoricalBarRequest
        # above: never let a raw FuturesDomainError leak through here.
        try:
            contract = _require_contract(contract, context="HistoricalBar.from_decimal_prices")
        except FuturesDomainError as exc:
            raise InvalidHistoricalBarError(f"HistoricalBar.from_decimal_prices contract is invalid: {exc}") from exc
        tick_size_decimal = _require_finite_decimal(tick_size, field_name="tick_size")
        if tick_size_decimal <= 0:
            raise InvalidHistoricalBarError(f"tick_size must be strictly positive; got {tick_size_decimal}")

        ticks = {}
        for field_name, raw_price in (
            ("open", open_price),
            ("high", high_price),
            ("low", low_price),
            ("close", close_price),
        ):
            price = _require_finite_decimal(raw_price, field_name=field_name)
            try:
                quotient = price / tick_size_decimal
            except DecimalException as exc:
                raise InvalidHistoricalBarError(
                    f"Could not divide {field_name} price {price} by tick_size {tick_size_decimal}"
                ) from exc
            if quotient != quotient.to_integral_value():
                raise HistoricalPriceTickMisalignedError(
                    f"{field_name} price {price} is not an exact multiple of tick_size "
                    f"{tick_size_decimal} (contract {contract.display_code}); refusing to round "
                    f"onto the nearest tick."
                )
            ticks[field_name] = int(quotient)

        return cls(
            root_symbol=contract.root_symbol,
            contract_year=contract.year,
            contract_month=int(contract.month),
            timeframe=timeframe,
            ts_event=ts_event,
            open_ticks=ticks["open"],
            high_ticks=ticks["high"],
            low_ticks=ticks["low"],
            close_ticks=ticks["close"],
            volume=volume,
            tick_size=tick_size_decimal,
            data_label=data_label,
            provider=provider,
            dataset=dataset,
            provider_raw_symbol=provider_raw_symbol,
            provider_instrument_id=provider_instrument_id,
        )


# -- Structured fetch result -------------------------------------------


class HistoricalFetchStatus(str, Enum):
    """Explicit status semantics for a historical-data fetch operation.

    Callers must never be forced to infer success/failure from
    ``None``, an empty list, or exceptions alone -- every outcome
    ``HistoricalDataService.fetch_and_store`` can produce has a named
    status here. A provider failure is never silently turned into an
    empty-success result, and a safety rejection is never confused
    with a genuine provider failure.
    """

    SUCCESS = "SUCCESS"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    REJECTED_INVALID_REQUEST = "REJECTED_INVALID_REQUEST"
    REJECTED_NOT_TRADABLE = "REJECTED_NOT_TRADABLE"
    REJECTED_NETWORK_DISABLED = "REJECTED_NETWORK_DISABLED"
    REJECTED_COST_LIMIT = "REJECTED_COST_LIMIT"
    REJECTED_COST_ESTIMATE_FAILED = "REJECTED_COST_ESTIMATE_FAILED"
    FAILED = "FAILED"

    @property
    def is_success(self) -> bool:
        return self in (HistoricalFetchStatus.SUCCESS, HistoricalFetchStatus.SUCCESS_EMPTY)


@dataclass(frozen=True)
class HistoricalFetchResult:
    """The structured outcome of one
    ``HistoricalDataService.fetch_and_store`` call.

    Phase 3.1 hardening: independent review constructed instances with
    ``bars=None``, ``estimated_cost_usd=NaN``, and
    ``new_records_stored=-1`` with no error -- an impossible result
    state for a domain object whose whole purpose is to be a trustworthy
    structured outcome. Every field is now validated in
    ``__post_init__``, exactly like every other Phase 2/3 domain object.
    """

    status: HistoricalFetchStatus
    message: str = ""
    bars: tuple[HistoricalBar, ...] = field(default_factory=tuple)
    estimated_cost_usd: Optional[Decimal] = None
    new_records_stored: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, HistoricalFetchStatus):
            raise InvalidHistoricalRequestError(
                f"HistoricalFetchResult.status must be a HistoricalFetchStatus, got "
                f"{self.status!r} ({type(self.status).__name__})"
            )
        if not isinstance(self.message, str):
            raise InvalidHistoricalRequestError(
                f"HistoricalFetchResult.message must be a str, got "
                f"{self.message!r} ({type(self.message).__name__})"
            )

        bars = self.bars
        if not isinstance(bars, (tuple, list)):
            raise InvalidHistoricalRequestError(
                f"HistoricalFetchResult.bars must be a tuple/list of HistoricalBar, got "
                f"{bars!r} ({type(bars).__name__})"
            )
        for bar in bars:
            if not isinstance(bar, HistoricalBar):
                raise InvalidHistoricalRequestError(
                    f"HistoricalFetchResult.bars must contain only HistoricalBar instances, "
                    f"got an element of type {type(bar).__name__}"
                )
        object.__setattr__(self, "bars", tuple(bars))

        if self.estimated_cost_usd is not None:
            if isinstance(self.estimated_cost_usd, bool) or not isinstance(self.estimated_cost_usd, Decimal):
                raise InvalidHistoricalRequestError(
                    f"HistoricalFetchResult.estimated_cost_usd must be a Decimal or None, got "
                    f"{self.estimated_cost_usd!r} ({type(self.estimated_cost_usd).__name__})"
                )
            if not self.estimated_cost_usd.is_finite():
                raise InvalidHistoricalRequestError(
                    f"HistoricalFetchResult.estimated_cost_usd must be finite (not NaN/Infinity); "
                    f"got {self.estimated_cost_usd}"
                )
            if self.estimated_cost_usd < 0:
                raise InvalidHistoricalRequestError(
                    f"HistoricalFetchResult.estimated_cost_usd must not be negative; got "
                    f"{self.estimated_cost_usd}"
                )

        if self.new_records_stored is not None:
            if isinstance(self.new_records_stored, bool) or not isinstance(self.new_records_stored, int):
                raise InvalidHistoricalRequestError(
                    f"HistoricalFetchResult.new_records_stored must be a true int or None, got "
                    f"{self.new_records_stored!r} ({type(self.new_records_stored).__name__})"
                )
            if self.new_records_stored < 0:
                raise InvalidHistoricalRequestError(
                    f"HistoricalFetchResult.new_records_stored must not be negative; got "
                    f"{self.new_records_stored}"
                )

        # Phase 3.2 §5 fix: per-field validation above does not catch
        # an impossible COMBINATION of otherwise-individually-valid
        # fields (e.g. status=SUCCESS_EMPTY with real bars attached, or
        # a rejection/NOT_CONFIGURED status claiming records were
        # stored). Cross-field consistency rules, by status:
        #
        # - SUCCESS_EMPTY: no bars were fetched/stored -- bars must be
        #   empty and new_records_stored must be 0 or unreported.
        # - SUCCESS: must represent an ACTUAL non-empty fetch/storage
        #   outcome -- at least one bar (the empty case is
        #   SUCCESS_EMPTY, never SUCCESS).
        # - every rejection status and NOT_CONFIGURED: no provider
        #   fetch/storage could legitimately have happened yet, so
        #   neither bars nor a positive stored-record count may be
        #   reported.
        # - FAILED is deliberately NOT constrained here: the existing
        #   storage-failure path intentionally returns normalized bars
        #   alongside status=FAILED for diagnostic context.
        if self.status is HistoricalFetchStatus.SUCCESS_EMPTY:
            if self.bars:
                raise InvalidHistoricalRequestError(
                    "HistoricalFetchResult with status SUCCESS_EMPTY must have an empty bars "
                    f"tuple; got {len(self.bars)} bar(s)."
                )
            if self.new_records_stored not in (None, 0):
                raise InvalidHistoricalRequestError(
                    "HistoricalFetchResult with status SUCCESS_EMPTY must have "
                    f"new_records_stored of 0 or None; got {self.new_records_stored!r}."
                )
        elif self.status is HistoricalFetchStatus.SUCCESS:
            if not self.bars:
                raise InvalidHistoricalRequestError(
                    "HistoricalFetchResult with status SUCCESS must report at least one bar "
                    "-- an actual successful fetch/storage outcome is never empty (see "
                    "SUCCESS_EMPTY for the legitimate empty case)."
                )
            # Phase 3.3 §16: HistoricalDataService always durably
            # stores on a genuine SUCCESS and always passes the real
            # HistoricalWriteResult.new_records count through --
            # new_records_stored being None on SUCCESS would mean
            # "unknown whether anything was stored," which never
            # legitimately happens for this status (per-field
            # validation above already guarantees that when NOT None,
            # it is a real non-negative int).
            if self.new_records_stored is None:
                raise InvalidHistoricalRequestError(
                    "HistoricalFetchResult with status SUCCESS must report a real "
                    "non-negative new_records_stored int (a successful fetch always "
                    "durably stores and reports a count); got None."
                )
        elif self.status in (
            HistoricalFetchStatus.NOT_CONFIGURED,
            HistoricalFetchStatus.REJECTED_INVALID_REQUEST,
            HistoricalFetchStatus.REJECTED_NOT_TRADABLE,
            HistoricalFetchStatus.REJECTED_NETWORK_DISABLED,
            HistoricalFetchStatus.REJECTED_COST_LIMIT,
            HistoricalFetchStatus.REJECTED_COST_ESTIMATE_FAILED,
        ):
            if self.bars:
                raise InvalidHistoricalRequestError(
                    f"HistoricalFetchResult with status {self.status.value} must not report "
                    f"any bars (no provider fetch/storage could legitimately have happened "
                    f"yet); got {len(self.bars)} bar(s)."
                )
            if self.new_records_stored not in (None, 0):
                raise InvalidHistoricalRequestError(
                    f"HistoricalFetchResult with status {self.status.value} must not claim a "
                    f"positive stored-record count; got "
                    f"new_records_stored={self.new_records_stored!r}."
                )
