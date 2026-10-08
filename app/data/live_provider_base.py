"""Olive's provider-independent real-time market-data interface
(Phase 4).

Mirrors the design of ``app.data.provider_base`` for historical data:
``RealTimeDataService`` (``app.data.live_service``) depends only on
this module, never on a concrete provider -- so Databento can be
replaced or a second real-time provider added later without rewriting
subscription validation, contract-identity enforcement, or service
orchestration. Only a new ``RealTimeMarketDataProvider`` implementation
(plus a factory branch in ``app.data.providers.live_factory``) is
needed.

Deliberately does NOT impose the always-on supervisor/daemon
architecture reserved for Phase 13. A provider instance here
represents exactly one subscribed stream's lifecycle; the caller owns
the consumption loop by iterating :meth:`RealTimeMarketDataProvider.events`
and deciding when to call :meth:`RealTimeMarketDataProvider.close`.
Importing this module, constructing a provider, or calling
``connect`` with networking disabled must never itself start a
background thread, task, or any other form of implicit concurrency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping

from app.futures.models import FuturesInstrument
from app.data.live_models import ConnectionState, LiveEvent, LiveStreamStatus, LiveSubscriptionRequest


class RealTimeMarketDataProvider(ABC):
    """Olive's provider-independent real-time market-data interface.

    Every concrete implementation must:

    - accept Olive's own domain objects (:class:`LiveSubscriptionRequest`,
      a mapping of already-production-validated
      :class:`~app.futures.models.FuturesInstrument` keyed by
      ``contract.identity``) and derive any vendor-specific
      symbology/schema/dataset internally -- never accept or require a
      raw provider symbol string from the caller.
    - translate every expected vendor failure (authentication,
      permission, rate limit, timeout, invalid subscription, stream
      closed, unmapped/contradictory instrument identity) into one of
      ``app.data.live_models``'s own :class:`~app.data.live_models.LiveDataError`
      subclasses -- never leak a raw vendor exception, and never
      broadly swallow an unexpected *programming* error.
    - never include API keys/credentials in any raised error message,
      log line, or :attr:`status` detail.
    - yield already-normalized live event objects
      (:class:`~app.data.live_models.LiveTrade`,
      :class:`~app.data.live_models.LiveQuote`,
      :class:`~app.data.live_models.LiveBar`) from :meth:`events` --
      never a raw vendor record. Normalization happens at the adapter
      boundary so the rest of Olive need not know any vendor's wire
      format.
    - never open a real network connection from ``__init__``,
      ``name``, ``state``, or ``status`` -- only :meth:`connect` may
      do so, and only after the caller (``RealTimeDataService``) has
      already enforced Olive's network-enablement kill switch.
    - make :meth:`close` safe to call multiple times, including
      before a successful :meth:`connect` -- it must never raise
      merely because the stream was never opened or was already
      closed.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """A short, stable, human-readable provider name (e.g.
        ``"databento"``, ``"unconfigured"``) for health/status/log
        reporting. Never includes or implies a secret value."""

    @property
    @abstractmethod
    def state(self) -> ConnectionState:
        """The provider's current connection-lifecycle state. Must be
        ``ConnectionState.DISCONNECTED`` before :meth:`connect` has
        ever been called successfully."""

    @property
    @abstractmethod
    def status(self) -> LiveStreamStatus:
        """Provider-neutral liveness/staleness telemetry for the
        current (or most recently closed) stream. Must never contain
        a secret value anywhere in its fields."""

    @abstractmethod
    def connect(
        self,
        subscription: LiveSubscriptionRequest,
        instruments: Mapping[str, FuturesInstrument],
    ) -> None:
        """Open the real-time stream for ``subscription``.

        ``instruments`` maps each subscribed contract's
        ``contract.identity`` to Olive's already-production-validated
        :class:`~app.futures.models.FuturesInstrument` for that
        contract's root (see
        ``app.data.validation.require_olive_tradable_contract``) --
        providers need this for tick-size/economic context and must
        never derive or guess it independently.

        Must raise before any network call is attempted if Olive's
        real-time network kill switch is disabled, if the provider is
        not configured, or if ``subscription``/``instruments`` are
        invalid or inconsistent with each other. Must raise a
        provider-specific :class:`~app.data.live_models.LiveProviderError`
        subclass (never a raw vendor exception) if the underlying
        connection attempt itself fails.
        """

    @abstractmethod
    def events(self) -> Iterator[LiveEvent]:
        """Return an iterator yielding normalized live events as they
        arrive from the open stream.

        Must only be called after a successful :meth:`connect`. Each
        yielded object is already contract-identity-enforced,
        type-validated, and labeled ``DataLabel.LIVE`` -- data for an
        instrument that is not part of ``subscription`` must never be
        yielded; it is either dropped (with ``status.events_rejected``
        incremented) or raised as a
        :class:`~app.data.live_models.LiveContractIdentityError`,
        per the concrete provider's own documented policy.

        This method does not itself manage a background thread or
        internal buffer unless a concrete provider's docstring says
        otherwise and explains why -- the default expectation is a
        thin, unbuffered, pass-through iterator so no unbounded queue
        can ever accumulate.
        """

    @abstractmethod
    def close(self) -> None:
        """Cleanly stop the stream and release any underlying
        connection resources.

        Must be idempotent: calling it when already closed, or before
        :meth:`connect` was ever called, must succeed silently rather
        than raising. Must never leave the underlying connection
        half-open on return.
        """
