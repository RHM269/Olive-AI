"""Olive production-tradability and cross-bar validation for historical
market data (Phase 3).

Two deliberately separate layers, continuing the Phase 2 "generic
structural validity vs. Olive production correctness" distinction
(see CLAUDE.md and ``docs/futures_domain.md`` §9):

- :func:`require_olive_tradable_contract` answers "is this contract
  one of Olive's required PRODUCTION NQ/MNQ instruments?" -- by
  delegating entirely to the *existing* Phase 2 production-validation
  layer (``app.futures.validation``). No canonical NQ/MNQ fact
  (multiplier, tick size, required roots, ...) is duplicated here;
  this module only imports and reuses what Phase 2 already validates.
- :func:`normalize_and_validate_bars` answers a different question:
  "given a batch of already-structurally-valid
  :class:`~app.data.models.HistoricalBar` objects (each individually
  self-consistent per its own ``__post_init__``), are they consistent
  WITH EACH OTHER and with the request that produced them?" -- correct
  contract/timeframe identity, timestamps inside the requested
  half-open window, deterministic ordering, and duplicate/conflict
  detection.

Neither function re-implements economics or structural bar validation
``app.data.models.HistoricalBar`` already performs.
"""

from __future__ import annotations

from typing import Sequence

from app.futures.models import FuturesContract, FuturesDomainError, UnknownInstrumentError, _require_contract
from app.futures.registry import FuturesInstrumentRegistry, _require_registry
from app.futures.validation import (
    REQUIRED_TRADABLE_ROOTS,
    _CANONICAL_SPECS,
    _validate_instrument_matches_spec,
)
from app.futures.models import FuturesInstrument

from app.data.models import (
    HistoricalBar,
    HistoricalBarContractMismatchError,
    HistoricalBarConflictError,
    HistoricalBarOutOfRangeError,
    HistoricalBarProductionEconomicsMismatchError,
    HistoricalBarProviderProvenanceMismatchError,
    HistoricalBarRequest,
    InvalidHistoricalBarError,
    NotOliveTradableContractError,
    _require_historical_bar_request,
)
from typing import Optional


def require_olive_tradable_contract(
    contract: FuturesContract, registry: FuturesInstrumentRegistry
) -> FuturesInstrument:
    """Require that ``contract``'s root is one of Olive's required
    PRODUCTION tradable instruments, matching Olive's canonical Phase
    2 financial specification exactly -- not merely that ``contract``
    is a structurally valid :class:`FuturesContract`.

    This is the historical-data-layer equivalent of the cross-instrument
    protections Phase 2.1 built for contract navigation: a caller
    submitting a structurally valid contract for an unrelated root
    (``ES``, ``CL``, ...) -- or even a *wrong* NQ/MNQ definition, if a
    caller somehow supplied a corrupted registry -- is rejected here,
    before any provider cost estimate or fetch call, every time.

    Delegates entirely to ``app.futures.validation``'s existing
    canonical specs (``_CANONICAL_SPECS``) and comparison logic
    (``_validate_instrument_matches_spec``) -- this function duplicates
    no NQ/MNQ fact of its own. Returns the matching, Olive-production-
    validated :class:`FuturesInstrument` (which callers need next, to
    get ``tick_size`` for bar construction) on success.

    Raises :class:`NotOliveTradableContractError` (never a raw
    ``KeyError``/``AttributeError``, and never the underlying
    ``app.futures.validation.ProductionDomainIntegrityError`` or
    ``app.futures.models.UnknownInstrumentError`` directly -- both are
    translated/chained into this package's own error type) if the
    contract's root is not tradable, or if the registered instrument
    for that root does not match Olive's canonical specification.
    """
    # Translate futures-domain validation failures (a different error
    # hierarchy root) into this package's own HistoricalDataError
    # hierarchy -- never let a raw FuturesDomainError leak through this
    # boundary; a caller of this function only expects to catch
    # HistoricalDataError (specifically NotOliveTradableContractError).
    try:
        contract = _require_contract(contract, context="require_olive_tradable_contract")
    except FuturesDomainError as exc:
        raise NotOliveTradableContractError(f"require_olive_tradable_contract: invalid contract: {exc}") from exc
    try:
        registry = _require_registry(registry, context="require_olive_tradable_contract")
    except FuturesDomainError as exc:
        raise NotOliveTradableContractError(f"require_olive_tradable_contract: invalid registry: {exc}") from exc

    root_symbol = contract.root_symbol
    if root_symbol not in REQUIRED_TRADABLE_ROOTS:
        raise NotOliveTradableContractError(
            f"{root_symbol} is not part of Olive's PRODUCTION tradable universe "
            f"{sorted(REQUIRED_TRADABLE_ROOTS)}; historical data may only be requested for "
            f"NQ/MNQ contracts."
        )

    try:
        instrument = registry.get(root_symbol)
    except UnknownInstrumentError as exc:
        raise NotOliveTradableContractError(
            f"{root_symbol} is in Olive's required tradable roots but is not present in the "
            f"loaded instrument registry -- the futures domain is misconfigured."
        ) from exc

    spec = next((candidate for candidate in _CANONICAL_SPECS if candidate.root_symbol == root_symbol), None)
    if spec is None:  # pragma: no cover - defensive; unreachable given the membership check above
        raise NotOliveTradableContractError(
            f"{root_symbol} has no canonical Olive production specification."
        )

    try:
        _validate_instrument_matches_spec(instrument, spec)
    except FuturesDomainError as exc:
        raise NotOliveTradableContractError(
            f"The registered {root_symbol} instrument does not match Olive's canonical "
            f"production specification: {exc}"
        ) from exc

    return instrument


def normalize_and_validate_bars(
    bars: Sequence[HistoricalBar],
    *,
    request: HistoricalBarRequest,
    instrument: Optional[FuturesInstrument] = None,
    expected_provider_name: Optional[str] = None,
) -> tuple[HistoricalBar, ...]:
    """Validate a batch of provider-normalized bars against each other
    and against the request that produced them, returning a
    deterministically sorted, de-duplicated tuple.

    Each ``bar`` in ``bars`` is assumed to already be a structurally
    valid :class:`HistoricalBar` (its own ``__post_init__`` already
    guarantees that); this function checks what an individual bar
    cannot check about itself:

    - every bar's contract identity (root/year/month) and timeframe
      match ``request`` exactly -- never a different contract/
      timeframe silently accepted (:class:`HistoricalBarContractMismatchError`).
    - every bar's ``ts_event`` falls within ``request``'s half-open
      ``[start, end)`` interval -- a provider returning data outside
      the requested window is rejected, never silently stored
      (:class:`HistoricalBarOutOfRangeError`).
    - bars are returned sorted by ``ts_event`` ascending -- explicit,
      deterministic normalization of provider rows that may arrive
      out of order (never assumed to already be sorted).
    - bars sharing the same canonical key (contract + timeframe +
      ``ts_event``) with IDENTICAL content are de-duplicated to one
      record; bars sharing a key with CONFLICTING content raise
      :class:`HistoricalBarConflictError` -- Olive never silently
      picks a winner.
    - (Phase 3.2 §10-12) when ``instrument`` is supplied -- Olive's
      already PRODUCTION-validated :class:`FuturesInstrument` for this
      request's contract root, from
      ``require_olive_tradable_contract`` -- every bar's ``tick_size``
      must match it exactly
      (:class:`HistoricalBarProductionEconomicsMismatchError`). A bar
      can be perfectly self-consistent on its own while still using
      the wrong tick size for the actual NQ/MNQ instrument Olive
      trades; this is the historical-data instance of the Phase 2
      "generic structural validity vs. Olive production correctness"
      distinction (see CLAUDE.md). Every bar's ``provider_raw_symbol``
      must always equal ``request.contract.display_code`` (the exact
      raw symbol Olive derived for this request), and, when
      ``expected_provider_name`` is supplied, every bar's ``provider``
      must equal it -- both defense in depth against a buggy or
      malicious provider adapter attributing bars to the wrong
      provider/symbol (:class:`HistoricalBarProviderProvenanceMismatchError`).

    Does not itself touch storage or a provider -- see
    ``app.data.service.HistoricalDataService`` for how these compose,
    and ``require_olive_tradable_contract`` for Olive's separate
    production tradability gate.
    """
    request = _require_historical_bar_request(request, context="normalize_and_validate_bars")
    if instrument is not None and not isinstance(instrument, FuturesInstrument):
        raise InvalidHistoricalBarError(
            f"normalize_and_validate_bars expects instrument to be a FuturesInstrument or "
            f"None, got {type(instrument).__name__}"
        )
    if expected_provider_name is not None and not isinstance(expected_provider_name, str):
        raise InvalidHistoricalBarError(
            f"normalize_and_validate_bars expects expected_provider_name to be a str or "
            f"None, got {type(expected_provider_name).__name__}"
        )
    if not isinstance(bars, (list, tuple)):
        # Phase 3.1 §18 fix: `tuple(bars)` on a non-iterable caller
        # value (None, an int, ...) raises a raw TypeError -- never
        # let that escape this public boundary; fail closed with
        # Olive's own error instead.
        try:
            bars = tuple(bars)
        except TypeError as exc:
            raise InvalidHistoricalBarError(
                f"normalize_and_validate_bars expects an iterable of HistoricalBar, got "
                f"{type(bars).__name__} ({bars!r}), which is not iterable."
            ) from exc
    for bar in bars:
        if not isinstance(bar, HistoricalBar):
            raise HistoricalBarContractMismatchError(
                f"normalize_and_validate_bars expects every element to be a HistoricalBar, got "
                f"{type(bar).__name__}"
            )

    expected_root = request.contract.root_symbol
    expected_year = request.contract.year
    expected_month = int(request.contract.month)
    expected_raw_symbol = request.contract.display_code

    for bar in bars:
        if (
            bar.root_symbol != expected_root
            or bar.contract_year != expected_year
            or bar.contract_month != expected_month
        ):
            raise HistoricalBarContractMismatchError(
                f"Bar identity {bar.contract_identity} does not match requested contract "
                f"{request.contract.identity} -- refusing to store data under the wrong "
                f"contract identity."
            )
        if bar.timeframe is not request.timeframe:
            raise HistoricalBarContractMismatchError(
                f"Bar timeframe {bar.timeframe!r} does not match requested timeframe "
                f"{request.timeframe!r}."
            )
        if not (request.start <= bar.ts_event < request.end):
            raise HistoricalBarOutOfRangeError(
                f"Bar ts_event {bar.ts_event.isoformat()} is outside the requested half-open "
                f"interval [{request.start.isoformat()}, {request.end.isoformat()})."
            )

        # Phase 3.2 §10: generic structural validity vs. Olive
        # production correctness -- a bar can be internally
        # self-consistent while still using the wrong tick size for
        # Olive's actual production instrument. Never trusted on
        # self-consistency alone.
        if instrument is not None and bar.tick_size != instrument.tick_size:
            raise HistoricalBarProductionEconomicsMismatchError(
                f"Bar {bar.contract_identity} reports tick_size={bar.tick_size}, but Olive's "
                f"production-validated instrument for {expected_root} requires "
                f"tick_size={instrument.tick_size}."
            )

        # Phase 3.2 §11: defense in depth against a buggy/malicious
        # provider adapter -- a bar's own provenance must agree with
        # the provider actually servicing this request and the exact
        # raw symbol Olive derived for it, even though the provider
        # implements the HistoricalMarketDataProvider ABC.
        if expected_provider_name is not None and bar.provider != expected_provider_name:
            raise HistoricalBarProviderProvenanceMismatchError(
                f"Bar {bar.contract_identity} reports provider={bar.provider!r}, but this "
                f"request is being serviced by provider {expected_provider_name!r}. Refusing "
                f"to store a bar whose own provenance does not match the provider that "
                f"actually produced it."
            )
        if bar.provider_raw_symbol != expected_raw_symbol:
            raise HistoricalBarProviderProvenanceMismatchError(
                f"Bar {bar.contract_identity} reports "
                f"provider_raw_symbol={bar.provider_raw_symbol!r}, but Olive derived raw "
                f"symbol {expected_raw_symbol!r} for this request's contract. Refusing to "
                f"store a bar whose own raw-symbol provenance does not match what Olive "
                f"actually requested."
            )

    ordered = sorted(bars, key=lambda bar: bar.ts_event)

    deduplicated: list[HistoricalBar] = []
    seen_by_key: dict[tuple, HistoricalBar] = {}
    for bar in ordered:
        key = bar.canonical_key
        existing = seen_by_key.get(key)
        if existing is None:
            seen_by_key[key] = bar
            deduplicated.append(bar)
            continue
        if existing.conflicts_with(bar):
            raise HistoricalBarConflictError(
                f"Conflicting bars for canonical key {key}: "
                f"existing OHLCV=({existing.open}, {existing.high}, {existing.low}, "
                f"{existing.close}, vol={existing.volume}) vs. new OHLCV=({bar.open}, "
                f"{bar.high}, {bar.low}, {bar.close}, vol={bar.volume}). Refusing to silently "
                f"choose one."
            )
        # Identical duplicate -- safe to de-duplicate, keep the first.

    return tuple(deduplicated)
