"""Builds the configured real-time market-data provider from Olive's
settings (Phase 4).

The single place that decides which concrete
:class:`~app.data.live_provider_base.RealTimeMarketDataProvider` Olive
uses. Mirrors ``app.data.providers.factory.build_historical_provider``
exactly: never makes a network call itself, and never silently
substitutes a fake/placeholder provider for a misconfigured real one
-- every path that cannot produce a working Databento live provider
returns an honestly
:class:`~app.data.providers.unconfigured_live.UnconfiguredLiveProvider`
naming the actual reason (no API key, package not installed, ...).

``DATABENTO_API_KEY`` is intentionally REUSED from the historical
subsystem's configuration -- Phase 4 does not duplicate secret
configuration. ``OLIVE_LIVE_NETWORK_ENABLED`` is checked only by
``app.data.live_service.RealTimeDataService.open_stream``, never here
-- this factory's job is solely to decide WHICH provider object to
construct, not whether a connection may currently be opened.
"""

from __future__ import annotations

from app.config import LiveProviderKind, Settings
from app.data.live_models import InvalidLiveServiceConfigurationError
from app.data.live_provider_base import RealTimeMarketDataProvider
from app.data.providers.unconfigured_live import UnconfiguredLiveProvider


def build_live_provider(settings: Settings) -> RealTimeMarketDataProvider:
    """Build the real-time provider ``settings`` describes.

    Importing this module, or calling this function, never performs
    network I/O and never opens a live connection -- see
    ``app.data.providers.databento_live.DatabentoLiveProvider``'s own
    guarantee that constructing it never calls its client factory.
    """
    if not isinstance(settings, Settings):
        raise InvalidLiveServiceConfigurationError(
            f"build_live_provider expects a Settings instance, got {type(settings).__name__}"
        )

    if settings.live_provider is LiveProviderKind.UNCONFIGURED:
        return UnconfiguredLiveProvider("OLIVE_LIVE_PROVIDER is not set (default: unconfigured).")

    if settings.live_provider is LiveProviderKind.DATABENTO:
        if not settings.has_databento_api_key:
            return UnconfiguredLiveProvider(
                "OLIVE_LIVE_PROVIDER=databento is selected, but DATABENTO_API_KEY is not set."
            )
        try:
            import databento  # noqa: F401  (existence check only; see providers/databento_live.py)
        except ImportError:
            return UnconfiguredLiveProvider(
                "OLIVE_LIVE_PROVIDER=databento is selected and an API key is configured, but "
                "the 'databento' package is not installed in this environment."
            )
        from app.data.providers.databento_live import DatabentoLiveProvider, ReconnectPolicy

        reconnect_policy = ReconnectPolicy(
            max_attempts=settings.live_reconnect_max_attempts,
            base_delay_seconds=settings.live_reconnect_base_delay_seconds,
            max_delay_seconds=settings.live_reconnect_max_delay_seconds,
        )
        return DatabentoLiveProvider(
            api_key=settings.databento_api_key,
            reconnect_policy=reconnect_policy,
            stale_threshold_seconds=settings.live_stale_threshold_seconds,
        )

    # Defensive: LiveProviderKind is a closed enum validated by
    # app.config, so this is unreachable with a genuine Settings
    # instance -- but never silently fall through to a fake provider
    # for an unrecognized value either.
    raise ValueError(f"Unrecognized live provider kind: {settings.live_provider!r}")  # pragma: no cover
