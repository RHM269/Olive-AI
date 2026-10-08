"""Core typed domain objects for Olive AI's real-time market-data layer
(Phase 4).

Mirrors the design discipline ``app.data.models`` established for
historical data: every dataclass here validates and normalizes its own
inputs in ``__post_init__`` rather than trusting a caller to have gone
through a "nicer" entry point first (a service, a provider adapter).
After successful construction, the invariants documented on each class
hold unconditionally.

Design decisions, stated explicitly so they are not mistaken for
oversights:

- **Separate error hierarchy.** :class:`LiveDataError` is its own root,
  NOT a subclass of ``app.data.models.HistoricalDataError``. The two
  subsystems share a design pattern (a dedicated error root, specific
  subclasses for specific failure categories, never a leaked raw
  stdlib exception) but are otherwise independent: a caller that only
  expects historical errors must never have to also catch live errors,
  and vice versa. This also means Phase 4 adds nothing to the Phase 3
  error hierarchy -- no accepted Phase 1-3 module is modified to
  introduce this.
- **``DataLabel`` is REUSED, not duplicated.** ``app.data.models.DataLabel``
  already declares a ``LIVE`` member (reserved for exactly this phase
  since Phase 3). Every live event model below requires
  ``data_label is DataLabel.LIVE`` -- there is no code path in this
  module capable of constructing a live event object labeled anything
  else, which is what makes "no fake LIVE data" a type-level guarantee
  rather than a convention: the ONLY way to get a
  ``LiveTrade``/``LiveQuote``/``LiveBar`` is to construct one, and
  every one of those is unconditionally labeled LIVE.
- **Contract identity is a real ``FuturesContract``, not flattened
  fields.** Unlike ``HistoricalBar`` (which flattens contract identity
  into ``root_symbol``/``contract_year``/``contract_month`` for its
  Parquet storage schema), live events are never persisted by this
  phase -- there is no storage schema forcing flattening, so each live
  event simply holds the actual :class:`~app.futures.models.FuturesContract`
  object, which is simpler and loses nothing.
- **Subscription-level root check reuses, rather than duplicates,
  Olive's one canonical tradable-root constant.** Phase 2's "generic
  structural validity vs. Olive production correctness" split
  (see CLAUDE.md) means ``FuturesContract`` itself has no opinion on
  which roots are tradable, and historical data follows that same
  split by deferring the check entirely to
  ``app.data.validation.require_olive_tradable_contract`` at the
  service boundary. Phase 4's prompt explicitly requires the
  SUBSCRIPTION MODEL ITSELF to reject non-NQ/MNQ contracts, so
  :class:`LiveSubscriptionRequest` performs a lightweight membership
  check against ``app.futures.validation.REQUIRED_TRADABLE_ROOTS`` (the
  existing single source of truth for "which roots," reused verbatim,
  never redeclared) at construction time. This is a structural
  allow-list check only -- it does not duplicate NQ/MNQ's economic
  specification (tick size, multiplier, ...), which remains the
  service layer's job via ``require_olive_tradable_contract`` (see
  ``app.data.live_service.RealTimeDataService``), exactly mirroring
  the historical pattern for the deeper check.

This module performs no I/O and talks to no provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from enum import Enum
from typing import Optional

from app.futures.models import FuturesContract, FuturesDomainError, _require_contract
from app.futures.validation import REQUIRED_TRADABLE_ROOTS

from app.data.models import DataLabel, HistoricalTimeframe

# -- Error hierarchy ------------------------------------------------------


class LiveDataError(Exception):
    """Base class for all Olive real-time-market-data errors.

    Public live-data functions raise a subclass of this (never a bare
    ``ValueError``/``KeyError``/``TypeError``/``AttributeError``/stdlib
    exception) for any input or provider/connection condition a caller
    could reasonably encounter. A deliberately SEPARATE root from
    ``app.futures.models.FuturesDomainError`` and
    ``app.data.models.HistoricalDataError`` -- see this module's
    docstring.
    """


class InvalidLiveSubscriptionError(LiveDataError):
    """Raised when a value that should be a well-formed
    :class:`LiveSubscriptionRequest` is not one, or when subscription
    construction is attempted with invalid field values (empty
    contracts, a non-NQ/MNQ root, a duplicate contract, an empty or
    invalid event-type set, a duplicate event type, or a malformed
    element of either collection)."""


class InvalidLiveEventError(LiveDataError):
    """Raised when a value cannot form, or does not satisfy the
    invariants of, a well-formed :class:`LiveTrade`/:class:`LiveQuote`/
    :class:`LiveBar`."""


class LiveTickMisalignedError(InvalidLiveEventError):
    """Raised when a provider-supplied live price is not an exact
    integer multiple of the instrument's tick size. Mirrors
    ``app.data.models.HistoricalPriceTickMisalignedError`` -- Olive
    never silently rounds a misaligned live price onto the nearest
    tick."""


class InvalidLiveServiceConfigurationError(LiveDataError):
    """Raised when a constructor-time argument to
    ``app.data.live_service.RealTimeDataService`` or
    ``app.data.providers.live_factory.build_live_provider`` is not the
    expected Olive type. Mirrors
    ``app.data.models.InvalidHistoricalServiceConfigurationError``."""


class LiveProviderNotConfiguredError(LiveDataError):
    """Raised by
    ``app.data.providers.unconfigured_live.UnconfiguredLiveProvider``
    for every operation -- Olive has no real live provider selected
    (or a selected provider is missing required configuration, such as
    an API key or an installed client package)."""


class LiveNetworkDisabledError(LiveDataError):
    """Raised when a live connection is attempted while
    ``OLIVE_LIVE_NETWORK_ENABLED`` is false. Raised BEFORE any network
    call is made -- see ``app.data.live_service.RealTimeDataService.open_stream``."""


class LiveProviderError(LiveDataError):
    """Base class for live-provider-adapter failures, translated at
    the provider-adapter boundary from whatever vendor-specific
    exception a real provider client raises.

    Mirrors ``app.data.models.HistoricalProviderError``'s secret-safety
    policy EXACTLY: never includes a raw vendor exception's own message
    text (which can itself contain the configured API key), carries
    only a fixed, generic, pre-written message for the matched
    failure category, and is always raised with the vendor exception
    chain severed (``from None``)."""


class LiveProviderAuthenticationError(LiveProviderError):
    """Raised when the configured live provider rejects Olive's
    credentials. Never retried -- see
    ``app.data.providers.databento_live.ReconnectPolicy``."""


class LiveProviderPermissionError(LiveProviderError):
    """Raised when the configured live provider rejects a subscription
    as outside Olive's entitlement. Never retried."""


class LiveProviderRateLimitError(LiveProviderError):
    """Raised when the configured live provider reports Olive has
    exceeded its connection/subscription rate."""


class LiveProviderUnavailableError(LiveProviderError):
    """Raised for any other provider-side failure not covered by a
    more specific category (an unreachable service, a connection reset,
    a malformed/undecodable control message). This is the category
    treated as a TRANSIENT, reconnect-eligible failure unless a more
    specific (permanent) category applies."""


class LiveProviderShutdownError(LiveProviderError):
    """Raised by :meth:`~app.data.providers.databento_live.DatabentoLiveProvider.close`
    when the underlying client reports an exception while being
    stopped that is NOT one of a narrow, specifically recognized set
    of already-expected benign shutdown conditions (e.g. "already
    stopped," "connection already closed") -- added by the Phase 4.2
    correction, which found that unconditionally suppressing EVERY
    vendor-origin exception during shutdown (Phase 4's own earlier
    correction) was still too broad: a vendor-origin exception is not
    automatically a harmless one. The provider still reaches a
    coherent ``STOPPED`` state regardless of whether this is raised
    (enforced via ``finally``, not by this exception's own handling)."""


class LiveContractIdentityError(LiveDataError):
    """Raised when an incoming provider record cannot be tied
    unambiguously to one of the contracts Olive actually subscribed to
    -- an unmapped instrument ID, a record for an instrument Olive
    never subscribed to, or a mapping that contradicts an
    already-established mapping for the same instrument ID. Olive
    never guesses; an unknown or contradictory mapping fails closed and
    the record is rejected, never silently admitted into the
    stream."""


class LiveIdentityUnresolvedError(LiveContractIdentityError):
    """Raised when Olive cannot establish, via an authoritative
    point-in-time Databento symbology resolution, a confirmed full-
    year/month instrument identity for one of a subscription's
    contracts BEFORE a live connection is opened -- including when the
    resolution is missing, ambiguous, partial, or malformed for that
    contract's own calendar month, when the configured metadata client
    cannot perform the resolution at all, or when two distinct Olive
    contracts in the same subscription authoritatively resolve to the
    same provider instrument ID (a genuine identity collision). Also
    raised at RUNTIME if a live ``SymbolMappingMsg`` contradicts a
    contract's own pre-verified authoritative identity.

    A Databento raw futures symbol (e.g. ``"NQZ6"``) is NOT, by itself,
    sufficient proof of a contract's full year -- it encodes only the
    last digit of the contract year, so it is genuinely ambiguous
    between, for example, 2016/2026/2036 (see
    docs/realtime_data.md "Full-year contract identity proof"). Olive
    never infers a contract's identity from that trailing digit alone,
    and never yields a single market event for a contract whose
    identity has not been authoritatively confirmed this way."""


class LiveProviderDataError(LiveDataError):
    """Raised when a provider's own live record fails Olive's
    event-construction/normalization invariants (a tick-misaligned
    price, invalid size, malformed timestamp, inconsistent quote
    sides, ...). An EXPECTED provider-data-quality failure -- never a
    programming bug. Mirrors ``app.data.models.ProviderDataError``."""


class LiveStreamClosedError(LiveDataError):
    """Raised when an operation is attempted on a stream that has
    already been closed/stopped, or when a lifecycle transition is
    requested that is not valid from the stream's current
    :class:`ConnectionState`."""


class LiveReconnectExhaustedError(LiveProviderError):
    """Raised when the configured maximum number of reconnect attempts
    has been exhausted following a transient failure, and the stream
    could not be re-established."""


# -- Event-type / schema vocabulary --------------------------------------


class LiveEventType(str, Enum):
    """The real-time event categories Olive's Phase 4 subsystem
    normalizes. Deliberately limited to the three Databento schemas
    this phase implements -- full order-book (MBO) reconstruction and
    deeper book depth (MBP-10) are explicitly out of scope (see
    docs/realtime_data.md)."""

    TRADE = "TRADE"
    QUOTE = "QUOTE"
    BAR = "BAR"

    @property
    def databento_schema(self) -> str:
        """The exact Databento live schema name for this event type."""
        return _EVENT_TYPE_TO_DATABENTO_SCHEMA[self]


_EVENT_TYPE_TO_DATABENTO_SCHEMA: dict[LiveEventType, str] = {
    LiveEventType.TRADE: "trades",
    LiveEventType.QUOTE: "mbp-1",
    LiveEventType.BAR: "ohlcv-1s",
}


class ConnectionState(str, Enum):
    """The lifecycle state of a real-time market-data stream.

    Explicit named states rather than scattered booleans, per the
    Phase 4 prompt's own requirement -- every transition this build's
    shipped adapter (``app.data.providers.databento_live.DatabentoLiveProvider``)
    can actually reach is intentional and covered by a regression
    test. ``DEGRADED`` is part of this ABC-level contract but is not
    one of them: it is reserved for a future
    ``RealTimeMarketDataProvider`` implementation that can distinguish
    "still connected, but currently serving a degraded/partial feed"
    from ``RECONNECTING`` (actively tearing down and rebuilding the
    underlying client connection) -- mirroring how
    ``app.health.HealthState.DEGRADED`` is likewise declared ahead of
    any code path that produces it. ``events()`` on any provider
    accepts it as a valid state to keep consuming from, exactly like
    ``CONNECTED``, so a provider that does start using it needs no
    change to this ABC or to ``RealTimeDataService``.
    """

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


# -- Shared small validators ----------------------------------------------


def _require_true_int(value: object, *, field_name: str, errcls: type[LiveDataError] = InvalidLiveEventError) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise errcls(f"{field_name} must be a true int (not bool, float, or str); got {value!r} ({type(value).__name__})")
    return value


def _require_finite_decimal(
    value: object, *, field_name: str, errcls: type[LiveDataError] = InvalidLiveEventError
) -> Decimal:
    if isinstance(value, bool):
        raise errcls(f"{field_name} must be a Decimal, not a bool; got {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, (int, str)):
        try:
            candidate = Decimal(value)
        except DecimalException as exc:
            raise errcls(f"{field_name} could not be parsed as a Decimal: {value!r}") from exc
    else:
        raise errcls(f"{field_name} must be a Decimal, int, or numeric str; got {value!r} ({type(value).__name__})")
    if not candidate.is_finite():
        raise errcls(f"{field_name} must be finite (not NaN/Infinity); got {candidate}")
    return candidate


def _require_utc_datetime(
    value: object, *, field_name: str, errcls: type[LiveDataError] = InvalidLiveEventError
) -> datetime:
    """Require a timezone-aware ``datetime`` whose offset is exactly
    UTC -- never naive, never a non-UTC timezone. Mirrors
    ``HistoricalBar.ts_event``'s existing policy: Olive's canonical
    event/receive timestamps are always UTC, and normalizing happens at
    the provider-adapter boundary, before a live event model is ever
    constructed."""
    if not isinstance(value, datetime):
        raise errcls(f"{field_name} must be a datetime.datetime, got {value!r} ({type(value).__name__})")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise errcls(f"{field_name} must be timezone-aware, got a naive datetime: {value!r}")
    if value.utcoffset() != timezone.utc.utcoffset(None):
        raise errcls(
            f"{field_name} must be UTC (offset {timezone.utc.utcoffset(None)}), got offset "
            f"{value.utcoffset()} ({value!r}). Normalize to UTC before constructing a live event."
        )
    return value


def _require_optional_utc_datetime(
    value: object, *, field_name: str, errcls: type[LiveDataError] = InvalidLiveEventError
) -> Optional[datetime]:
    if value is None:
        return None
    return _require_utc_datetime(value, field_name=field_name, errcls=errcls)


def _require_nonempty_str(value: object, *, field_name: str, errcls: type[LiveDataError] = InvalidLiveEventError) -> str:
    if not isinstance(value, str) or not value.strip():
        raise errcls(f"{field_name} must be a non-empty string, got {value!r}")
    return value


def _require_optional_nonempty_str(
    value: object, *, field_name: str, errcls: type[LiveDataError] = InvalidLiveEventError
) -> Optional[str]:
    if value is None:
        return None
    return _require_nonempty_str(value, field_name=field_name, errcls=errcls)


def _require_optional_nonnegative_int(
    value: object, *, field_name: str, errcls: type[LiveDataError] = InvalidLiveEventError
) -> Optional[int]:
    if value is None:
        return None
    result = _require_true_int(value, field_name=field_name, errcls=errcls)
    if result < 0:
        raise errcls(f"{field_name} must not be negative; got {result}")
    return result


def _require_data_label_live(value: object, *, errcls: type[LiveDataError] = InvalidLiveEventError) -> DataLabel:
    if not isinstance(value, DataLabel):
        raise errcls(f"data_label must be a DataLabel, got {value!r} ({type(value).__name__})")
    if value is not DataLabel.LIVE:
        raise errcls(
            f"Live event models may only be labeled DataLabel.LIVE; got {value!r}. Olive has no "
            "code path that may present real-time market data as HISTORICAL/DELAYED/SIMULATED/DEMO."
        )
    return value


# -- Subscription model ----------------------------------------------------


@dataclass(frozen=True)
class LiveSubscriptionRequest:
    """A typed, immutable request to stream real-time events for one or
    more exact Olive contracts.

    Invariants enforced by ``__post_init__`` (true for every
    successfully constructed instance, regardless of caller):

    - ``contracts`` is a non-empty tuple of actual
      :class:`~app.futures.models.FuturesContract` objects -- never a
      raw symbol string, and never empty.
    - Every contract's root is one of Olive's required tradable roots
      (``{"NQ", "MNQ"}``, reused from
      ``app.futures.validation.REQUIRED_TRADABLE_ROOTS`` -- see this
      module's docstring). This is a structural allow-list check, not
      the deeper production-economics check
      ``require_olive_tradable_contract`` performs at the service
      boundary.
    - No two contracts in ``contracts`` share the same
      :attr:`~app.futures.models.FuturesContract.identity` -- a
      duplicate subscription to the exact same contract is
      semantically meaningless and rejected rather than silently
      accepted twice.
    - ``event_types`` is a non-empty tuple of distinct
      :class:`LiveEventType` members -- an empty event-type set (no
      schema to subscribe to at all) and a duplicate event type are
      both rejected.
    """

    contracts: tuple[FuturesContract, ...]
    event_types: tuple[LiveEventType, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.contracts, (tuple, list)):
            raise InvalidLiveSubscriptionError(
                f"LiveSubscriptionRequest.contracts must be a tuple/list of FuturesContract, got "
                f"{type(self.contracts).__name__}"
            )
        contracts_list = list(self.contracts)
        if not contracts_list:
            raise InvalidLiveSubscriptionError(
                "LiveSubscriptionRequest.contracts must not be empty -- a subscription for zero "
                "contracts is meaningless."
            )

        validated_contracts: list[FuturesContract] = []
        seen_identities: set[str] = set()
        for raw_contract in contracts_list:
            try:
                contract = _require_contract(raw_contract, context="LiveSubscriptionRequest")
            except FuturesDomainError as exc:
                raise InvalidLiveSubscriptionError(
                    f"LiveSubscriptionRequest.contracts contains an invalid contract: {exc}"
                ) from exc
            if contract.root_symbol not in REQUIRED_TRADABLE_ROOTS:
                raise InvalidLiveSubscriptionError(
                    f"{contract.root_symbol} is not part of Olive's tradable universe "
                    f"{sorted(REQUIRED_TRADABLE_ROOTS)}; real-time data may only be subscribed "
                    f"for NQ/MNQ contracts."
                )
            if contract.identity in seen_identities:
                raise InvalidLiveSubscriptionError(
                    f"LiveSubscriptionRequest.contracts contains a duplicate contract: "
                    f"{contract.identity}."
                )
            seen_identities.add(contract.identity)
            validated_contracts.append(contract)

        if not isinstance(self.event_types, (tuple, list)):
            raise InvalidLiveSubscriptionError(
                f"LiveSubscriptionRequest.event_types must be a tuple/list of LiveEventType, got "
                f"{type(self.event_types).__name__}"
            )
        event_types_list = list(self.event_types)
        if not event_types_list:
            raise InvalidLiveSubscriptionError(
                "LiveSubscriptionRequest.event_types must not be empty -- a subscription with no "
                "event type requests no data at all."
            )
        validated_event_types: list[LiveEventType] = []
        seen_event_types: set[LiveEventType] = set()
        for raw_event_type in event_types_list:
            if not isinstance(raw_event_type, LiveEventType):
                raise InvalidLiveSubscriptionError(
                    f"LiveSubscriptionRequest.event_types must contain only LiveEventType members, "
                    f"got {raw_event_type!r} ({type(raw_event_type).__name__})"
                )
            if raw_event_type in seen_event_types:
                raise InvalidLiveSubscriptionError(
                    f"LiveSubscriptionRequest.event_types contains a duplicate event type: "
                    f"{raw_event_type.value}."
                )
            seen_event_types.add(raw_event_type)
            validated_event_types.append(raw_event_type)

        object.__setattr__(self, "contracts", tuple(validated_contracts))
        object.__setattr__(self, "event_types", tuple(validated_event_types))

    @property
    def contract_identities(self) -> frozenset[str]:
        return frozenset(contract.identity for contract in self.contracts)


def _require_live_subscription_request(value: object, *, context: str) -> LiveSubscriptionRequest:
    """Shared public-API object validator, mirroring
    ``app.data.models._require_historical_bar_request``."""
    if not isinstance(value, LiveSubscriptionRequest):
        raise InvalidLiveSubscriptionError(
            f"{context} expects a LiveSubscriptionRequest, got {type(value).__name__}"
        )
    return value


# -- Normalized live event models ------------------------------------------


@dataclass(frozen=True)
class LiveTrade:
    """One normalized, validated real-time trade execution.

    Invariants enforced by ``__post_init__``:

    - ``contract`` is an actual :class:`FuturesContract`.
    - ``provider``/``dataset``/``provider_raw_symbol`` are non-empty
      strings.
    - ``ts_event`` (the exchange/matching-engine event timestamp) is a
      timezone-aware, UTC ``datetime``.
    - ``ts_recv`` (the provider's own receive timestamp), when present,
      is also timezone-aware UTC -- kept distinct from ``ts_event``;
      neither is ever used to silently overwrite the other.
    - ``olive_received_at`` (Olive's own receive wall-clock time) is
      timezone-aware UTC.
    - ``price`` is a finite, strictly positive ``Decimal`` (never a
      bare ``float`` -- see docs/realtime_data.md "Numeric integrity").
    - ``size`` is a true, strictly positive int (an executed trade is
      never zero-sized).
    - ``sequence``, when present, is a true, non-negative int --
      Olive never invents a sequence number; this is the provider's
      own value, preserved, never fabricated.
    - ``provider_instrument_id``, when present, is a non-empty string.
    - ``data_label`` is exactly :data:`DataLabel.LIVE`.
    """

    contract: FuturesContract
    provider: str
    dataset: str
    provider_raw_symbol: str
    ts_event: datetime
    price: Decimal
    size: int
    ts_recv: Optional[datetime] = None
    olive_received_at: Optional[datetime] = None
    provider_instrument_id: Optional[str] = None
    sequence: Optional[int] = None
    data_label: DataLabel = DataLabel.LIVE

    def __post_init__(self) -> None:
        try:
            contract = _require_contract(self.contract, context="LiveTrade")
        except FuturesDomainError as exc:
            raise InvalidLiveEventError(f"LiveTrade.contract is invalid: {exc}") from exc

        provider = _require_nonempty_str(self.provider, field_name="LiveTrade.provider")
        dataset = _require_nonempty_str(self.dataset, field_name="LiveTrade.dataset")
        provider_raw_symbol = _require_nonempty_str(
            self.provider_raw_symbol, field_name="LiveTrade.provider_raw_symbol"
        )
        ts_event = _require_utc_datetime(self.ts_event, field_name="LiveTrade.ts_event")
        ts_recv = _require_optional_utc_datetime(self.ts_recv, field_name="LiveTrade.ts_recv")
        olive_received_at = _require_optional_utc_datetime(
            self.olive_received_at, field_name="LiveTrade.olive_received_at"
        )

        price = _require_finite_decimal(self.price, field_name="LiveTrade.price")
        if price <= 0:
            raise InvalidLiveEventError(f"LiveTrade.price must be strictly positive; got {price}")

        size = _require_true_int(self.size, field_name="LiveTrade.size")
        if size <= 0:
            raise InvalidLiveEventError(f"LiveTrade.size must be strictly positive; got {size}")

        sequence = _require_optional_nonnegative_int(self.sequence, field_name="LiveTrade.sequence")
        provider_instrument_id = _require_optional_nonempty_str(
            self.provider_instrument_id, field_name="LiveTrade.provider_instrument_id"
        )
        data_label = _require_data_label_live(self.data_label)

        object.__setattr__(self, "contract", contract)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "dataset", dataset)
        object.__setattr__(self, "provider_raw_symbol", provider_raw_symbol)
        object.__setattr__(self, "ts_event", ts_event)
        object.__setattr__(self, "ts_recv", ts_recv)
        object.__setattr__(self, "olive_received_at", olive_received_at)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "provider_instrument_id", provider_instrument_id)
        object.__setattr__(self, "data_label", data_label)


@dataclass(frozen=True)
class LiveQuote:
    """One normalized, validated real-time top-of-book (MBP-1) quote.

    ``bid_price``/``bid_size`` and ``ask_price``/``ask_size`` are each
    independently optional (a real top-of-book can legitimately have
    only one side resting, e.g. immediately after a book-clearing
    event) -- but within a side, price and size are all-or-nothing:
    a price with no size, or a size with no price, is malformed and
    rejected.
    """

    contract: FuturesContract
    provider: str
    dataset: str
    provider_raw_symbol: str
    ts_event: datetime
    bid_price: Optional[Decimal] = None
    bid_size: Optional[int] = None
    ask_price: Optional[Decimal] = None
    ask_size: Optional[int] = None
    ts_recv: Optional[datetime] = None
    olive_received_at: Optional[datetime] = None
    provider_instrument_id: Optional[str] = None
    sequence: Optional[int] = None
    data_label: DataLabel = DataLabel.LIVE

    def _validate_side(self, *, price: object, size: object, side_name: str) -> tuple[Optional[Decimal], Optional[int]]:
        if price is None and size is None:
            return None, None
        if price is None or size is None:
            raise InvalidLiveEventError(
                f"LiveQuote.{side_name}_price and LiveQuote.{side_name}_size must both be "
                f"present or both be None; got price={price!r}, size={size!r}."
            )
        validated_price = _require_finite_decimal(price, field_name=f"LiveQuote.{side_name}_price")
        if validated_price <= 0:
            raise InvalidLiveEventError(
                f"LiveQuote.{side_name}_price must be strictly positive; got {validated_price}"
            )
        validated_size = _require_true_int(size, field_name=f"LiveQuote.{side_name}_size")
        if validated_size <= 0:
            raise InvalidLiveEventError(
                f"LiveQuote.{side_name}_size must be strictly positive; got {validated_size}"
            )
        return validated_price, validated_size

    def __post_init__(self) -> None:
        try:
            contract = _require_contract(self.contract, context="LiveQuote")
        except FuturesDomainError as exc:
            raise InvalidLiveEventError(f"LiveQuote.contract is invalid: {exc}") from exc

        provider = _require_nonempty_str(self.provider, field_name="LiveQuote.provider")
        dataset = _require_nonempty_str(self.dataset, field_name="LiveQuote.dataset")
        provider_raw_symbol = _require_nonempty_str(
            self.provider_raw_symbol, field_name="LiveQuote.provider_raw_symbol"
        )
        ts_event = _require_utc_datetime(self.ts_event, field_name="LiveQuote.ts_event")
        ts_recv = _require_optional_utc_datetime(self.ts_recv, field_name="LiveQuote.ts_recv")
        olive_received_at = _require_optional_utc_datetime(
            self.olive_received_at, field_name="LiveQuote.olive_received_at"
        )

        bid_price, bid_size = self._validate_side(price=self.bid_price, size=self.bid_size, side_name="bid")
        ask_price, ask_size = self._validate_side(price=self.ask_price, size=self.ask_size, side_name="ask")

        sequence = _require_optional_nonnegative_int(self.sequence, field_name="LiveQuote.sequence")
        provider_instrument_id = _require_optional_nonempty_str(
            self.provider_instrument_id, field_name="LiveQuote.provider_instrument_id"
        )
        data_label = _require_data_label_live(self.data_label)

        object.__setattr__(self, "contract", contract)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "dataset", dataset)
        object.__setattr__(self, "provider_raw_symbol", provider_raw_symbol)
        object.__setattr__(self, "ts_event", ts_event)
        object.__setattr__(self, "ts_recv", ts_recv)
        object.__setattr__(self, "olive_received_at", olive_received_at)
        object.__setattr__(self, "bid_price", bid_price)
        object.__setattr__(self, "bid_size", bid_size)
        object.__setattr__(self, "ask_price", ask_price)
        object.__setattr__(self, "ask_size", ask_size)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "provider_instrument_id", provider_instrument_id)
        object.__setattr__(self, "data_label", data_label)


@dataclass(frozen=True)
class LiveBar:
    """One normalized, validated real-time OHLCV bar.

    ``ts_event`` carries Databento's documented OHLCV semantics: the
    INCLUSIVE START of the bar's aggregation interval (never the
    close/end) -- see docs/realtime_data.md "Live OHLCV timestamp
    semantics" for the full citation. ``interval`` reuses
    ``app.data.models.HistoricalTimeframe`` rather than declaring a
    second timeframe vocabulary; Phase 4's own adapter only ever
    constructs bars with ``HistoricalTimeframe.ONE_SECOND`` (Databento's
    ``ohlcv-1s`` live schema), but this model itself stays as generic
    as the historical one.
    """

    contract: FuturesContract
    provider: str
    dataset: str
    provider_raw_symbol: str
    interval: HistoricalTimeframe
    ts_event: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    ts_recv: Optional[datetime] = None
    olive_received_at: Optional[datetime] = None
    provider_instrument_id: Optional[str] = None
    data_label: DataLabel = DataLabel.LIVE

    def __post_init__(self) -> None:
        try:
            contract = _require_contract(self.contract, context="LiveBar")
        except FuturesDomainError as exc:
            raise InvalidLiveEventError(f"LiveBar.contract is invalid: {exc}") from exc

        if not isinstance(self.interval, HistoricalTimeframe):
            raise InvalidLiveEventError(
                f"LiveBar.interval must be a HistoricalTimeframe, got "
                f"{self.interval!r} ({type(self.interval).__name__})"
            )

        provider = _require_nonempty_str(self.provider, field_name="LiveBar.provider")
        dataset = _require_nonempty_str(self.dataset, field_name="LiveBar.dataset")
        provider_raw_symbol = _require_nonempty_str(
            self.provider_raw_symbol, field_name="LiveBar.provider_raw_symbol"
        )
        ts_event = _require_utc_datetime(self.ts_event, field_name="LiveBar.ts_event")
        ts_recv = _require_optional_utc_datetime(self.ts_recv, field_name="LiveBar.ts_recv")
        olive_received_at = _require_optional_utc_datetime(
            self.olive_received_at, field_name="LiveBar.olive_received_at"
        )

        open_price = _require_finite_decimal(self.open, field_name="LiveBar.open")
        high_price = _require_finite_decimal(self.high, field_name="LiveBar.high")
        low_price = _require_finite_decimal(self.low, field_name="LiveBar.low")
        close_price = _require_finite_decimal(self.close, field_name="LiveBar.close")
        for name, price in (("open", open_price), ("high", high_price), ("low", low_price), ("close", close_price)):
            if price <= 0:
                raise InvalidLiveEventError(f"LiveBar.{name} must be strictly positive; got {price}")

        if high_price != max(open_price, high_price, low_price, close_price):
            raise InvalidLiveEventError(
                f"LiveBar.high ({high_price}) must be >= open ({open_price}), close "
                f"({close_price}), and low ({low_price})"
            )
        if low_price != min(open_price, high_price, low_price, close_price):
            raise InvalidLiveEventError(
                f"LiveBar.low ({low_price}) must be <= open ({open_price}), close "
                f"({close_price}), and high ({high_price})"
            )

        volume = _require_true_int(self.volume, field_name="LiveBar.volume")
        if volume < 0:
            raise InvalidLiveEventError(f"LiveBar.volume must not be negative; got {volume}")

        provider_instrument_id = _require_optional_nonempty_str(
            self.provider_instrument_id, field_name="LiveBar.provider_instrument_id"
        )
        data_label = _require_data_label_live(self.data_label)

        object.__setattr__(self, "contract", contract)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "dataset", dataset)
        object.__setattr__(self, "provider_raw_symbol", provider_raw_symbol)
        object.__setattr__(self, "ts_event", ts_event)
        object.__setattr__(self, "ts_recv", ts_recv)
        object.__setattr__(self, "olive_received_at", olive_received_at)
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high_price)
        object.__setattr__(self, "low", low_price)
        object.__setattr__(self, "close", close_price)
        object.__setattr__(self, "volume", volume)
        object.__setattr__(self, "provider_instrument_id", provider_instrument_id)
        object.__setattr__(self, "data_label", data_label)


LiveEvent = (LiveTrade, LiveQuote, LiveBar)
"""Convenience tuple for ``isinstance(x, LiveEvent)`` checks -- the three
concrete normalized live event types this phase produces."""


# -- Stream status / telemetry --------------------------------------------


@dataclass(frozen=True)
class LiveStreamStatus:
    """A snapshot of a real-time stream's connection state and
    telemetry, sufficient for later monitoring (Phase 13) without this
    phase building any monitoring system itself.

    - ``events_received``: every record the provider delivered,
      including control/system messages and rejected records.
    - ``events_accepted``: records successfully normalized into a
      :class:`LiveTrade`/:class:`LiveQuote`/:class:`LiveBar` and handed
      to the caller.
    - ``events_rejected``: records that failed identity/validation and
      were NOT handed to the caller (see docs/realtime_data.md "Event
      integrity guarantees").
    - ``last_event_at``: the ``ts_event`` of the most recently ACCEPTED
      event, or ``None`` if none has been accepted yet.
    - ``last_receive_at``: Olive's own wall-clock receive time of the
      most recently received record (accepted or not), or ``None``.
    - ``reconnect_count``: how many times this stream has successfully
      RE-established its connection after a PRIOR, already-successful
      connection was subsequently lost. This is a truthful "did Olive
      actually recover from a drop" count, not a retry-attempt tally
      -- it is NEVER incremented for a retry during the very first
      connection attempt (there is nothing to "re"-establish yet), and
      it is incremented by exactly 1 per successful recovery
      regardless of how many individual attempts that recovery took
      (Phase 4 correction §6: a prior build incremented this for every
      failed attempt during the *initial* connection too, which was not
      a reconnection at all and made this field lie about what it
      measured).
    - ``reconnect_attempts_total``: the total number of individual
      connection ATTEMPTS that failed and were retried, across both the
      initial connection and every later reconnection combined -- for
      observability of retry volume, kept separate from
      ``reconnect_count`` precisely so the latter can mean only
      "successful re-establishments" without losing this information.
    - ``data_gap_count``: how many times the provider has reported a
      documented, non-session-fatal data-loss condition (Databento's
      ``SKIPPED_RECORDS_AFTER_SLOW_READING`` ``ErrorMsg`` code) --
      Olive never treats this as though the stream remained fully
      healthy; see ``app.data.providers.databento_live`` "Data-loss
      handling." Cumulative for the life of the connection; never
      decremented.
    - ``is_stale``: whether the stream is currently considered stale --
      computed by the caller against a configured threshold (see
      ``app.data.live_service.RealTimeDataService``), never a bare
      string comparison.
    """

    state: ConnectionState
    events_received: int = 0
    events_accepted: int = 0
    events_rejected: int = 0
    reconnect_count: int = 0
    reconnect_attempts_total: int = 0
    data_gap_count: int = 0
    last_event_at: Optional[datetime] = None
    last_receive_at: Optional[datetime] = None
    is_stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.state, ConnectionState):
            raise InvalidLiveEventError(
                f"LiveStreamStatus.state must be a ConnectionState, got "
                f"{self.state!r} ({type(self.state).__name__})"
            )
        for field_name in (
            "events_received",
            "events_accepted",
            "events_rejected",
            "reconnect_count",
            "reconnect_attempts_total",
            "data_gap_count",
        ):
            value = _require_true_int(getattr(self, field_name), field_name=f"LiveStreamStatus.{field_name}")
            if value < 0:
                raise InvalidLiveEventError(f"LiveStreamStatus.{field_name} must not be negative; got {value}")
            object.__setattr__(self, field_name, value)
        # reconnect_count and reconnect_attempts_total are deliberately
        # NOT cross-validated against each other here: a reconnection
        # can succeed on its very first attempt (contributing 1 to
        # reconnect_count and 0 to reconnect_attempts_total), so neither
        # bounds the other.
        if self.events_accepted + self.events_rejected > self.events_received:
            raise InvalidLiveEventError(
                "LiveStreamStatus.events_accepted + events_rejected must not exceed "
                f"events_received; got accepted={self.events_accepted}, "
                f"rejected={self.events_rejected}, received={self.events_received}."
            )
        object.__setattr__(
            self,
            "last_event_at",
            _require_optional_utc_datetime(self.last_event_at, field_name="LiveStreamStatus.last_event_at"),
        )
        object.__setattr__(
            self,
            "last_receive_at",
            _require_optional_utc_datetime(self.last_receive_at, field_name="LiveStreamStatus.last_receive_at"),
        )
        if not isinstance(self.is_stale, bool):
            raise InvalidLiveEventError(
                f"LiveStreamStatus.is_stale must be an actual bool, got "
                f"{self.is_stale!r} ({type(self.is_stale).__name__})"
            )
