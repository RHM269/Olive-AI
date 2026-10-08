"""The safe default real-time market-data provider: Olive has none
configured (Phase 4).

Used whenever ``OLIVE_LIVE_PROVIDER`` is unset/``unconfigured``, or a
provider was selected but is missing required configuration (e.g.
``databento`` selected with no ``DATABENTO_API_KEY``, or with the
``databento`` package unavailable) -- see
``app.data.providers.live_factory``. Never makes a network call,
never pretends to have a live connection, and never silently
substitutes a fake provider for a misconfigured real one: every
operation raises a clear, explicit error naming the actual reason.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from app.futures.models import FuturesInstrument
from app.data.live_models import (
    ConnectionState,
    LiveEvent,
    LiveProviderNotConfiguredError,
    LiveStreamStatus,
    LiveSubscriptionRequest,
)
from app.data.live_provider_base import RealTimeMarketDataProvider


class UnconfiguredLiveProvider(RealTimeMarketDataProvider):
    """A real-time provider that is explicitly, honestly, not
    configured. Every operation raises
    :class:`LiveProviderNotConfiguredError` with ``reason`` -- never
    returns fabricated events, never attempts any network call, and
    always reports :attr:`state` as ``ConnectionState.DISCONNECTED``.
    """

    def __init__(self, reason: str = "No real-time data provider is configured.") -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise LiveProviderNotConfiguredError(
                "UnconfiguredLiveProvider requires a non-empty reason string"
            )
        self._reason = reason.strip()

    @property
    def name(self) -> str:
        return "unconfigured"

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def state(self) -> ConnectionState:
        return ConnectionState.DISCONNECTED

    @property
    def status(self) -> LiveStreamStatus:
        return LiveStreamStatus(state=ConnectionState.DISCONNECTED)

    def connect(
        self,
        subscription: LiveSubscriptionRequest,
        instruments: Mapping[str, FuturesInstrument],
    ) -> None:
        raise LiveProviderNotConfiguredError(self._reason)

    def events(self) -> Iterator[LiveEvent]:
        raise LiveProviderNotConfiguredError(self._reason)

    def close(self) -> None:
        # Idempotent and always safe -- there is nothing to close.
        return None
