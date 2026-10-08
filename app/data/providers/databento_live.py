"""Olive's Databento real-time-data provider adapter (Phase 4, corrected;
Phase 4.2 real-package hardening pass applied).

Mirrors ``app.data.providers.databento`` (the Phase 3 historical
adapter)'s secret-safety and lazy-import discipline, but differs from
it in two deliberate structural ways: a Databento ``Live`` client
cannot generally be reused after a connection failure, so this adapter
is built around an injectable ``client_factory: Callable[[], Any]``
(called once per connection attempt -- the initial attempt AND every
reconnect) rather than a single long-lived injected ``client`` object;
and confirming a subscribed contract's full-year identity requires a
SEPARATE metadata client (``metadata_client_factory``), because that
is a Databento *historical/metadata* capability (``symbology.resolve``),
not something the streaming ``Live`` client itself exposes. Neither
factory is ever called during ``__init__`` -- constructing this
provider never opens a network connection; only :meth:`connect` (and
an internal reconnect loop within :meth:`events`) ever does.

Third-party import safety: the ``databento`` package is imported
lazily, only when actually building a default factory (never at
module import time, never merely by selecting
``OLIVE_LIVE_PROVIDER=databento`` in configuration) -- see
``app.data.providers.live_factory``. Both factories may also be
injected directly (what every test in
``tests/test_live_data_databento.py`` does), so this module and its
tests never require the real ``databento`` package to be installed.

**Secret-safety policy**: identical to the historical adapter -- a
vendor exception's own message text never reaches anything this
module raises, logs, or returns. The API key is kept only as a private
constructor-captured value used to build the default factories; it is
never stored as a readable attribute, never included in any
``repr()``, and never interpolated into any error message.

**Record classification**: Databento's live wire protocol distinguishes
record types (``TradeMsg``, ``Mbp1Msg``, ``OhlcvMsg``,
``SymbolMappingMsg``, ``SystemMsg``, ``ErrorMsg``, and others this
phase does not consume) by their binary record-type byte, which the
``databento`` package decodes into distinct Python classes before
Olive ever sees a record. This adapter classifies an incoming record
by DUCK-TYPING its attribute shape rather than ``isinstance`` against
the real ``databento`` module's classes. This is a deliberate choice,
not an oversight: it lets every test in
``tests/test_live_data_databento.py`` inject plain fake record objects
without requiring the real ``databento`` package to be installed,
while the exact same classification code also handles real
``databento`` record objects identically when the real package IS
installed (verification of that is deferred to the user's local Mac
environment -- see docs/realtime_data.md "Known limitations"). A
record this function cannot classify is ``"unknown"`` and is always
explicitly rejected (counted, never yielded, and never allowed to fall
through into another schema's parsing code -- a Phase 4 correction
fix; see this module's ``_handle_record``).

**Corrected Databento 0.87 Live API usage** (Phase 4 correction §2-3,
following an independent audit of the originally delivered adapter,
which was written against an incorrect, undocumented API shape):

- ``databento.Live(key=...)`` takes NO ``dataset`` parameter -- dataset
  belongs only in ``client.subscribe(dataset=..., ...)``. The default
  client factory below constructs ``Live`` with ``key`` only.
- For Olive's synchronous, iterator-based consumption model,
  ``Live.__iter__()`` is documented to auto-start the session itself;
  calling ``.start()`` first and THEN synchronously iterating is
  documented to raise ``ValueError``. This adapter therefore never
  calls ``.start()`` at all -- it subscribes, then consumes directly
  via ``for record in client:``, and calls ``.stop()`` for graceful
  shutdown. ``_require_databento_live_client_interface`` reflects this:
  it requires ``subscribe``/``stop``/``__iter__`` only, never
  ``start``.

None of this could be exercised against the real installed package
during this build (see docs/realtime_data.md "Known limitations") --
the corrected shape above is taken from the user-supplied, documented
Databento 0.87 API description this correction was issued against,
and the compatibility test doubles in
``tests/test_live_data_databento.py`` are deliberately written to
ENFORCE this exact signature/lifecycle (reject an unexpected
``dataset`` kwarg to ``Live()``; raise ``ValueError`` if ``start()`` is
called before iteration) so a future regression back to the old,
incorrect shape fails loudly rather than being silently accepted by an
overly permissive fake.

**Phase 4.2 correction -- real-package hardening pass**: a second,
narrower correction pass issued after independent audit of the
corrected Phase 4 artifact, addressing four remaining real-Databento
compatibility/lifecycle gaps that Phase 4's own test doubles had been
too permissive to expose:

- **MBP-1 top-of-book extraction** (§1): the originally-corrected quote
  branch still gated ``levels[0]`` on ``isinstance(levels, (list,
  tuple))`` and silently fell back to reading ``bid_px``/``ask_px``/
  ``bid_sz``/``ask_sz`` off the whole ``Mbp1Msg`` record (fields that
  do not exist there) when that check failed -- and every test double
  used a plain Python ``list``, so this never failed in this sandbox.
  Databento's real record interface exposes top-of-book via EITHER
  flat ``bid_px_00``/``ask_px_00``/``bid_sz_00``/``ask_sz_00``
  properties, or a ``levels`` array whose element type (``BidAskPair``)
  is indexable but not necessarily a built-in ``list``/``tuple``. See
  :func:`_mbp1_top_of_book_fields`, which this adapter's quote
  normalization now calls instead of the old inline logic: it prefers
  the flat properties when present, otherwise treats ``levels`` as
  genuinely indexable (a narrow ``try``/``except`` around ``levels[0]``,
  never an ``isinstance`` gate), and raises
  :class:`~app.data.live_models.LiveProviderDataError` -- never a
  silent empty quote -- if neither representation is usable.
- **Shutdown exception narrowing** (§3): :meth:`DatabentoLiveProvider.close`
  previously suppressed ANY exception merely because its class module
  started with ``databento`` -- too broad, since a vendor-origin
  exception is not automatically a harmless shutdown condition. It now
  splits into three cases: a recognized-benign shutdown message (see
  :func:`_is_benign_shutdown_exception`) is suppressed; an unexpected
  ``databento``-origin exception is translated into a sanitized
  :class:`~app.data.live_models.LiveProviderShutdownError` and raised
  (never silently discarded, never leaking vendor internals); a
  non-``databento``-origin exception always propagates unchanged. A
  coherent ``STOPPED`` state is reached via ``finally`` on every path.
- **Old-client cleanup during mid-stream reconnect** (§4): the
  mid-stream recovery branch of :meth:`events` previously replaced
  ``self._client`` with a freshly constructed client immediately on an
  iterator failure, without first attempting to stop the OLD, now-
  broken client -- an iterator exception does not guarantee every
  underlying resource on that old client was already released. See
  :meth:`DatabentoLiveProvider._best_effort_stop_old_client`, called
  with the old client BEFORE ``_connect_with_reconnect`` is attempted;
  deliberately more permissive than :meth:`close`'s policy (ANY
  ``databento``-origin exception from the old client's own shutdown is
  absorbed, since this is best-effort cleanup of a connection already
  known to be broken), while a non-``databento``-origin exception still
  always propagates. ``self._client`` is reassigned to the new client,
  and ``reconnect_count`` incremented, only once a genuinely new client
  has been successfully established -- never before, and never leaving
  the old client referenced as active afterward.
- **Real-package offline record-class introspection** (§2): beyond the
  existing ``inspect.signature``-based ``Live``/``Historical``
  constructor check, ``tests/test_live_data_databento.py`` now also
  introspects (via ``dir()``, no network, no API key) the real
  ``TradeMsg``/``Mbp1Msg``/``OhlcvMsg``/``SymbolMappingMsg``/
  ``ErrorMsg``/``SystemMsg``/``BidAskPair`` classes whenever the real
  ``databento``/``databento_dbn`` packages are installed, confirming
  this adapter's assumed attribute names are still valid on the
  installed version.

**Full-year contract identity proof** (Phase 4 correction §4): a
Databento raw futures symbol (e.g. ``"NQZ6"``) encodes only the last
digit of the contract year, so it is genuinely ambiguous across
decades (``NQ-2026-12`` and ``NQ-2036-12`` both produce ``"NQZ6"``).
Before this adapter ever subscribes to the live stream, it
authoritatively resolves EVERY subscribed contract's raw symbol via
Databento's point-in-time ``symbology.resolve`` metadata endpoint,
scoped to that EXACT contract's own calendar month -- a contract that
is not actually listed for that specific month (including a contract
so far in the future CME has not listed it yet) fails this resolution
and the whole ``connect()`` call is rejected before any live
connection is opened. Two distinct Olive contracts that resolve to the
same instrument ID are also rejected as a genuine identity collision.
The resulting per-contract authoritative instrument-ID map is then
used to validate every live ``SymbolMappingMsg`` as data starts
flowing: a mapping that contradicts a contract's own pre-verified
identity raises :class:`~app.data.live_models.LiveIdentityUnresolvedError`
rather than being silently trusted. See docs/realtime_data.md
"Full-year contract identity proof" for the full write-up.

**ErrorMsg fatality policy** (Phase 4 correction §5): see
``_FATAL_ERROR_CODE_NAMES``/``_DATA_GAP_ERROR_CODE_NAMES`` below and
docs/realtime_data.md "ErrorMsg handling and data-loss policy".
"""

from __future__ import annotations

import operator
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from typing import Any, Optional

from app.futures.models import FuturesContract, FuturesInstrument
from app.data.live_models import (
    ConnectionState,
    LiveBar,
    LiveContractIdentityError,
    LiveDataError,
    LiveEvent,
    LiveEventType,
    LiveIdentityUnresolvedError,
    LiveProviderAuthenticationError,
    LiveProviderDataError,
    LiveProviderError,
    LiveProviderNotConfiguredError,
    LiveProviderPermissionError,
    LiveProviderRateLimitError,
    LiveProviderShutdownError,
    LiveProviderUnavailableError,
    LiveQuote,
    LiveReconnectExhaustedError,
    LiveStreamClosedError,
    LiveStreamStatus,
    LiveSubscriptionRequest,
    LiveTickMisalignedError,
    LiveTrade,
    _require_live_subscription_request,
)
from app.data.live_provider_base import RealTimeMarketDataProvider
from app.data.models import HistoricalTimeframe
from app.data.providers.databento import ProviderSymbologyError, _distinct_resolved_instrument_id

DEFAULT_LIVE_DATASET = "GLBX.MDP3"

# Databento's documented fixed-point price scale: every integer price
# field in a live record is the true price multiplied by 1e9. Olive
# always converts via Decimal division -- never a float division,
# which could lose precision for a futures-market price.
_PRICE_FIXED_POINT_SCALE = Decimal("1000000000")

# Databento ErrorMsg codes documented as FATAL (the connection closes
# and must not be retried). Phase 4 correction §5: the previous list
# omitted INTERNAL_ERROR and REPLAY_DATA_AGED_OUT. SYMBOL_RESOLUTION_FAILED
# is deliberately NOT listed here -- it is documented as non-fatal
# (streaming continues) and is handled as an ordinary rejected record
# below. A code this adapter does not specifically recognize is
# treated as transient/recoverable by default, the safer
# fail-open-to-retry choice for an unrecognized code.
_FATAL_ERROR_CODE_NAMES = frozenset(
    {
        "AUTH_FAILED",
        "API_KEY_DEACTIVATED",
        "CONNECTION_LIMIT_EXCEEDED",
        "INVALID_SUBSCRIPTION",
        "INTERNAL_ERROR",
        "REPLAY_DATA_AGED_OUT",
    }
)

# Phase 4 correction §5: codes that are NOT session-fatal but represent
# genuine, documented market-data loss -- Olive must never continue as
# though the stream remained fully healthy. Handled by transitioning
# to ConnectionState.DEGRADED and incrementing
# LiveStreamStatus.data_gap_count (see _handle_record), rather than by
# raising (the connection itself is still good) or by silent
# acceptance (data loss did happen).
_DATA_GAP_ERROR_CODE_NAMES = frozenset({"SKIPPED_RECORDS_AFTER_SLOW_READING"})


def _is_databento_exception(exc: BaseException) -> bool:
    """Whether ``exc`` originates from the ``databento`` package (its
    real exception hierarchy, or a test double deliberately declaring
    ``__module__`` to match it) -- mirrors
    ``app.data.providers.databento._is_databento_exception`` exactly."""
    module = type(exc).__module__
    return isinstance(module, str) and module.startswith("databento")


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "http_status", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


# Phase 4.2 correction §3: a vendor-origin exception raised while
# stopping a client is NOT automatically a harmless shutdown
# condition -- only a message matching one of these specifically
# recognized, already-expected benign patterns is treated that way.
# Classified by message substring ONLY (the same secret-safety
# discipline as _translate_databento_exception below: the message is
# inspected here to pick a category, never echoed into anything this
# adapter raises).
_BENIGN_SHUTDOWN_MESSAGE_SUBSTRINGS = (
    "already stopped",
    "already closed",
    "already disconnected",
    "already shut down",
    "not connected",
    "not started",
    "no active session",
    "no active connection",
)


def _is_benign_shutdown_exception(exc: BaseException) -> bool:
    """Whether a ``databento``-origin exception (the caller has
    already confirmed this via :func:`_is_databento_exception`)
    represents a specifically recognized, already-expected benign
    shutdown condition -- e.g. stopping a connection that is already
    stopped, or was never fully established -- rather than an
    unexpected vendor-side failure. See :meth:`DatabentoLiveProvider.close`."""
    message = str(exc).lower()
    return any(substring in message for substring in _BENIGN_SHUTDOWN_MESSAGE_SUBSTRINGS)


def _translate_databento_exception(exc: BaseException) -> LiveProviderError:
    """Translate a Databento vendor exception raised during connect/
    subscribe into one of this package's own :class:`LiveProviderError`
    subclasses, carrying only a fixed, generic, pre-written message for
    the matched category. Mirrors
    ``app.data.providers.databento._translate_databento_exception``'s
    secret-safety discipline exactly: the vendor exception's text is
    inspected HERE ONLY, to classify it, and is never echoed into the
    returned error."""
    message = str(exc)
    lowered = message.lower()
    status_code = _status_code(exc)

    is_auth_failure = (
        status_code == 401
        or "unauthoriz" in lowered
        or "authentic" in lowered
        or "invalid api key" in lowered
        or "invalid username" in lowered
        or "basic auth" in lowered
        or "bad api key" in lowered
        or "api_key_deactivated" in lowered
        or "deactivated" in lowered
    )
    if is_auth_failure:
        return LiveProviderAuthenticationError(
            "Databento live authentication failed (invalid, rejected, or deactivated API key)."
        )

    is_permission_failure = (
        status_code == 403
        or "forbidden" in lowered
        or "permission" in lowered
        or "subscription" in lowered
        or "entitle" in lowered
        or "invalid_subscription" in lowered
    )
    if is_permission_failure:
        return LiveProviderPermissionError(
            "Databento live permission/subscription error (the API key is valid but not "
            "entitled to this dataset/schema/symbol, or the subscription request itself was "
            "rejected as invalid)."
        )

    is_rate_limit = (
        status_code == 429
        or "rate limit" in lowered
        or "too many requests" in lowered
        or "connection_limit_exceeded" in lowered
        or "connection limit" in lowered
    )
    if is_rate_limit:
        return LiveProviderRateLimitError("Databento live connection/rate limit exceeded.")

    if status_code is not None and 500 <= status_code < 600:
        return LiveProviderUnavailableError(f"Databento live server error (status {status_code}).")

    return LiveProviderUnavailableError("Databento live provider error.")


def _translate_error_record(code_name: Optional[str]) -> LiveProviderError:
    """Translate a Databento ``ErrorMsg`` record's own documented
    ``code`` (already resolved to its symbolic name by the caller --
    see :func:`_error_code_name`) into one of this package's
    :class:`LiveProviderError` subclasses, for a FATAL code only (the
    caller is responsible for routing a non-fatal/data-gap code
    elsewhere -- see ``_FATAL_ERROR_CODE_NAMES``/
    ``_DATA_GAP_ERROR_CODE_NAMES``). A fatal code this adapter does not
    specifically recognize by name is translated as
    :class:`LiveProviderUnavailableError` defensively, though
    ``_handle_record`` only calls this for codes already confirmed to
    be in ``_FATAL_ERROR_CODE_NAMES``."""
    if code_name in ("AUTH_FAILED", "API_KEY_DEACTIVATED"):
        return LiveProviderAuthenticationError(
            "Databento live stream reported an authentication failure; the connection has "
            "closed and will not be retried."
        )
    if code_name == "CONNECTION_LIMIT_EXCEEDED":
        return LiveProviderRateLimitError(
            "Databento live stream reported the account's connection limit was exceeded."
        )
    if code_name == "INVALID_SUBSCRIPTION":
        return LiveProviderPermissionError(
            "Databento live stream rejected the subscription as invalid; the connection has "
            "closed and will not be retried."
        )
    if code_name == "INTERNAL_ERROR":
        return LiveProviderUnavailableError(
            "Databento live stream reported an internal provider error; the connection has "
            "closed and will not be retried."
        )
    if code_name == "REPLAY_DATA_AGED_OUT":
        return LiveProviderUnavailableError(
            "Databento live stream reported that requested replay data has aged out; the "
            "connection has closed and will not be retried."
        )
    return LiveProviderUnavailableError("Databento live stream reported a fatal provider-side error.")


@dataclass(frozen=True)
class ReconnectPolicy:
    """A pure, testable bounded-exponential-backoff policy for
    reconnecting a live stream after a TRANSIENT failure.

    Deliberately holds no mutable state and performs no actual
    sleeping itself -- :meth:`delay_for_attempt` is a pure function of
    ``attempt``, and the caller (:class:`DatabentoLiveProvider`)
    supplies its own injectable ``sleep_fn`` so unit tests never
    actually sleep (see the Phase 4 prompt's explicit "deterministic
    backoff testing" requirement).
    """

    max_attempts: int
    base_delay_seconds: Decimal
    max_delay_seconds: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise LiveDataError(
                f"ReconnectPolicy.max_attempts must be a true int, got {self.max_attempts!r} "
                f"({type(self.max_attempts).__name__})"
            )
        if self.max_attempts < 0:
            raise LiveDataError(f"ReconnectPolicy.max_attempts must not be negative; got {self.max_attempts}")
        for field_name in ("base_delay_seconds", "max_delay_seconds"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, Decimal):
                raise LiveDataError(f"ReconnectPolicy.{field_name} must be a Decimal, got {value!r}")
            if not value.is_finite() or value <= 0:
                raise LiveDataError(f"ReconnectPolicy.{field_name} must be finite and strictly positive; got {value}")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise LiveDataError(
                "ReconnectPolicy.max_delay_seconds must be >= base_delay_seconds; got "
                f"max_delay_seconds={self.max_delay_seconds}, base_delay_seconds={self.base_delay_seconds}"
            )

    def delay_for_attempt(self, attempt: int) -> Decimal:
        """The backoff delay before reconnect attempt number ``attempt``
        (1-indexed: the first retry is ``attempt=1``), bounded above by
        ``max_delay_seconds``."""
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise LiveDataError(f"delay_for_attempt(attempt) must be a true int, got {attempt!r}")
        if attempt < 1:
            raise LiveDataError(f"delay_for_attempt(attempt) must be >= 1; got {attempt}")
        # 2 ** (attempt - 1) keeps the first retry at exactly
        # base_delay_seconds rather than doubling it immediately.
        candidate = self.base_delay_seconds * Decimal(2 ** (attempt - 1))
        return min(candidate, self.max_delay_seconds)


def _require_databento_live_client_interface(client: Any) -> None:
    """Validate the injected/constructed live client's CALLABLE
    INTERFACE before this adapter relies on any of it -- mirrors
    ``app.data.providers.databento._require_databento_client_interface``'s
    "the DI seam is a public boundary too" discipline. Never a strict
    ``isinstance`` against Databento's concrete client class, so an
    injected test double implementing only this interface remains a
    valid, intentional way to exercise this adapter without the real
    package installed.

    Phase 4 correction §3: requires only ``subscribe``, ``stop``, and
    iteration support -- NOT ``start``. This adapter's corrected
    consumption model never calls ``.start()`` (synchronous iteration
    auto-starts the session per Databento's documented API; calling
    ``.start()`` first and then iterating is documented to raise
    ``ValueError``), so requiring ``start`` here would accept a client
    this adapter does not actually need and could mask the opposite
    mistake (a client that only supports the callback/``start()``
    model, which this adapter's architecture does not use)."""
    missing: list[str] = []
    if not callable(getattr(client, "subscribe", None)):
        missing.append("subscribe")
    if not callable(getattr(client, "stop", None)):
        missing.append("stop")
    if not callable(getattr(client, "__iter__", None)):
        missing.append("__iter__")
    if missing:
        raise LiveProviderNotConfiguredError(
            "The constructed/injected Databento live client is missing the required interface "
            f"this adapter needs ({', '.join(missing)}); refusing to use a client that cannot "
            "service a live subscription."
        )


def _require_databento_metadata_client_interface(client: Any) -> None:
    """Validate the injected/constructed metadata client exposes
    ``symbology.resolve`` -- the one capability this adapter needs it
    for (full-year contract identity proof; see this module's
    docstring). Never a strict ``isinstance`` against Databento's
    concrete ``Historical`` client class."""
    symbology = getattr(client, "symbology", None)
    if symbology is None or not callable(getattr(symbology, "resolve", None)):
        raise LiveIdentityUnresolvedError(
            "The configured Databento metadata client has no symbology-resolution capability; "
            "refusing to open a live stream without confirmed full-year contract identity for "
            "every subscribed contract."
        )


def _contract_month_window(contract: FuturesContract) -> tuple[date, date]:
    """The half-open ``[start, end)`` calendar-month window for
    ``contract``'s OWN contract month -- used to scope the point-in-
    time symbology resolution that proves its full-year identity (see
    this module's docstring). Deliberately the contract's own named
    month, not an arbitrary window anchored on "today": a contract
    that genuinely exists has an established raw-symbol mapping
    covering its own month (CME lists quarterly futures well ahead of
    their trading month); a contract that does not exist yet (e.g. a
    decade-away request) has no such mapping and fails resolution,
    which is exactly the fail-closed behavior this exists to provide."""
    year = contract.year
    month = int(contract.month)
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _require_instrument_id(raw_instrument_id: object) -> Optional[str]:
    """Validate a Databento record's ``instrument_id`` field as the
    actual integer-like provider identifier Databento documents it as
    -- never a float, bool, or arbitrary object silently stringified
    into a trusted identity key (Phase 4 correction §10). Returns the
    canonical, strictly-positive-integer string form, or ``None`` if
    the value is missing/malformed -- the caller treats ``None`` as a
    rejected record, never a crash."""
    if raw_instrument_id is None or isinstance(raw_instrument_id, bool):
        return None
    try:
        value = operator.index(raw_instrument_id)
    except TypeError:
        return None
    if value <= 0:
        return None
    return str(value)


def _price_from_fixed_point(raw_price: object, *, field_name: str) -> Decimal:
    """Convert a Databento fixed-point integer price (true price *
    1e9) to an exact ``Decimal`` -- never a float division. Rejects
    ``bool`` (``operator.index`` would otherwise silently accept it)
    and any non-integer-like value."""
    if isinstance(raw_price, bool):
        raise LiveProviderDataError(f"{field_name} must not be a bool; got {raw_price!r}")
    try:
        value = operator.index(raw_price)
    except TypeError:
        raise LiveProviderDataError(
            f"{field_name} must be a true integer-like fixed-point value; got {raw_price!r} "
            f"({type(raw_price).__name__})"
        ) from None
    return Decimal(value) / _PRICE_FIXED_POINT_SCALE


def _require_tick_aligned(price: Decimal, tick_size: Decimal, *, field_name: str) -> Decimal:
    """Require ``price`` to be an exact integer multiple of
    ``tick_size`` -- Olive never silently rounds a misaligned live
    price onto the nearest tick (mirrors
    ``app.data.models.HistoricalPriceTickMisalignedError``'s policy).
    ``tick_size`` is always the already-production-validated
    :attr:`~app.futures.models.FuturesInstrument.tick_size` supplied by
    the service layer -- this adapter never derives or guesses it."""
    if tick_size <= 0:
        raise LiveProviderDataError(f"{field_name}: instrument tick_size must be positive; got {tick_size}")
    if price % tick_size != 0:
        raise LiveTickMisalignedError(
            f"{field_name} {price} is not an exact multiple of the instrument's tick size {tick_size}."
        )
    return price


def _safe_size(raw_size: object, *, field_name: str) -> int:
    if isinstance(raw_size, bool):
        raise LiveProviderDataError(f"{field_name} must not be a bool; got {raw_size!r}")
    try:
        value = operator.index(raw_size)
    except TypeError:
        raise LiveProviderDataError(
            f"{field_name} must be a true integer-like value; got {raw_size!r} ({type(raw_size).__name__})"
        ) from None
    return value


# Phase 4.2 correction §1: the attribute names Databento's real 0.87
# Mbp1Msg exposes directly for depth level 0 (always populated for a
# trades/mbp-1 subscription) -- preferred over `levels[0]` when
# present at all, since they are documented specifically for this
# "top of book only" use case.
_MBP1_FLAT_TOP_OF_BOOK_ATTRS = ("bid_px_00", "ask_px_00", "bid_sz_00", "ask_sz_00")


def _mbp1_top_of_book_fields(record: Any) -> tuple[object, object, object, object]:
    """Extract an MBP-1 record's top-of-book raw
    ``(bid_px, ask_px, bid_sz, ask_sz)`` values.

    Phase 4.2 correction §1: the originally delivered adapter required
    ``isinstance(levels, (list, tuple))`` before trusting ``levels[0]``
    at all, and silently fell back to reading ``bid_px``/``ask_px``/
    ``bid_sz``/``ask_sz`` directly off the WHOLE ``Mbp1Msg`` record
    otherwise -- fields that do not exist there. Every test double
    used a plain Python ``list`` for ``levels``, so this never failed
    in this sandbox, but Databento's real Python record interface
    exposes MBP-1 top-of-book data as EITHER flat ``*_00`` properties
    directly on the record, or a ``levels`` array whose element type
    (``BidAskPair``) is an indexable provider structure that is not
    necessarily a built-in ``list``/``tuple`` -- so the real record
    would have failed the ``isinstance`` check and silently produced
    an empty (falsely no-bid/no-ask) quote instead of either the real
    data or a clear rejection.

    This function instead: (1) prefers the flat ``*_00`` properties
    when the record exposes ANY of them at all; (2) otherwise treats
    ``levels`` as genuinely indexable -- a bare ``levels[0]`` inside a
    narrow ``try``/``except``, never an ``isinstance`` check against
    built-in container types, so a real ``BidAskPair`` array (or any
    other indexable provider structure, including a deliberately
    non-list/tuple fake in this module's own tests) is accepted; (3)
    raises :class:`LiveProviderDataError` -- never silently returns an
    empty/placeholder result -- if neither representation is usable,
    including when the resolved top-of-book entry exposes none of the
    four expected attributes at all."""
    if any(hasattr(record, attr) for attr in _MBP1_FLAT_TOP_OF_BOOK_ATTRS):
        return (
            getattr(record, "bid_px_00", None),
            getattr(record, "ask_px_00", None),
            getattr(record, "bid_sz_00", None),
            getattr(record, "ask_sz_00", None),
        )

    levels = getattr(record, "levels", None)
    if levels is not None:
        try:
            top = levels[0]
        except (TypeError, IndexError, KeyError):
            top = None
        if top is not None and any(
            hasattr(top, attr) for attr in ("bid_px", "ask_px", "bid_sz", "ask_sz")
        ):
            return (
                getattr(top, "bid_px", None),
                getattr(top, "ask_px", None),
                getattr(top, "bid_sz", None),
                getattr(top, "ask_sz", None),
            )

    raise LiveProviderDataError(
        "Databento MBP-1 quote record exposes neither the documented flat "
        "bid_px_00/ask_px_00/bid_sz_00/ask_sz_00 top-of-book properties nor a usable, "
        "indexable 'levels' top-of-book entry; refusing to silently treat it as an "
        "empty quote."
    )


def _ts_event_from_nanos(raw_ts: object, *, field_name: str) -> datetime:
    """Convert a Databento ``ts_event``/``ts_recv`` field (documented
    as a UNIX nanosecond timestamp, delivered to Python as a true int)
    to a timezone-aware UTC ``datetime``. A ``databento`` DataFrame/
    record wrapper may also hand back an object already exposing
    ``to_pydatetime()`` (e.g. a ``pandas.Timestamp``) -- that path is
    used directly when present, since re-deriving nanoseconds from an
    already-converted value would risk a rounding mismatch."""
    if hasattr(raw_ts, "to_pydatetime"):
        converted = raw_ts.to_pydatetime()
        if converted.tzinfo is None:
            converted = converted.replace(tzinfo=timezone.utc)
        return converted.astimezone(timezone.utc)
    if isinstance(raw_ts, datetime):
        if raw_ts.tzinfo is None:
            return raw_ts.replace(tzinfo=timezone.utc)
        return raw_ts.astimezone(timezone.utc)
    if isinstance(raw_ts, bool) or not isinstance(raw_ts, int):
        raise LiveProviderDataError(
            f"{field_name} must be a UNIX nanosecond int timestamp; got {raw_ts!r} ({type(raw_ts).__name__})"
        )
    try:
        seconds, nanos_remainder = divmod(raw_ts, 1_000_000_000)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=nanos_remainder // 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise LiveProviderDataError(f"{field_name} is not a representable UNIX nanosecond timestamp: {raw_ts!r}") from exc


def _error_code_name(record: Any) -> Optional[str]:
    """Resolve a Databento ``ErrorMsg``/``SystemMsg`` record's ``code``
    to its symbolic name, whether the real package exposes it as an
    ``IntEnum`` member (whose own ``.name`` is already the symbolic
    string) or as a bare int (unrecognized -- returns ``None``)."""
    code = getattr(record, "code", None)
    name = getattr(code, "name", None)
    if isinstance(name, str) and name:
        return name
    return None


def _classify_record(record: Any) -> str:
    """Classify an incoming Databento live record by duck-typing its
    attribute shape -- see this module's docstring for why this is a
    deliberate choice over ``isinstance`` against real ``databento``
    classes. Returns one of ``"error"``, ``"system"``,
    ``"symbol_mapping"``, ``"quote"``, ``"bar"``, ``"trade"``, or
    ``"unknown"`` (an unrecognized record is never a crash, and is
    NEVER passed to trade/quote/bar normalization -- see
    ``_handle_record``'s explicit ``"unknown"`` rejection, a Phase 4
    correction fix for a record that previously fell through into bar
    normalization by default)."""
    if hasattr(record, "err") and hasattr(record, "is_last"):
        return "error"
    if hasattr(record, "stype_in_symbol") and hasattr(record, "stype_out_symbol"):
        return "symbol_mapping"
    if hasattr(record, "msg") and hasattr(record, "code") and not hasattr(record, "price"):
        return "system"
    if hasattr(record, "levels") or any(hasattr(record, attr) for attr in _MBP1_FLAT_TOP_OF_BOOK_ATTRS):
        return "quote"
    if (
        hasattr(record, "open")
        and hasattr(record, "high")
        and hasattr(record, "low")
        and hasattr(record, "close")
        and hasattr(record, "volume")
    ):
        return "bar"
    if hasattr(record, "price") and hasattr(record, "size"):
        return "trade"
    return "unknown"


class DatabentoLiveProvider(RealTimeMarketDataProvider):
    """Olive's real-time provider adapter for Databento (``GLBX.MDP3``).

    Implements :class:`RealTimeMarketDataProvider` using the documented
    ``databento.Live`` client surface. Every subscribed event type maps
    to its own Databento live schema (see
    ``app.data.live_models.LiveEventType.databento_schema``); this
    adapter issues one ``client.subscribe(...)`` call per requested
    schema, all against the same connected client and dataset.

    See this module's docstring for the corrected Databento 0.87 Live
    API usage (no ``dataset`` on ``Live()``, no ``.start()`` before
    synchronous iteration) and the full-year contract identity proof
    this adapter performs before ``connect()`` ever opens a live
    connection.
    """

    def __init__(
        self,
        api_key: str,
        dataset: str = DEFAULT_LIVE_DATASET,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
        metadata_client_factory: Optional[Callable[[], Any]] = None,
        reconnect_policy: Optional[ReconnectPolicy] = None,
        stale_threshold_seconds: Decimal = Decimal("10"),
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if client_factory is None or metadata_client_factory is None:
            if not isinstance(api_key, str) or not api_key.strip():
                raise LiveProviderNotConfiguredError("Databento live provider requires a non-empty API key")
        if not isinstance(dataset, str) or not dataset.strip():
            raise LiveProviderNotConfiguredError("Databento live provider requires a non-empty dataset code")
        if reconnect_policy is not None and not isinstance(reconnect_policy, ReconnectPolicy):
            raise LiveProviderNotConfiguredError(
                f"reconnect_policy must be a ReconnectPolicy, got {type(reconnect_policy).__name__}"
            )
        if not callable(sleep_fn):
            raise LiveProviderNotConfiguredError("sleep_fn must be callable")

        self._dataset = dataset.strip()
        self._reconnect_policy = reconnect_policy or ReconnectPolicy(
            max_attempts=5, base_delay_seconds=Decimal("1"), max_delay_seconds=Decimal("30")
        )
        self._sleep_fn = sleep_fn
        self._stale_threshold_seconds = stale_threshold_seconds

        stripped_key = api_key.strip() if isinstance(api_key, str) else None

        if client_factory is not None:
            # Dependency injection path: every test in
            # tests/test_live_data_databento.py uses this, so the real
            # `databento` package is never required to be installed.
            self._client_factory = client_factory
        else:
            def _default_client_factory() -> Any:
                try:
                    import databento  # Phase 4 third-party import safety: lazy, only here.
                except ImportError as exc:
                    raise LiveProviderNotConfiguredError(
                        "OLIVE_LIVE_PROVIDER=databento is selected, but the 'databento' package "
                        "is not installed in this environment."
                    ) from exc
                # Phase 4 correction §2: Live() takes NO `dataset` --
                # dataset is supplied only to client.subscribe(...).
                return databento.Live(key=stripped_key)

            self._client_factory = _default_client_factory

        if metadata_client_factory is not None:
            self._metadata_client_factory = metadata_client_factory
        else:
            def _default_metadata_client_factory() -> Any:
                try:
                    import databento
                except ImportError as exc:
                    raise LiveIdentityUnresolvedError(
                        "OLIVE_LIVE_PROVIDER=databento is selected, but the 'databento' package "
                        "is not installed in this environment; cannot confirm contract identity."
                    ) from exc
                return databento.Historical(key=stripped_key)

            self._metadata_client_factory = _default_metadata_client_factory
        # api_key/stripped_key are never stored as a readable instance
        # attribute beyond the closures above -- they cannot leak
        # through repr()/logging/errors raised by this adapter.

        self._client: Optional[Any] = None
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._subscription: Optional[LiveSubscriptionRequest] = None
        self._instrument_map: dict[str, FuturesInstrument] = {}
        self._instrument_id_to_contract: dict[str, FuturesContract] = {}
        self._expected_instrument_ids: dict[str, str] = {}

        self._events_received = 0
        self._events_accepted = 0
        self._events_rejected = 0
        self._reconnect_count = 0
        self._reconnect_attempts_total = 0
        self._data_gap_count = 0
        self._last_event_at: Optional[datetime] = None
        self._last_receive_at: Optional[datetime] = None

    @property
    def name(self) -> str:
        return "databento"

    @property
    def dataset(self) -> str:
        return self._dataset

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def status(self) -> LiveStreamStatus:
        is_stale = False
        if self._last_receive_at is not None and self._state is ConnectionState.CONNECTED:
            elapsed = (datetime.now(timezone.utc) - self._last_receive_at).total_seconds()
            is_stale = Decimal(str(elapsed)) > self._stale_threshold_seconds
        return LiveStreamStatus(
            state=self._state,
            events_received=self._events_received,
            events_accepted=self._events_accepted,
            events_rejected=self._events_rejected,
            reconnect_count=self._reconnect_count,
            reconnect_attempts_total=self._reconnect_attempts_total,
            data_gap_count=self._data_gap_count,
            last_event_at=self._last_event_at,
            last_receive_at=self._last_receive_at,
            is_stale=is_stale,
        )

    # -- Full-year contract identity proof -------------------------------

    def _resolve_authoritative_instrument_ids(self, subscription: LiveSubscriptionRequest) -> dict[str, str]:
        """Authoritatively resolve every subscribed contract's full-
        year/month identity via Databento's point-in-time symbology
        resolution, BEFORE any live connection is opened or any market
        event is ever yielded. See this module's docstring "Full-year
        contract identity proof" for the full rationale. Returns
        ``{contract.identity: instrument_id}``; raises
        :class:`LiveIdentityUnresolvedError` (never anything else) on
        any failure to establish that proof for every contract."""
        metadata_client = self._metadata_client_factory()
        _require_databento_metadata_client_interface(metadata_client)

        resolved: dict[str, str] = {}
        resolved_id_to_contract: dict[str, FuturesContract] = {}
        for contract in subscription.contracts:
            raw_symbol = contract.display_code
            start_date, end_date = _contract_month_window(contract)
            try:
                resolution = metadata_client.symbology.resolve(
                    dataset=self._dataset,
                    symbols=[raw_symbol],
                    stype_in="raw_symbol",
                    stype_out="instrument_id",
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as exc:
                if not _is_databento_exception(exc):
                    raise
                raise LiveIdentityUnresolvedError(
                    f"Databento symbology resolution failed while confirming the full-year "
                    f"identity of {contract.identity} (raw symbol {raw_symbol!r}); refusing to "
                    "open a live stream without confirmed contract identity."
                ) from None

            try:
                instrument_id = _distinct_resolved_instrument_id(resolution, raw_symbol)
            except ProviderSymbologyError as exc:
                raise LiveIdentityUnresolvedError(
                    f"Could not confirm {contract.identity}'s full-year identity for raw symbol "
                    f"{raw_symbol!r} during its own contract month ({start_date} to {end_date}): "
                    f"{exc}"
                ) from None

            existing_contract = resolved_id_to_contract.get(instrument_id)
            if existing_contract is not None and existing_contract.identity != contract.identity:
                raise LiveIdentityUnresolvedError(
                    f"Databento instrument ID {instrument_id!r} was authoritatively resolved for "
                    f"BOTH {existing_contract.identity} and {contract.identity}; refusing to treat "
                    "two distinct Olive contracts as the same live instrument."
                )
            resolved_id_to_contract[instrument_id] = contract
            resolved[contract.identity] = instrument_id

        return resolved

    # -- Connection lifecycle -------------------------------------------

    def connect(
        self,
        subscription: LiveSubscriptionRequest,
        instruments: Mapping[str, FuturesInstrument],
    ) -> None:
        subscription = _require_live_subscription_request(subscription, context="DatabentoLiveProvider.connect")
        if not isinstance(instruments, Mapping):
            raise LiveProviderNotConfiguredError(
                f"DatabentoLiveProvider.connect expects a mapping of instruments, got {type(instruments).__name__}"
            )
        for identity in subscription.contract_identities:
            if identity not in instruments or not isinstance(instruments[identity], FuturesInstrument):
                raise LiveProviderNotConfiguredError(
                    f"DatabentoLiveProvider.connect was not given a validated FuturesInstrument for "
                    f"subscribed contract {identity!r}."
                )
        if self._state not in (ConnectionState.DISCONNECTED, ConnectionState.STOPPED, ConnectionState.FAILED):
            raise LiveStreamClosedError(
                f"DatabentoLiveProvider.connect was called while the stream is already "
                f"{self._state.value}; close() it first."
            )

        self._subscription = subscription
        self._instrument_map = dict(instruments)
        self._instrument_id_to_contract = {}
        self._state = ConnectionState.CONNECTING

        try:
            self._expected_instrument_ids = self._resolve_authoritative_instrument_ids(subscription)
        except LiveDataError:
            self._state = ConnectionState.FAILED
            raise

        self._client = self._connect_with_reconnect(initial=True)
        self._state = ConnectionState.CONNECTED

    def _connect_with_reconnect(self, *, initial: bool) -> Any:
        """Build a fresh client and subscribe it to every event type in
        ``self._subscription`` -- retrying transient failures per
        :attr:`_reconnect_policy` with NO actual sleeping in tests
        (``self._sleep_fn`` is injectable). A permanent failure
        (authentication/permission) is raised immediately, never
        retried.

        Phase 4 correction §2-3: never calls ``.start()`` -- Olive's
        synchronous consumption model relies on ``Live.__iter__()``'s
        documented auto-start behavior (see :meth:`events`).

        Phase 4 correction §6: this method's own retry count is
        tallied into ``self._reconnect_attempts_total`` only -- it
        NEVER touches ``self._reconnect_count`` (which means "a prior
        successful connection was lost and then re-established"; that
        is only ever true for the caller's own mid-stream recovery
        path in :meth:`events`, never for this method's ``initial``
        connection, however many attempts that took)."""
        if self._subscription is None:
            raise LiveStreamClosedError(
                "DatabentoLiveProvider._connect_with_reconnect called with no active subscription."
            )
        subscription = self._subscription
        attempt = 0
        while True:
            try:
                client = self._client_factory()
                _require_databento_live_client_interface(client)
                raw_symbols = [contract.display_code for contract in subscription.contracts]
                for event_type in subscription.event_types:
                    client.subscribe(
                        dataset=self._dataset,
                        schema=event_type.databento_schema,
                        symbols=raw_symbols,
                        stype_in="raw_symbol",
                    )
            except LiveProviderNotConfiguredError:
                raise
            except Exception as exc:
                if not _is_databento_exception(exc):
                    raise
                translated = _translate_databento_exception(exc)
                if isinstance(translated, (LiveProviderAuthenticationError, LiveProviderPermissionError)):
                    self._state = ConnectionState.FAILED
                    raise translated
                attempt += 1
                if attempt > self._reconnect_policy.max_attempts:
                    self._state = ConnectionState.FAILED
                    raise LiveReconnectExhaustedError(
                        f"Databento live connection failed after {attempt - 1} reconnect attempt(s); "
                        "giving up."
                    ) from None
                self._state = ConnectionState.RECONNECTING
                delay = self._reconnect_policy.delay_for_attempt(attempt)
                self._sleep_fn(float(delay))
                continue
            self._reconnect_attempts_total += attempt
            return client

    def close(self) -> None:
        """Idempotent, safe shutdown.

        Phase 4.2 correction §3: Phase 4's own earlier correction
        (replacing a blanket ``except Exception: pass`` with a check for
        any ``databento``-origin exception) was still too broad -- a
        vendor-origin exception during shutdown is not automatically a
        harmless, already-expected condition. This now splits into three
        cases:

        1. A ``databento``-origin exception whose message matches a
           narrow, specifically recognized benign shutdown condition
           (e.g. "already stopped," "connection already closed," or an
           equivalent -- see :func:`_is_benign_shutdown_exception`) is
           suppressed.
        2. A ``databento``-origin exception that does NOT match one of
           those recognized conditions is an unexpected vendor failure;
           it is never silently discarded. It is translated into a
           sanitized, Olive-owned :class:`LiveProviderShutdownError` and
           raised (never leaking vendor internals/secrets).
        3. A non-``databento``-origin exception (a genuine programming
           bug -- the wrong method name, a bad call signature) always
           propagates unchanged, exactly as before.

        ``finally`` guarantees the provider still reaches a coherent
        ``STOPPED`` state regardless of which path is taken."""
        if self._client is None:
            self._state = ConnectionState.STOPPED
            return
        client = self._client
        self._client = None
        try:
            client.stop()
        except Exception as exc:
            if not _is_databento_exception(exc):
                raise
            if _is_benign_shutdown_exception(exc):
                # A recognized, already-expected benign shutdown
                # condition (e.g. stopping an already-stopped/degraded
                # connection) must never prevent Olive from observing a
                # clean STOPPED state.
                pass
            else:
                raise LiveProviderShutdownError(
                    "Databento client reported an unexpected error while being stopped "
                    f"(vendor exception type: {type(exc).__name__})."
                ) from None
        finally:
            self._state = ConnectionState.STOPPED

    def _best_effort_stop_old_client(self, client: Any) -> None:
        """Best-effort cleanup of an OLD client being discarded during
        mid-stream reconnect (Phase 4.2 correction §4).

        Deliberately MORE permissive than :meth:`close`'s shutdown
        policy: this client has already failed (the caller is here
        because iterating it just raised) and is about to be replaced,
        so forcing a hard failure out of its cleanup would turn one
        already-observed transient failure into an unrelated second
        one. Any ``databento``-origin exception raised while stopping
        it -- recognized-benign or not -- is therefore absorbed here,
        never raised as :class:`LiveProviderShutdownError`. A
        non-``databento``-origin exception (a genuine programming bug)
        still always propagates, exactly as it would from
        :meth:`close`, and never leaks any vendor secret since nothing
        about the exception is included in a raised message here."""
        if client is None:
            return
        try:
            client.stop()
        except Exception as exc:
            if not _is_databento_exception(exc):
                raise
            # Any vendor-origin failure while tearing down an
            # already-broken, about-to-be-discarded client is absorbed:
            # best-effort cleanup must never block establishing the
            # replacement connection.

    # -- Event consumption ------------------------------------------------

    def events(self) -> Iterator[LiveEvent]:
        """Consume the live stream.

        Phase 4 correction §2-3: relies on ``Live.__iter__()``'s
        documented auto-start -- this adapter never calls ``.start()``
        anywhere (see :meth:`_connect_with_reconnect`)."""
        if self._client is None or self._state not in (ConnectionState.CONNECTED, ConnectionState.DEGRADED):
            raise LiveStreamClosedError(
                "DatabentoLiveProvider.events() was called before a successful connect(), or "
                "after the stream was closed."
            )
        while True:
            try:
                record_iterator = iter(self._client)
                for record in record_iterator:
                    yield from self._handle_record(record)
            except LiveDataError:
                raise
            except Exception as exc:
                if not _is_databento_exception(exc):
                    raise
                translated = _translate_databento_exception(exc)
                if isinstance(translated, (LiveProviderAuthenticationError, LiveProviderPermissionError)):
                    self._state = ConnectionState.FAILED
                    raise translated
                # Transient mid-stream failure: attempt to re-establish
                # the connection and resume consumption. This is the
                # ONLY place reconnect_count is incremented -- by
                # exactly 1 per successful recovery, regardless of how
                # many internal attempts _connect_with_reconnect needed
                # (those are tallied separately into
                # reconnect_attempts_total) -- because this is the only
                # place a PRIOR, already-successful connection was lost
                # and then re-established (Phase 4 correction §6).
                self._state = ConnectionState.RECONNECTING
                # Phase 4.2 correction §4: explicitly clean up the OLD
                # client before replacing it. An iterator exception does
                # not guarantee every underlying resource/connection on
                # the old client has already been released -- so this
                # adapter no longer just drops the reference and hopes.
                old_client = self._client
                self._best_effort_stop_old_client(old_client)
                try:
                    new_client = self._connect_with_reconnect(initial=False)
                except LiveProviderError:
                    self._state = ConnectionState.FAILED
                    raise
                # reconnect_count is only incremented -- and self._client
                # only reassigned to the new client -- once a genuinely
                # new client has been successfully established, never
                # before (Phase 4.2 correction §4).
                self._client = new_client
                self._reconnect_count += 1
                self._state = ConnectionState.CONNECTED
                continue
            # The client's own iterator ended without error (a graceful
            # upstream close) -- treat this as a clean stream end, not
            # a reconnect-eligible failure.
            self._state = ConnectionState.STOPPED
            return

    def _handle_record(self, record: Any) -> Iterator[LiveEvent]:
        self._events_received += 1
        now = datetime.now(timezone.utc)
        self._last_receive_at = now

        kind = _classify_record(record)

        if kind == "error":
            self._events_rejected += 1
            code_name = _error_code_name(record)
            if code_name in _FATAL_ERROR_CODE_NAMES:
                translated = _translate_error_record(code_name)
                self._state = ConnectionState.FAILED
                raise translated
            if code_name in _DATA_GAP_ERROR_CODE_NAMES:
                # Non-session-fatal but represents real, documented
                # market-data loss -- Olive must never continue as
                # though the stream remained fully healthy. Surfaced
                # explicitly via both a dedicated counter and a
                # DEGRADED connection state (Phase 4 correction §5).
                self._data_gap_count += 1
                self._state = ConnectionState.DEGRADED
                return
            # Any other non-fatal ErrorMsg (e.g. SYMBOL_RESOLUTION_FAILED)
            # does not close the connection -- Databento's own
            # documented behavior is that streaming continues.
            return

        if kind == "system":
            # Heartbeats/subscription-acks/replay-completion notices
            # carry no market data -- counted as received, never
            # accepted/rejected, never yielded.
            return

        if kind == "symbol_mapping":
            self._record_symbol_mapping(record)
            return

        if kind == "unknown":
            # Phase 4 correction §10/§15: an unrecognized record must
            # never silently fall through into trade/quote/bar
            # normalization (which, before this fix, assumed any
            # record reaching that point was a bar). Explicitly
            # rejected and counted instead.
            self._events_rejected += 1
            return

        instrument_id = _require_instrument_id(getattr(record, "instrument_id", None))
        if instrument_id is None:
            self._events_rejected += 1
            return
        contract = self._instrument_id_to_contract.get(instrument_id)
        if contract is None:
            self._events_rejected += 1
            return

        try:
            event = self._normalize_record(kind, record, contract, olive_received_at=now)
        except LiveProviderDataError:
            self._events_rejected += 1
            return

        self._events_accepted += 1
        self._last_event_at = event.ts_event
        if self._state is ConnectionState.DEGRADED:
            # A fresh, successfully-accepted event proves the stream
            # has recovered from the data-gap condition that put it
            # into DEGRADED; data_gap_count itself is never
            # decremented (it is a cumulative historical tally).
            self._state = ConnectionState.CONNECTED
        yield event

    def _record_symbol_mapping(self, record: Any) -> None:
        if self._subscription is None:
            self._events_rejected += 1
            return
        raw_symbol = getattr(record, "stype_in_symbol", None)
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            self._events_rejected += 1
            return
        instrument_id = _require_instrument_id(getattr(record, "instrument_id", None))
        if instrument_id is None:
            self._events_rejected += 1
            return

        candidates = [contract for contract in self._subscription.contracts if contract.display_code == raw_symbol]
        if not candidates:
            # A mapping for a symbol Olive never subscribed to -- never
            # trusted, never merged into the identity map.
            self._events_rejected += 1
            return

        matched_contract = None
        for contract in candidates:
            expected_id = self._expected_instrument_ids.get(contract.identity)
            if expected_id is not None and expected_id == instrument_id:
                matched_contract = contract
                break

        if matched_contract is None:
            # Phase 4 correction §4: every candidate contract sharing
            # this raw symbol disagrees with its own pre-verified,
            # authoritative identity (established in connect() before
            # this stream was ever opened) -- fail closed rather than
            # guess which contract (if any) this live mapping was
            # "really" for. This is what stops a single contract from
            # being labeled with an arbitrary full year merely because
            # the live stream's own SymbolMappingMsg says so.
            raise LiveIdentityUnresolvedError(
                f"Databento live SymbolMappingMsg mapped raw symbol {raw_symbol!r} to "
                f"instrument_id {instrument_id!r}, which contradicts every subscribed "
                "contract's own pre-verified authoritative identity for that symbol; refusing "
                "to trust an unconfirmed live mapping."
            )

        existing = self._instrument_id_to_contract.get(instrument_id)
        if existing is not None and existing.identity != matched_contract.identity:
            # A contradictory remapping of an already-established
            # instrument_id must fail closed rather than silently
            # overwrite a previously trusted mapping.
            raise LiveContractIdentityError(
                f"Databento live stream remapped instrument_id {instrument_id!r} from "
                f"contract {existing.identity} to {matched_contract.identity}; refusing to "
                "trust a contradictory identity mapping."
            )
        self._instrument_id_to_contract[instrument_id] = matched_contract

    def _normalize_record(
        self, kind: str, record: Any, contract: FuturesContract, *, olive_received_at: datetime
    ) -> LiveEvent:
        instrument = self._instrument_map[contract.identity]
        raw_symbol = contract.display_code
        ts_event = _ts_event_from_nanos(getattr(record, "ts_event", None), field_name="ts_event")
        ts_recv_raw = getattr(record, "ts_recv", None)
        ts_recv = _ts_event_from_nanos(ts_recv_raw, field_name="ts_recv") if ts_recv_raw is not None else None
        provider_instrument_id = _require_instrument_id(getattr(record, "instrument_id", None))
        sequence_raw = getattr(record, "sequence", None)
        sequence = _safe_size(sequence_raw, field_name="sequence") if sequence_raw is not None else None

        if kind == "trade":
            try:
                price = _price_from_fixed_point(getattr(record, "price", None), field_name="price")
                price = _require_tick_aligned(price, instrument.tick_size, field_name="LiveTrade.price")
                size = _safe_size(getattr(record, "size", None), field_name="size")
                return LiveTrade(
                    contract=contract,
                    provider=self.name,
                    dataset=self._dataset,
                    provider_raw_symbol=raw_symbol,
                    ts_event=ts_event,
                    ts_recv=ts_recv,
                    olive_received_at=olive_received_at,
                    price=price,
                    size=size,
                    provider_instrument_id=provider_instrument_id,
                    sequence=sequence,
                )
            except LiveProviderDataError:
                raise
            except LiveDataError as exc:
                raise LiveProviderDataError(f"Databento trade record failed Olive's normalization: {exc}") from None

        if kind == "quote":
            try:
                bid_px_raw, ask_px_raw, bid_sz_raw, ask_sz_raw = _mbp1_top_of_book_fields(record)
                # Databento represents an empty book side with size=0
                # (the documented, unambiguous "no resting quantity"
                # signal) -- the SIZE, not the price sentinel, is what
                # this adapter gates presence on, since Databento's
                # documented "no price" sentinel value is not something
                # this adapter can verify without the real installed
                # package (see docs/realtime_data.md "Known
                # limitations"). A side is present only when its size
                # is a true, strictly positive int.
                bid_size = _safe_size(bid_sz_raw, field_name="bid_sz") if bid_sz_raw is not None else 0
                ask_size = _safe_size(ask_sz_raw, field_name="ask_sz") if ask_sz_raw is not None else 0
                bid_price = (
                    _require_tick_aligned(
                        _price_from_fixed_point(bid_px_raw, field_name="bid_px"),
                        instrument.tick_size,
                        field_name="LiveQuote.bid_price",
                    )
                    if bid_size > 0
                    else None
                )
                ask_price = (
                    _require_tick_aligned(
                        _price_from_fixed_point(ask_px_raw, field_name="ask_px"),
                        instrument.tick_size,
                        field_name="LiveQuote.ask_price",
                    )
                    if ask_size > 0
                    else None
                )
                bid_size = bid_size if bid_price is not None else None
                ask_size = ask_size if ask_price is not None else None
                return LiveQuote(
                    contract=contract,
                    provider=self.name,
                    dataset=self._dataset,
                    provider_raw_symbol=raw_symbol,
                    ts_event=ts_event,
                    ts_recv=ts_recv,
                    olive_received_at=olive_received_at,
                    bid_price=bid_price,
                    bid_size=bid_size,
                    ask_price=ask_price,
                    ask_size=ask_size,
                    provider_instrument_id=provider_instrument_id,
                    sequence=sequence,
                )
            except LiveProviderDataError:
                raise
            except LiveDataError as exc:
                raise LiveProviderDataError(f"Databento quote record failed Olive's normalization: {exc}") from None

        # kind == "bar"
        try:
            open_price = _require_tick_aligned(
                _price_from_fixed_point(getattr(record, "open", None), field_name="open"),
                instrument.tick_size,
                field_name="LiveBar.open",
            )
            high_price = _require_tick_aligned(
                _price_from_fixed_point(getattr(record, "high", None), field_name="high"),
                instrument.tick_size,
                field_name="LiveBar.high",
            )
            low_price = _require_tick_aligned(
                _price_from_fixed_point(getattr(record, "low", None), field_name="low"),
                instrument.tick_size,
                field_name="LiveBar.low",
            )
            close_price = _require_tick_aligned(
                _price_from_fixed_point(getattr(record, "close", None), field_name="close"),
                instrument.tick_size,
                field_name="LiveBar.close",
            )
            volume = _safe_size(getattr(record, "volume", None), field_name="volume")
            return LiveBar(
                contract=contract,
                provider=self.name,
                dataset=self._dataset,
                provider_raw_symbol=raw_symbol,
                interval=HistoricalTimeframe.ONE_SECOND,
                ts_event=ts_event,
                ts_recv=ts_recv,
                olive_received_at=olive_received_at,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                provider_instrument_id=provider_instrument_id,
            )
        except LiveProviderDataError:
            raise
        except LiveDataError as exc:
            raise LiveProviderDataError(f"Databento bar record failed Olive's normalization: {exc}") from None
