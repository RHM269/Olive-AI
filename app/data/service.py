"""Olive's historical-data orchestration service (Phase 3).

``HistoricalDataService`` is the one place that coordinates every
safety gate a historical fetch must pass, in order, before any paid
provider call can happen -- and the one place that turns every
possible outcome into an explicit, structured
:class:`~app.data.models.HistoricalFetchResult` rather than letting a
caller guess at success from ``None``/an empty list/an exception.

Responsibilities deliberately kept OUT of
``app.data.providers.databento`` (the adapter stays a thin, dumb
translator of one vendor's API):

1. Olive production tradable-domain gate (``app.futures.validation`` +
   ``app.data.validation.require_olive_tradable_contract``) -- a
   non-Olive root, or a corrupted Olive futures domain, is rejected
   before any provider interaction.
2. Request type/shape validation (already enforced by
   :class:`~app.data.models.HistoricalBarRequest`'s own construction,
   re-checked here defensively for a caller-supplied value of unknown
   type).
3. Provider configuration / network-opt-in / cost-limit safety gates
   (see docs/historical_data.md "Network opt-in" / "Cost safety").
4. Cost estimation and comparison against
   ``Settings.historical_max_request_cost_usd``.
5. The actual provider fetch.
6. Cross-bar/cross-request validation and normalization
   (``app.data.validation.normalize_and_validate_bars``).
7. Optional durable storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.config import Settings
from app.futures.calendar import CycleDateCalendar, load_cycle_date_calendar
from app.futures.models import FuturesDomainError
from app.futures.registry import FuturesInstrumentRegistry, load_instrument_registry
from app.futures.validation import validate_olive_futures_domain

from app.data.models import (
    HistoricalBarRequest,
    HistoricalDataError,
    HistoricalFetchResult,
    HistoricalFetchStatus,
    HistoricalProviderError,
    CostEstimationFailedError,
    InvalidHistoricalServiceConfigurationError,
    _require_historical_bar_request,
)
from app.data.provider_base import CostEstimate, HistoricalMarketDataProvider
from app.data.providers.unconfigured import UnconfiguredHistoricalProvider
from app.data.storage import HistoricalBarStore, HistoricalWriteResult
from app.data.validation import normalize_and_validate_bars, require_olive_tradable_contract


class HistoricalDataService:
    """Olive's historical-data orchestration service.

    ``provider``, ``store``, ``registry``, and ``calendar`` are all
    injectable so tests never need a real Databento client, a real
    pyarrow-backed store, or even the real on-disk Phase 2
    configuration files -- see ``tests/test_historical_data_service.py``.
    When not supplied, ``registry``/``calendar`` are loaded from
    Olive's real Phase 2 configuration (a local, version-controlled
    JSON read -- never network I/O), and ``store`` defaults to a
    :class:`~app.data.storage.HistoricalBarStore` rooted at
    ``settings.historical_data_dir``.
    """

    def __init__(
        self,
        provider: HistoricalMarketDataProvider,
        settings: Settings,
        *,
        store: Optional[HistoricalBarStore] = None,
        registry: Optional[FuturesInstrumentRegistry] = None,
        calendar: Optional[CycleDateCalendar] = None,
    ) -> None:
        # Phase 3.1 §17: every constructor-time argument is validated
        # with an Olive-owned error at CONSTRUCTION time -- never a raw
        # TypeError, and never deferred until some later call happens
        # to trip over an AttributeError on a malformed injected
        # dependency.
        if not isinstance(provider, HistoricalMarketDataProvider):
            raise InvalidHistoricalServiceConfigurationError(
                f"HistoricalDataService requires a HistoricalMarketDataProvider, got "
                f"{type(provider).__name__}"
            )
        if not isinstance(settings, Settings):
            raise InvalidHistoricalServiceConfigurationError(
                f"HistoricalDataService requires a Settings instance, got {type(settings).__name__}"
            )
        # `store` is deliberately checked by DUCK-TYPED interface
        # (a callable `write_bars`), not by strict isinstance against
        # the concrete HistoricalBarStore -- this class's whole design
        # point (see its docstring) is that tests inject a store test
        # double without ever needing the real pyarrow-backed
        # implementation. A strict isinstance check here would defeat
        # that injectability for no safety benefit; it would still let
        # a blatantly wrong value (None handled above, a string, an
        # int, a bool, ...) through only if that value happened to
        # also expose a callable `write_bars`, which none of those do.
        if store is not None and not (hasattr(store, "write_bars") and callable(store.write_bars)):
            raise InvalidHistoricalServiceConfigurationError(
                f"HistoricalDataService's store, when supplied, must provide a callable "
                f"write_bars(...) method (a HistoricalBarStore, or a test double "
                f"implementing the same interface); got {type(store).__name__}"
            )
        if registry is not None and not isinstance(registry, FuturesInstrumentRegistry):
            raise InvalidHistoricalServiceConfigurationError(
                f"HistoricalDataService's registry, when supplied, must be a "
                f"FuturesInstrumentRegistry, got {type(registry).__name__}"
            )
        if calendar is not None and not isinstance(calendar, CycleDateCalendar):
            raise InvalidHistoricalServiceConfigurationError(
                f"HistoricalDataService's calendar, when supplied, must be a CycleDateCalendar, "
                f"got {type(calendar).__name__}"
            )

        self._provider = provider
        self._settings = settings
        self._store = store if store is not None else HistoricalBarStore(settings.historical_data_dir)
        self._registry = registry if registry is not None else load_instrument_registry()
        self._calendar = calendar if calendar is not None else load_cycle_date_calendar()

    def fetch_and_store(self, request: object) -> HistoricalFetchResult:
        """Run ``request`` through every safety gate, fetch, validate,
        and durably store its bars, returning a structured result.

        Never raises for an expected failure mode (a bad request, an
        untradable contract, network disabled, over cost limit, a
        provider failure, a validation/storage failure) -- every one
        of those becomes a distinct :class:`HistoricalFetchStatus`.
        Only a genuine programming bug (not a documented
        :class:`~app.data.models.HistoricalDataError`/
        :class:`~app.futures.models.FuturesDomainError`) propagates as
        an unhandled exception, so bugs are never silently absorbed
        into a misleading result.
        """
        try:
            request = _require_historical_bar_request(request, context="HistoricalDataService.fetch_and_store")
        except HistoricalDataError as exc:
            return HistoricalFetchResult(status=HistoricalFetchStatus.REJECTED_INVALID_REQUEST, message=str(exc))

        try:
            validate_olive_futures_domain(self._registry, self._calendar)
        except FuturesDomainError as exc:
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.FAILED,
                message=f"Olive's futures domain is not production-valid: {exc}",
            )

        try:
            instrument = require_olive_tradable_contract(request.contract, self._registry)
        except HistoricalDataError as exc:
            return HistoricalFetchResult(status=HistoricalFetchStatus.REJECTED_NOT_TRADABLE, message=str(exc))

        if isinstance(self._provider, UnconfiguredHistoricalProvider):
            return HistoricalFetchResult(status=HistoricalFetchStatus.NOT_CONFIGURED, message=self._provider.reason)

        if not self._settings.historical_network_enabled:
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.REJECTED_NETWORK_DISABLED,
                message="OLIVE_HISTORICAL_NETWORK_ENABLED is false; refusing to contact a provider.",
            )

        try:
            cost_estimate = self._provider.estimate_cost(request)
        except HistoricalProviderError as exc:
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.REJECTED_COST_ESTIMATE_FAILED,
                message=f"Cost estimation failed; refusing to fetch (fail closed): {exc}",
            )

        # Phase 3.1 §7: the provider is untrusted even though it
        # implements the ABC -- a broken/malicious provider returning
        # None, a dict, or any non-CostEstimate from estimate_cost()
        # must never be used as if it were a real CostEstimate (which
        # would raise a raw AttributeError on the next line, or worse,
        # silently misbehave if the object happens to duck-type far
        # enough). Fail closed exactly like a genuine provider error.
        if not isinstance(cost_estimate, CostEstimate):
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.REJECTED_COST_ESTIMATE_FAILED,
                message=(
                    "Cost estimation failed; refusing to fetch (fail closed): provider "
                    f"estimate_cost() returned {type(cost_estimate).__name__}, not a CostEstimate."
                ),
            )

        if cost_estimate.estimated_cost_usd > self._settings.historical_max_request_cost_usd:
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.REJECTED_COST_LIMIT,
                message=(
                    f"Estimated cost {cost_estimate.estimated_cost_usd} USD exceeds configured "
                    f"maximum {self._settings.historical_max_request_cost_usd} USD."
                ),
                estimated_cost_usd=cost_estimate.estimated_cost_usd,
            )

        try:
            raw_bars = self._provider.fetch_bars(request, instrument)
        except HistoricalProviderError as exc:
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.FAILED,
                message=f"Provider fetch failed: {exc}",
                estimated_cost_usd=cost_estimate.estimated_cost_usd,
            )

        # Phase 3.1 §16: a broken provider returning None/{}/"" (any
        # falsy non-sequence) must NEVER be mistaken for the
        # documented "no records for this window" empty-success case
        # (an empty tuple/list is the only legitimate empty
        # representation -- see HistoricalMarketDataProvider.fetch_bars).
        # A malformed return type is a provider FAILURE, not a success.
        if not isinstance(raw_bars, (tuple, list)):
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.FAILED,
                message=(
                    f"Provider fetch failed: fetch_bars() returned {type(raw_bars).__name__}, "
                    "not a tuple/list of HistoricalBar (fail closed rather than mistake this for "
                    "an empty-success result)."
                ),
                estimated_cost_usd=cost_estimate.estimated_cost_usd,
            )

        if not raw_bars:
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.SUCCESS_EMPTY,
                bars=(),
                estimated_cost_usd=cost_estimate.estimated_cost_usd,
                new_records_stored=0,
            )

        try:
            # Phase 3.2 §10-12: pass the already production-validated
            # instrument and this provider's own name through, so bar
            # economics (tick_size) and provenance (provider/
            # provider_raw_symbol) are cross-checked against Olive's
            # production domain and the provider actually servicing
            # this request -- never trusted merely because each bar is
            # individually self-consistent.
            normalized_bars = normalize_and_validate_bars(
                raw_bars, request=request, instrument=instrument, expected_provider_name=self._provider.name
            )
        except HistoricalDataError as exc:
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.FAILED,
                message=f"Bar validation failed: {exc}",
                estimated_cost_usd=cost_estimate.estimated_cost_usd,
            )

        try:
            write_result = self._store.write_bars(
                normalized_bars,
                requested_start=request.start,
                requested_end=request.end,
                estimated_cost_usd=cost_estimate.estimated_cost_usd,
                contract_multiplier=instrument.multiplier,
            )
        except (HistoricalDataError, OSError) as exc:
            # Phase 3.2 §14: an expected storage I/O failure (a raw
            # OSError from an injected duck-typed store, or from a
            # real HistoricalBarStore path not yet wrapped in its own
            # HistoricalStorageError translation) must become a
            # structured FAILED result too -- never bypass Olive's
            # structured result contract. This is narrow: any OTHER
            # exception type (a TypeError from calling the store with
            # the wrong signature, say) is a genuine programming bug
            # and is deliberately left to propagate unmodified.
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.FAILED,
                message=f"Storage failed: {exc}",
                bars=normalized_bars,
                estimated_cost_usd=cost_estimate.estimated_cost_usd,
            )

        # Phase 3.2 §13: `store` is deliberately duck-typed (see this
        # class's constructor), so its OUTPUT is just as untrusted as
        # any other injected dependency's output -- a malformed return
        # from write_bars() (None, a dict, the wrong Olive result
        # type, ...) must never be touched as if it were a real
        # HistoricalWriteResult (which would otherwise leak a raw
        # AttributeError the moment `.new_records` is accessed below).
        if not isinstance(write_result, HistoricalWriteResult):
            return HistoricalFetchResult(
                status=HistoricalFetchStatus.FAILED,
                message=(
                    f"Storage failed: write_bars() returned {type(write_result).__name__}, "
                    "not a HistoricalWriteResult (fail closed rather than trust an untrusted "
                    "duck-typed store's return value)."
                ),
                bars=normalized_bars,
                estimated_cost_usd=cost_estimate.estimated_cost_usd,
            )

        return HistoricalFetchResult(
            status=HistoricalFetchStatus.SUCCESS,
            bars=normalized_bars,
            estimated_cost_usd=cost_estimate.estimated_cost_usd,
            new_records_stored=write_result.new_records,
        )
