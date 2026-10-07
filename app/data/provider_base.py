"""Olive's provider-independent historical market-data provider
abstraction (Phase 3).

``HistoricalDataService`` depends only on this module, never on a
concrete provider. This is what makes it possible to replace
Databento later -- or add a second provider -- without rewriting
request validation, bar validation, storage, or service
orchestration: only a new ``HistoricalMarketDataProvider``
implementation (plus a factory branch) is needed.

Deliberately the smallest clean interface Olive actually needs for
Phase 3 -- two operations (estimate a request's cost; fetch its
bars), not a general-purpose vendor SDK wrapper. Each concrete
provider is responsible for deriving its own vendor-specific
symbology/schema names internally from Olive's own domain objects --
callers of this interface (``HistoricalDataService``) never construct
or pass a raw provider symbol string.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, DecimalException

from app.futures.models import FuturesInstrument
from app.data.models import HistoricalBar, HistoricalBarRequest, InvalidHistoricalRequestError


def _require_finite_nonnegative_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise InvalidHistoricalRequestError(f"{field_name} must be a Decimal, not a bool; got {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, (int, str)):
        try:
            candidate = Decimal(value)
        except DecimalException as exc:
            raise InvalidHistoricalRequestError(f"{field_name} could not be parsed as a Decimal: {value!r}") from exc
    else:
        raise InvalidHistoricalRequestError(
            f"{field_name} must be a Decimal, int, or numeric str; got {value!r} ({type(value).__name__})"
        )
    if not candidate.is_finite():
        raise InvalidHistoricalRequestError(f"{field_name} must be finite (not NaN/Infinity); got {candidate}")
    if candidate < 0:
        raise InvalidHistoricalRequestError(f"{field_name} must not be negative; got {candidate}")
    return candidate


@dataclass(frozen=True)
class CostEstimate:
    """A provider's APPROXIMATE estimate of a historical request's
    cost in US dollars -- never called "exact cost", "guaranteed
    cost", or "final billed cost" anywhere in Olive, because provider
    cost estimates are documented by Databento itself as approximate
    in some ranges. Olive's spending-limit comparison
    (``app.data.service.HistoricalDataService``) treats this as a
    conservative estimate to gate against, not a guarantee.
    """

    estimated_cost_usd: Decimal

    def __post_init__(self) -> None:
        validated = _require_finite_nonnegative_decimal(self.estimated_cost_usd, field_name="estimated_cost_usd")
        object.__setattr__(self, "estimated_cost_usd", validated)


class HistoricalMarketDataProvider(ABC):
    """Olive's provider-independent historical market-data interface.

    Every concrete implementation must:

    - accept Olive's own domain objects (:class:`HistoricalBarRequest`,
      :class:`FuturesInstrument`) and derive any vendor-specific
      symbology/schema name internally -- never accept or require a
      raw provider symbol string from the caller.
    - translate every expected vendor failure (authentication,
      permission, rate limit, timeout, invalid symbol/schema, data
      unavailable, cost-estimation failure) into one of this
      package's own :class:`~app.data.models.HistoricalProviderError`
      subclasses -- never leak a raw vendor exception, and never
      broadly swallow an unexpected *programming* error (a bug is not
      a "provider failure").
    - never include API keys/credentials in any raised error message.
    - return already-normalized :class:`HistoricalBar` objects from
      :meth:`fetch_bars` -- never a raw vendor record, DataFrame row,
      or dict. Normalization happens at the adapter boundary so the
      rest of Olive need not know any vendor's response shape.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """A short, stable, human-readable provider name (e.g.
        ``"databento"``, ``"unconfigured"``) for health/manifest/log
        reporting. Never includes or implies a secret value."""

    @abstractmethod
    def estimate_cost(self, request: HistoricalBarRequest) -> CostEstimate:
        """Estimate the USD cost of fetching ``request`` WITHOUT
        performing the (potentially paid) fetch itself.

        Must be safe to call purely for its cost information -- it
        must never itself trigger the paid retrieval call.
        """

    @abstractmethod
    def fetch_bars(self, request: HistoricalBarRequest, instrument: FuturesInstrument) -> tuple[HistoricalBar, ...]:
        """Fetch and normalize ``request``'s historical bars.

        ``instrument`` is Olive's already-production-validated
        :class:`FuturesInstrument` for ``request.contract``'s root
        (see ``app.data.validation.require_olive_tradable_contract``)
        -- providers need its ``tick_size`` to construct exact-tick
        :class:`HistoricalBar` objects via
        :meth:`HistoricalBar.from_decimal_prices`, and must never
        derive or guess it independently.

        An empty result (no trades/records for the requested window)
        is a legitimate, valid return value (an empty tuple) -- never
        ambiguous with failure, which must instead raise a
        :class:`~app.data.models.HistoricalProviderError` subclass.
        """
