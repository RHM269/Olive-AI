"""Olive's real-time-data orchestration service (Phase 4).

``RealTimeDataService`` is the one place that coordinates every safety
gate a live subscription must pass before any provider connection can
be opened -- mirroring ``app.data.service.HistoricalDataService``'s
role for historical fetches, but intentionally thinner: a live stream
has no single "fetch result" to return (data arrives continuously), so
this service's job ends at handing the caller back a connected
:class:`~app.data.live_provider_base.RealTimeMarketDataProvider` to
consume via its own ``events()``/``status``/``close()`` -- it does not
wrap that provider in a second abstraction, and it does not itself
implement a consumption loop, a daemon, or any scheduling (reserved
for Phase 13).

Responsibilities deliberately kept OUT of
``app.data.providers.databento_live`` (the adapter stays a thin
translator of one vendor's wire protocol):

1. Olive production tradable-domain gate (``app.futures.validation`` +
   ``app.data.validation.require_olive_tradable_contract``) -- applied
   to EVERY contract in the subscription, not just the first.
2. Subscription type/shape validation (already enforced by
   :class:`~app.data.live_models.LiveSubscriptionRequest`'s own
   construction, re-checked here defensively for a caller-supplied
   value of unknown type).
3. Provider configuration / network-opt-in safety gates (see
   docs/realtime_data.md "Network opt-in").
4. Market-session-aware staleness interpretation (reusing Phase 2's
   existing CME Globex session schedule, never a second calendar).

No market intelligence, indicators, strategies, or signals are
implemented here or anywhere else in this phase.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.config import Settings
from app.futures.calendar import CycleDateCalendar, load_cycle_date_calendar
from app.futures.models import FuturesDomainError, FuturesInstrument
from app.futures.registry import FuturesInstrumentRegistry, load_instrument_registry
from app.futures.sessions import SessionState, session_state_at
from app.futures.validation import validate_olive_futures_domain

from app.data.models import HistoricalDataError
from app.data.live_models import (
    InvalidLiveServiceConfigurationError,
    InvalidLiveSubscriptionError,
    LiveNetworkDisabledError,
    LiveProviderNotConfiguredError,
    LiveStreamStatus,
    LiveSubscriptionRequest,
    _require_live_subscription_request,
)
from app.data.live_provider_base import RealTimeMarketDataProvider
from app.data.providers.unconfigured_live import UnconfiguredLiveProvider
from app.data.validation import require_olive_tradable_contract


class RealTimeDataService:
    """Olive's real-time-data orchestration service.

    ``provider``, ``registry``, and ``calendar`` are all injectable so
    tests never need a real Databento client or even the real on-disk
    Phase 2 configuration files -- see ``tests/test_live_data_service.py``.
    When not supplied, ``registry``/``calendar`` are loaded from
    Olive's real Phase 2 configuration (a local, version-controlled
    JSON read -- never network I/O).
    """

    def __init__(
        self,
        provider: RealTimeMarketDataProvider,
        settings: Settings,
        *,
        registry: Optional[FuturesInstrumentRegistry] = None,
        calendar: Optional[CycleDateCalendar] = None,
    ) -> None:
        if not isinstance(provider, RealTimeMarketDataProvider):
            raise InvalidLiveServiceConfigurationError(
                f"RealTimeDataService requires a RealTimeMarketDataProvider, got "
                f"{type(provider).__name__}"
            )
        if not isinstance(settings, Settings):
            raise InvalidLiveServiceConfigurationError(
                f"RealTimeDataService requires a Settings instance, got {type(settings).__name__}"
            )
        if registry is not None and not isinstance(registry, FuturesInstrumentRegistry):
            raise InvalidLiveServiceConfigurationError(
                f"RealTimeDataService's registry, when supplied, must be a "
                f"FuturesInstrumentRegistry, got {type(registry).__name__}"
            )
        if calendar is not None and not isinstance(calendar, CycleDateCalendar):
            raise InvalidLiveServiceConfigurationError(
                f"RealTimeDataService's calendar, when supplied, must be a CycleDateCalendar, "
                f"got {type(calendar).__name__}"
            )

        self._provider = provider
        self._settings = settings
        self._registry = registry if registry is not None else load_instrument_registry()
        self._calendar = calendar if calendar is not None else load_cycle_date_calendar()

    @property
    def provider(self) -> RealTimeMarketDataProvider:
        """The underlying provider this service coordinates. Exposed
        read-only so a caller can inspect :attr:`status`/``state``
        without the service needing to re-expose every provider
        method itself."""
        return self._provider

    def open_stream(self, subscription: object) -> RealTimeMarketDataProvider:
        """Run ``subscription`` through every safety gate and open the
        real-time stream, returning the now-connected provider for the
        caller to consume via its own ``events()``/``status``/
        ``close()``.

        Raises a specific :class:`~app.data.live_models.LiveDataError`
        subclass for every expected failure mode (a bad subscription,
        an untradable contract, a misconfigured provider, network
        disabled, a provider connection failure) -- never returns a
        "maybe connected" provider; either this call raises, or the
        returned provider's ``state`` is ``ConnectionState.CONNECTED``.
        """
        subscription = _require_live_subscription_request(subscription, context="RealTimeDataService.open_stream")

        try:
            validate_olive_futures_domain(self._registry, self._calendar)
        except FuturesDomainError as exc:
            raise InvalidLiveServiceConfigurationError(
                f"Olive's futures domain is not production-valid: {exc}"
            ) from exc

        instruments: dict[str, FuturesInstrument] = {}
        for contract in subscription.contracts:
            try:
                instruments[contract.identity] = require_olive_tradable_contract(contract, self._registry)
            except HistoricalDataError as exc:
                # require_olive_tradable_contract raises from the
                # historical-data error hierarchy (it is Phase 3's
                # existing, single-source-of-truth production gate,
                # reused here rather than duplicated) -- translate it
                # into this subsystem's own LiveDataError hierarchy so
                # a caller of this live service only ever needs to
                # catch LiveDataError.
                raise InvalidLiveSubscriptionError(
                    f"Subscribed contract {contract.identity} is not Olive production-tradable: {exc}"
                ) from exc

        if isinstance(self._provider, UnconfiguredLiveProvider):
            raise LiveProviderNotConfiguredError(self._provider.reason)

        if not self._settings.live_network_enabled:
            raise LiveNetworkDisabledError(
                "OLIVE_LIVE_NETWORK_ENABLED is false; refusing to open a live connection."
            )

        self._provider.connect(subscription, instruments)
        return self._provider

    def close_stream(self) -> None:
        """Convenience passthrough to the underlying provider's
        ``close()`` -- idempotent, safe to call whether or not a
        stream was ever successfully opened."""
        self._provider.close()

    @property
    def status(self) -> LiveStreamStatus:
        """Passthrough to the underlying provider's current
        :class:`~app.data.live_models.LiveStreamStatus`."""
        return self._provider.status

    def is_feed_unexpectedly_stale(self, *, at: Optional[datetime] = None) -> bool:
        """Whether the current stream is stale for a reason OTHER than
        the CME Globex session being legitimately closed right now.

        Reuses Phase 2's existing session schedule
        (``app.futures.sessions.session_state_at``) rather than
        inventing a second exchange calendar (Phase 4 prompt §29):
        during ``MAINTENANCE``/``WEEKEND_CLOSED``, an absence of fresh
        events is expected, not a feed problem, so this returns
        ``False`` even if the provider's own ``status.is_stale`` is
        ``True``. During ``OPEN``, the provider's own staleness
        determination is trusted as-is.
        """
        status = self._provider.status
        if not status.is_stale:
            return False
        check_time = at if at is not None else datetime.now(timezone.utc)
        session_state = session_state_at(check_time)
        if session_state in (SessionState.MAINTENANCE, SessionState.WEEKEND_CLOSED):
            return False
        return True
