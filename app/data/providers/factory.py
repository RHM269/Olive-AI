"""Builds the configured historical market-data provider from Olive's
settings.

The single place that decides which concrete
:class:`~app.data.provider_base.HistoricalMarketDataProvider` Olive
uses. Never makes a network call itself. Never silently substitutes a
fake/placeholder provider for a misconfigured real one -- every path
that cannot produce a working Databento provider returns an honestly
:class:`~app.data.providers.unconfigured.UnconfiguredHistoricalProvider`
naming the actual reason (no API key, package not installed, ...),
which itself still raises on every operation rather than returning
fabricated data.
"""

from __future__ import annotations

from app.config import HistoricalProviderKind, Settings
from app.data.models import InvalidHistoricalServiceConfigurationError
from app.data.provider_base import HistoricalMarketDataProvider
from app.data.providers.unconfigured import UnconfiguredHistoricalProvider


def build_historical_provider(settings: Settings) -> HistoricalMarketDataProvider:
    """Build the historical provider ``settings`` describes.

    Importing this module, or calling this function, never performs
    network I/O and never constructs a live provider client unless
    ``settings`` genuinely selects a real provider with the
    configuration that provider requires (e.g. a non-empty API key for
    Databento) -- see docs/historical_data.md "Network opt-in".
    """
    # Phase 3.1 §17: Olive-owned error, never a raw TypeError, at this
    # constructor-adjacent boundary too.
    if not isinstance(settings, Settings):
        raise InvalidHistoricalServiceConfigurationError(
            f"build_historical_provider expects a Settings instance, got {type(settings).__name__}"
        )

    if settings.historical_provider is HistoricalProviderKind.UNCONFIGURED:
        return UnconfiguredHistoricalProvider("OLIVE_HISTORICAL_PROVIDER is not set (default: unconfigured).")

    if settings.historical_provider is HistoricalProviderKind.DATABENTO:
        if not settings.has_databento_api_key:
            return UnconfiguredHistoricalProvider(
                "OLIVE_HISTORICAL_PROVIDER=databento is selected, but DATABENTO_API_KEY is not set."
            )
        try:
            import databento  # noqa: F401  (existence check only; see providers/databento.py)
        except ImportError:
            return UnconfiguredHistoricalProvider(
                "OLIVE_HISTORICAL_PROVIDER=databento is selected and an API key is configured, "
                "but the 'databento' package is not installed in this environment."
            )
        from app.data.providers.databento import DatabentoHistoricalProvider

        return DatabentoHistoricalProvider(api_key=settings.databento_api_key)

    # Defensive: HistoricalProviderKind is a closed enum validated by
    # app.config, so this is unreachable with a genuine Settings
    # instance -- but never silently fall through to a fake provider
    # for an unrecognized value either.
    raise ValueError(f"Unrecognized historical provider kind: {settings.historical_provider!r}")  # pragma: no cover
