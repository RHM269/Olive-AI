"""The safe default historical market-data provider: Olive has none
configured.

Used whenever ``OLIVE_HISTORICAL_PROVIDER`` is unset/``unconfigured``,
or a provider was selected but is missing required configuration
(e.g. ``databento`` selected with no ``DATABENTO_API_KEY``, or with
the ``databento`` package unavailable) -- see
``app.data.providers.factory``. Never makes a network call, never
pretends to have real data, and never silently substitutes a fake
provider for a misconfigured real one: every operation raises a
clear, explicit error naming the actual reason.
"""

from __future__ import annotations

from app.futures.models import FuturesInstrument
from app.data.models import HistoricalBar, HistoricalBarRequest, ProviderNotConfiguredError
from app.data.provider_base import CostEstimate, HistoricalMarketDataProvider


class UnconfiguredHistoricalProvider(HistoricalMarketDataProvider):
    """A historical provider that is explicitly, honestly, not
    configured. Every method raises :class:`ProviderNotConfiguredError`
    with ``reason`` -- never returns fabricated data, never attempts
    any network call."""

    def __init__(self, reason: str = "No historical data provider is configured.") -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ProviderNotConfiguredError("UnconfiguredHistoricalProvider requires a non-empty reason string")
        self._reason = reason.strip()

    @property
    def name(self) -> str:
        return "unconfigured"

    @property
    def reason(self) -> str:
        return self._reason

    def estimate_cost(self, request: HistoricalBarRequest) -> CostEstimate:
        raise ProviderNotConfiguredError(self._reason)

    def fetch_bars(self, request: HistoricalBarRequest, instrument: FuturesInstrument) -> tuple[HistoricalBar, ...]:
        raise ProviderNotConfiguredError(self._reason)
