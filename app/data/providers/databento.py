"""Olive's Databento historical-data provider adapter (Phase 3, hardened
in Phase 3.1).

All Databento-specific knowledge lives here -- the rest of Olive never
needs to know about ``DBNStore``, Databento's schema-name strings, its
exception hierarchy, its symbology-resolution response shape, or its
DataFrame column quirks. Everything is normalized at this boundary into
Olive's own domain objects (:class:`~app.data.provider_base.CostEstimate`,
:class:`~app.data.models.HistoricalBar`) before returning.

Exact contract/symbology policy (see docs/historical_data.md for the
full rationale):

- Olive always requests an EXACT contract using ``stype_in="raw_symbol"``
  and the contract's own CME-style display code (e.g. ``"NQZ6"``,
  via :attr:`FuturesContract.display_code` -- Phase 2's existing,
  already-reviewed symbol derivation). Olive never accepts an
  arbitrary raw-symbol string from a caller; it is always derived
  internally from Olive's own :class:`FuturesContract`.
- Olive never uses Databento's PARENT symbology (``"NQ.FUT"``, which
  mixes outright futures and futures spreads) or CONTINUOUS symbology
  (``"NQ.c.0"``/``"NQ.v.0"``/``"NQ.n.0"``, which encodes Databento's
  own roll rules, not Olive's Phase 2 roll semantics) for canonical
  contract-level historical storage.
- Databento instrument IDs are recorded as optional provenance only
  (``HistoricalBar.provider_instrument_id``) -- never Olive's
  persistent contract identity, which remains
  ``FuturesContract.identity`` (e.g. ``"NQ-2026-12"``).
- **Phase 3.1 addition**: Databento raw symbols are DOCUMENTED to be
  reused across years (``NQZ6`` identifies a December contract in
  2016, 2026, AND 2036 -- the single trailing digit is only the last
  digit of the year). Checking that a response row's own ``symbol``
  field equals the requested raw symbol is therefore NOT sufficient to
  guarantee the response actually belongs to the full-year contract
  Olive asked for. Before any paid ``timeseries.get_range`` call, this
  adapter uses Databento's FREE ``symbology.resolve`` endpoint to
  resolve the raw symbol to its instrument ID both (a) within the
  requested contract's own month and (b) over the request's actual
  date range, and requires both to agree -- see
  :meth:`DatabentoHistoricalProvider._resolve_instrument_id` and the
  call in :meth:`fetch_bars`. A decade/mapping mismatch, or a
  resolution that is missing/ambiguous/partial, fails closed BEFORE
  the paid fetch. Every returned row's own ``instrument_id`` is then
  cross-checked against that resolved value as well, in addition to
  the existing ``symbol`` check.

Third-party import safety: the ``databento`` package is imported
lazily, only when actually constructing a live client (never at module
import time, never merely by selecting ``OLIVE_HISTORICAL_PROVIDER=databento``
in configuration) -- see ``app.data.providers.factory``. A ``client``
may also be injected directly (what every test in
``tests/test_historical_data_databento.py`` does), so this module and
its tests never require the real ``databento`` package to be
installed.

**Secret-safety policy (Phase 3.1, critical)**: this module NEVER
includes a Databento vendor exception's own message text in any error
it raises, in any log line, or anywhere else. Independent review
demonstrated that Databento's documented invalid-auth error text can
itself contain the configured API key (e.g. an invalid-Basic-auth
error of the shape ``"Invalid username in Basic auth ('<key>')"``).
Vendor exception text is inspected ONLY internally, to classify the
failure into a category, and every error this module raises carries a
fixed, generic, pre-written message for that category -- never an
f-string built from the vendor exception's own ``str(exc)``. Every
translated error is also raised with ``from None``, deliberately
severing the exception chain, so that even a full traceback print can
never surface the original vendor exception object or its text.

Databento version/API drift note: this adapter is written against the
documented ``databento-python`` client surface as of this phase
(``Historical(key=...)``; ``metadata.get_cost(...)``;
``symbology.resolve(...)``; ``timeseries.get_range(..., stype_in="raw_symbol")``;
``DBNStore.to_df(price_type="decimal", tz="UTC", map_symbols=True)``).
It could not be exercised against the real installed package during
this build (see docs/historical_data.md "Known limitations") --
verification here uses an injected fake client that mimics the
documented interface, per CLAUDE.md's "no network during completion
verification" policy. If the real client's signature drifts in a
future version, only this file should need to change.
"""

from __future__ import annotations

import operator
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Any, Optional

from app.futures.models import FuturesInstrument
from app.data.models import (
    CostEstimationFailedError,
    HistoricalBar,
    HistoricalBarContractMismatchError,
    HistoricalBarRequest,
    HistoricalDataError,
    HistoricalProviderError,
    InvalidHistoricalRequestError,
    ProviderAuthenticationError,
    ProviderDataError,
    ProviderNotConfiguredError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderResponseIdentityError,
    ProviderSymbologyError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    _require_historical_bar_request,
)
from app.data.provider_base import CostEstimate, HistoricalMarketDataProvider

DEFAULT_DATASET = "GLBX.MDP3"


def _is_databento_exception(exc: BaseException) -> bool:
    """Whether ``exc`` originates from the ``databento`` package (its
    real exception hierarchy, or a test double deliberately declaring
    ``__module__`` to match it) -- used to translate *vendor* failures
    without ever broadly swallowing an unrelated programming error
    (a ``TypeError`` from Olive's own code calling the client wrong is
    never mistaken for a "provider failure")."""
    module = type(exc).__module__
    return isinstance(module, str) and module.startswith("databento")


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "http_status", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _translate_databento_exception(exc: BaseException) -> HistoricalProviderError:
    """Translate a Databento vendor exception into one of this
    package's own :class:`HistoricalProviderError` subclasses, carrying
    only a fixed, generic, pre-written message for the matched
    category.

    The vendor exception's status code and message text are inspected
    HERE ONLY, to decide which category applies -- neither is ever
    echoed into the returned error. Falls back to
    :class:`ProviderUnavailableError` -- itself a safe, fail-closed
    outcome -- whenever the specific failure kind cannot be determined.
    """
    message = str(exc)
    lowered = message.lower()
    status_code = _status_code(exc)

    is_auth_failure = (
        status_code == 401
        or "unauthoriz" in lowered
        or "authentic" in lowered
        or "invalid api key" in lowered
        or "invalid username" in lowered  # Databento's documented invalid-Basic-auth shape
        or "basic auth" in lowered
        or "bad api key" in lowered
    )
    if is_auth_failure:
        return ProviderAuthenticationError("Databento authentication failed (invalid or rejected API key).")

    is_permission_failure = (
        status_code == 403
        or "forbidden" in lowered
        or "permission" in lowered
        or "subscription" in lowered
        or "entitle" in lowered
    )
    if is_permission_failure:
        return ProviderPermissionError(
            "Databento permission/subscription error (the API key is valid but not entitled "
            "to this dataset/schema/symbol)."
        )

    is_rate_limit = status_code == 429 or "rate limit" in lowered or "too many requests" in lowered
    if is_rate_limit:
        return ProviderRateLimitError("Databento rate limit exceeded.")

    if status_code is not None and 500 <= status_code < 600:
        return ProviderUnavailableError(f"Databento server error (status {status_code}).")

    if "timed out" in lowered or "timeout" in lowered:
        return ProviderTimeoutError("Databento request timed out.")

    if status_code is not None:
        return ProviderUnavailableError(f"Databento provider error (status {status_code}).")
    return ProviderUnavailableError("Databento provider error.")


def _safe_volume(raw_volume: object) -> int:
    """Convert a provider-supplied volume value to a true, non-negative
    ``int`` without ever silently truncating fractional data.

    Phase 3.1 fix: the previous implementation used ``int(row.volume)``,
    which silently truncates ``1.5`` to ``1`` -- undetectable data
    corruption. ``operator.index()`` accepts any type that implements
    ``__index__`` (real ``int``, and numpy integer scalar types, which
    a real Databento DataFrame may use) and rejects everything that
    does not, including every ``float`` (``1.5`` AND ``1.0`` -- Olive
    does not intentionally support a float volume column), ``NaN``/
    ``Infinity`` (also floats), and numeric strings. ``bool`` is
    rejected explicitly first, since ``bool.__index__`` would otherwise
    silently accept it as ``0``/``1``.
    """
    if isinstance(raw_volume, bool):
        raise ProviderDataError(f"Databento row volume must not be a bool; got {raw_volume!r}")
    try:
        value = operator.index(raw_volume)
    except TypeError as exc:
        raise ProviderDataError(
            f"Databento row volume must be a true integer-like value (never a float, "
            f"fractional, or non-numeric string); got {raw_volume!r} ({type(raw_volume).__name__})"
        ) from None
    if value < 0:
        raise ProviderDataError(f"Databento row volume must not be negative; got {value}")
    return value


def _resolution_mapping(resolution: object) -> dict:
    """Normalize a ``symbology.resolve()`` response (a ``dict``, or an
    attribute-style object exposing ``.result``/``.not_found``/
    ``.partial``) into a plain ``dict`` with those three keys.

    Phase 3.3 QA compliance rework (item 7): this previously used
    ``getattr(resolution, "not_found", []) or []`` -- but Python's ``or``
    only replaces a FALSY value, so a wrongly-typed but TRUTHY attribute
    (e.g. ``not_found=123``) passed straight through unchanged instead of
    being coerced to ``[]``. That malformed value then reached a later
    ``raw_symbol in not_found`` check and leaked a raw ``TypeError``
    (``argument of type 'int' is not iterable``) instead of the documented
    :class:`ProviderSymbologyError`. This function no longer attempts any
    coercion -- it only extracts whatever value is present (or ``None`` if
    absent); every value is validated for real type/shape by
    :func:`_require_symbology_container`/:func:`_require_symbology_result_mapping`
    in :func:`_distinct_resolved_instrument_id`, immediately before it is
    used, which is the only place malformed-vs-absent can be told apart
    correctly."""
    if isinstance(resolution, dict):
        return resolution
    # Defensive fallback for an attribute-style response object.
    return {
        "result": getattr(resolution, "result", None),
        "not_found": getattr(resolution, "not_found", None),
        "partial": getattr(resolution, "partial", None),
    }


def _is_valid_databento_instrument_id_string(value: object) -> bool:
    """Phase 3.3 §11: whether ``value`` is a well-formed Databento
    symbology instrument-ID string -- an actual ``str`` (never an
    ``int``/``float``/``bool``/other type, even though the underlying
    identifier IS documented as an unsigned integer -- Databento's
    symbology response represents the ``"s"`` mapping value as a
    STRING), non-empty after stripping, composed only of ASCII decimal
    digits, and strictly positive (``"0"`` is not a valid instrument
    ID). Rejects ``None``, ``""``, ``"   "``, ``0``, ``True``,
    ``False``, any ``float``, any non-digit text, and any other type
    -- every one of these previously passed straight through via a
    blind ``str(instrument_id)`` conversion, meaning a malformed
    resolution could reach the paid ``timeseries.get_range`` call
    before failing (only when a MISMATCHED malformed value was later
    compared against a row's own reported ``instrument_id``)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    if not (stripped.isascii() and stripped.isdigit()):
        return False
    return int(stripped) > 0


def _require_symbology_container(value: object, *, field_name: str, raw_symbol: str) -> list:
    """Phase 3.3 QA compliance rework (item 8) + Phase 3 final completion
    pass (item C): validate the ``not_found``/``partial`` container's own
    shape, AND every element inside it, BEFORE any ``in``/iteration
    touches it. Absent (``None``) is a legitimate "nothing in this
    category" case and normalizes to ``[]``; a ``list``/``tuple`` is
    accepted only when EVERY element is a non-empty symbol string (never
    silently tolerating a malformed member just because the container
    itself is the right type); any other container type (an ``int``, a
    bare string -- which would otherwise support ``in`` only as a
    *substring* check, not a membership check over symbols -- a ``dict``,
    etc.) is a malformed response and raises
    :class:`ProviderSymbologyError`, never a raw ``TypeError``.

    This closes a gap the container-type check alone left open:
    ``not_found=[123]``/``not_found=[{"symbol": "NQZ6"}]`` previously
    passed this function (a list IS a list), and the subsequent
    ``raw_symbol in not_found`` membership test simply never matched
    (``"NQZ6" != 123``) -- silently treating a structurally malformed
    response as if ``not_found`` had legitimately not contained
    ``raw_symbol``, rather than rejecting the response outright for being
    untrustworthy as a whole. Testing only the container's TYPE never
    proves its MEMBERS are safe."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        result = list(value)
        for element in result:
            if not isinstance(element, str) or not element.strip():
                raise ProviderSymbologyError(
                    f"Databento symbology resolution for raw symbol {raw_symbol!r} has a "
                    f"malformed element in its {field_name!r} field (expected every element "
                    f"to be a non-empty symbol string; got {element!r} of type "
                    f"{type(element).__name__}); refusing to fetch without confirmed contract "
                    f"identity."
                )
        return result
    raise ProviderSymbologyError(
        f"Databento symbology resolution for raw symbol {raw_symbol!r} has a malformed "
        f"{field_name!r} field (expected a list/tuple of symbols, or absent; got {value!r} "
        f"of type {type(value).__name__}); refusing to fetch without confirmed contract "
        f"identity."
    )


def _require_symbology_result_mapping(value: object, *, raw_symbol: str) -> dict:
    """Phase 3.3 QA compliance rework (item 8): validate the top-level
    ``result`` field's own shape BEFORE indexing into it. Absent (``None``)
    normalizes to ``{}`` (no entries for any symbol); a ``dict`` is
    accepted as-is; any other type raises :class:`ProviderSymbologyError`."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise ProviderSymbologyError(
        f"Databento symbology resolution for raw symbol {raw_symbol!r} has a malformed "
        f"'result' field (expected a mapping of symbol to entries; got {value!r} of type "
        f"{type(value).__name__}); refusing to fetch without confirmed contract identity."
    )


def _require_symbology_entries_sequence(value: object, *, raw_symbol: str) -> list:
    """Phase 3.3 QA compliance rework (item 7/8): validate
    ``result[raw_symbol]`` itself BEFORE iterating over it.

    This is the direct fix for the reported defect: ``{"result":
    {"NQZ6": 123}, "not_found": [], "partial": []}`` previously reached
    ``for entry in entries:`` with ``entries=123`` and leaked a raw
    ``TypeError: 'int' object is not iterable``. A ``str``/``bytes`` is
    rejected explicitly too, even though both ARE iterable in Python --
    iterating one yields individual characters/bytes, not mapping
    entries, which is exactly as malformed as a non-iterable value and
    must fail the same way, not with a confusing downstream error about
    a character missing an 's' attribute."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ProviderSymbologyError(
            f"Databento symbology resolution for raw symbol {raw_symbol!r} has a malformed "
            f"entries container (expected a list/tuple of mapping entries; got {value!r} of "
            f"type {type(value).__name__}); refusing to fetch without confirmed contract "
            f"identity."
        )
    return list(value)


def _entry_instrument_id_value(entry: object, *, raw_symbol: str) -> object:
    """Phase 3.3 §12: extract the raw ``"s"`` value from one symbology
    mapping entry, raising :class:`ProviderSymbologyError` -- never a
    raw ``AttributeError``/``TypeError`` -- when ``entry`` itself is
    not a usable shape at all (``None``, an empty/non-empty dict with
    no ``"s"`` key, a list, a bare string/number/bool, or any other
    type lacking an ``s`` attribute)."""
    if isinstance(entry, dict):
        if "s" not in entry:
            raise ProviderSymbologyError(
                f"Databento symbology resolution for raw symbol {raw_symbol!r} returned a "
                f"mapping entry with no 's' (instrument ID) field; refusing to fetch without "
                f"confirmed contract identity."
            )
        return entry["s"]
    if entry is None or isinstance(entry, (str, bytes, int, float, bool, list, tuple, set)):
        raise ProviderSymbologyError(
            f"Databento symbology resolution for raw symbol {raw_symbol!r} contains a "
            f"malformed mapping entry (expected a dict-like object with an 's' field; got "
            f"{entry!r} of type {type(entry).__name__}); refusing to fetch without confirmed "
            f"contract identity."
        )
    if not hasattr(entry, "s"):
        raise ProviderSymbologyError(
            f"Databento symbology resolution for raw symbol {raw_symbol!r} returned a "
            f"mapping entry with no 's' (instrument ID) field; refusing to fetch without "
            f"confirmed contract identity."
        )
    return getattr(entry, "s")


def _require_valid_symbology_result(result: dict, *, raw_symbol: str) -> None:
    """Phase 3 final completion pass (item 3): validate the ENTIRE
    ``result`` mapping structurally -- every key, every entries
    container, every entry's shape, and every entry's ``"s"`` value --
    not only ``result[raw_symbol]``, which Phase 3.3's hardening
    validated in isolation. Olive's own usage always submits exactly
    one symbol per ``symbology.resolve()`` call (``symbols=[raw_symbol]``
    in both :meth:`DatabentoHistoricalProvider._resolve_instrument_id`
    call sites), so in practice this loop iterates over at most one real
    key today -- but it is written generically, over every key the
    vendor actually returned, rather than hardcoded to ``raw_symbol``
    alone, for two reasons: a response attributed to a key OTHER than
    the one Olive actually requested is just as untrustworthy a vendor
    response as a malformed entry for the requested key would be (the
    whole response should be structurally trustworthy, not merely the
    one slice Olive happens to read), and a future change extending
    Olive to resolve multiple symbols in one call must not silently
    inherit a validator that only ever checked one of them."""
    for key, entries_value in result.items():
        if not isinstance(key, str) or not key.strip():
            raise ProviderSymbologyError(
                f"Databento symbology resolution has a malformed 'result' mapping key "
                f"{key!r} (expected a non-empty raw-symbol string); refusing to fetch "
                f"without confirmed contract identity for {raw_symbol!r}."
            )
        sibling_entries = _require_symbology_entries_sequence(entries_value, raw_symbol=key)
        for entry in sibling_entries:
            instrument_id = _entry_instrument_id_value(entry, raw_symbol=key)
            if not _is_valid_databento_instrument_id_string(instrument_id):
                raise ProviderSymbologyError(
                    f"Databento symbology resolution for raw symbol {key!r} returned a "
                    f"malformed instrument ID in a mapping entry ({instrument_id!r}); "
                    f"refusing to fetch without confirmed contract identity for "
                    f"{raw_symbol!r}."
                )


def _distinct_resolved_instrument_id(resolution: object, raw_symbol: str) -> str:
    """Parse a ``symbology.resolve()`` response and return the single
    instrument ID ``raw_symbol`` resolves to, or raise
    :class:`ProviderSymbologyError` if the resolution is missing,
    partial, empty, ambiguous (maps to more than one distinct
    instrument ID across the requested window), or MALFORMED (Phase
    3.3 §11/§12: any mapping entry that is not a usable shape, or
    whose instrument-ID value is not a well-formed positive
    decimal-digit string) -- in every case, BEFORE any paid
    ``timeseries.get_range`` call.

    Written against Databento's documented resolution JSON shape
    (``{"result": {symbol: [{"d0":..., "d1":..., "s": instrument_id}, ...]},
    "not_found": [...], "partial": [...]}``); see this module's
    docstring for the same API-drift caveat that applies to the rest of
    this adapter.

    Phase 3.3 QA compliance rework (items 7/8): every container at every
    level of this response tree -- ``not_found``, ``partial``, ``result``
    itself, and ``result[raw_symbol]`` -- is now validated for its actual
    shape BEFORE any ``in``/iteration operation touches it, never relying
    on Python's ``or``-based truthiness coercion (which does not
    distinguish a wrongly-typed truthy value from a correctly-typed one)
    or on a bare ``for``/``in`` to fail loudly on its own. Independent
    review reproduced three malformed-container inputs that previously
    leaked a raw ``TypeError`` instead of the documented
    :class:`ProviderSymbologyError`:
    ``{"result": {"NQZ6": 123}, "not_found": [], "partial": []}``,
    ``{"result": {"NQZ6": [...]}, "not_found": 123, "partial": []}``, and
    the same with ``partial=123``. All three (and the analogous
    malformed-``result`` case) are now covered.

    Phase 3 final completion pass (items C/3): ``not_found``/``partial``
    now validate every ELEMENT, not only the container's type (see
    :func:`_require_symbology_container`), and the entire ``result``
    mapping is validated structurally -- every key, every entries
    container, every entry, and every ``"s"`` value -- via
    :func:`_require_valid_symbology_result`, not only
    ``result[raw_symbol]`` in isolation.

    On ``d0``/``d1``: Databento's documented mapping-entry shape is
    ``{"d0": ..., "d1": ..., "s": instrument_id}``, where ``d0``/``d1``
    are the sub-interval (within the queried window) that one entry's
    mapping covers. This adapter deliberately never reads ``d0``/``d1``
    -- a malformed or inconsistent ``d0``/``d1`` therefore cannot affect
    Olive's correctness at all, because no code path consumes them. The
    two properties this function actually needs are already guaranteed
    by other means: (1) an identity CHANGE within the queried window
    (different sub-intervals resolving to different instrument IDs) is
    caught by the ``len(distinct_ids) != 1`` ambiguity check below,
    regardless of how many ``d0``/``d1`` sub-intervals produced those
    ids; (2) a coverage GAP (the symbol not resolving for part of the
    window) is Databento's own documented responsibility to report via
    the ``not_found``/``partial`` classification -- a symbol present in
    ``result`` with no sibling ``not_found``/``partial`` entry is
    documented as resolving for the ENTIRE queried window, which is
    exactly what this function already requires before ever reaching
    the entries loop. No additional ``d0``/``d1`` validation is added:
    it would duplicate a guarantee this function already has from a
    different, already-enforced direction, for a value nothing here
    ever reads.
    """
    mapping = _resolution_mapping(resolution)
    not_found = _require_symbology_container(
        mapping.get("not_found"), field_name="not_found", raw_symbol=raw_symbol
    )
    partial = _require_symbology_container(
        mapping.get("partial"), field_name="partial", raw_symbol=raw_symbol
    )

    if raw_symbol in not_found:
        raise ProviderSymbologyError(
            f"Databento symbology resolution could not find any mapping for raw symbol "
            f"{raw_symbol!r} in the requested window; refusing to fetch without confirmed "
            f"contract identity."
        )
    if raw_symbol in partial:
        raise ProviderSymbologyError(
            f"Databento symbology resolution for raw symbol {raw_symbol!r} is only PARTIALLY "
            f"mapped over the requested window; refusing to fetch without full, confirmed "
            f"contract identity for the entire window."
        )

    result = _require_symbology_result_mapping(mapping.get("result"), raw_symbol=raw_symbol)
    _require_valid_symbology_result(result, raw_symbol=raw_symbol)
    raw_entries = result.get(raw_symbol)
    if raw_entries is None:
        raise ProviderSymbologyError(
            f"Databento symbology resolution returned no mapping entries for raw symbol "
            f"{raw_symbol!r}; refusing to fetch without confirmed contract identity."
        )
    entries = _require_symbology_entries_sequence(raw_entries, raw_symbol=raw_symbol)
    if not entries:
        raise ProviderSymbologyError(
            f"Databento symbology resolution returned no mapping entries for raw symbol "
            f"{raw_symbol!r}; refusing to fetch without confirmed contract identity."
        )

    distinct_ids: set[str] = set()
    for entry in entries:
        raw_instrument_id = _entry_instrument_id_value(entry, raw_symbol=raw_symbol)
        if not _is_valid_databento_instrument_id_string(raw_instrument_id):
            raise ProviderSymbologyError(
                f"Databento symbology resolution for raw symbol {raw_symbol!r} returned a "
                f"malformed instrument ID in a mapping entry ({raw_instrument_id!r}); refusing "
                f"to fetch without confirmed contract identity."
            )
        distinct_ids.add(raw_instrument_id.strip())

    if len(distinct_ids) != 1:
        raise ProviderSymbologyError(
            f"Databento raw symbol {raw_symbol!r} resolves AMBIGUOUSLY to "
            f"{len(distinct_ids)} distinct instrument IDs over the requested window; Olive "
            f"never guesses which one is correct. Refusing to fetch."
        )
    return next(iter(distinct_ids))


def _require_databento_client_interface(client: Any) -> None:
    """Phase 3.2 §15: the injected-client dependency-injection seam is
    itself a PUBLIC boundary -- a malformed injected client (``"bad"``,
    ``None``-bearing attributes, an object missing the methods this
    adapter actually calls, ...) must be rejected at CONSTRUCTION time
    with an Olive-owned error, never accepted and left to leak a raw
    ``AttributeError`` the first time ``estimate_cost()``/
    ``fetch_bars()`` touches one of its expected attributes.

    Validates the CALLABLE INTERFACE Olive actually needs
    (``metadata.get_cost``, ``timeseries.get_range``,
    ``symbology.resolve``), not a strict ``isinstance`` against
    Databento's concrete client class -- test doubles implementing
    only this interface are an intentional, valuable part of this
    adapter's design (see this module's docstring), and a strict
    ``isinstance`` check would defeat that for no safety benefit.
    """
    missing: list[str] = []
    metadata = getattr(client, "metadata", None)
    if metadata is None or not callable(getattr(metadata, "get_cost", None)):
        missing.append("metadata.get_cost")
    timeseries = getattr(client, "timeseries", None)
    if timeseries is None or not callable(getattr(timeseries, "get_range", None)):
        missing.append("timeseries.get_range")
    symbology = getattr(client, "symbology", None)
    if symbology is None or not callable(getattr(symbology, "resolve", None)):
        missing.append("symbology.resolve")
    if missing:
        raise ProviderNotConfiguredError(
            "The injected Databento client is missing the required callable interface this "
            f"adapter needs ({', '.join(missing)}); refusing to construct "
            "DatabentoHistoricalProvider with a client that cannot service requests."
        )


class DatabentoHistoricalProvider(HistoricalMarketDataProvider):
    """Olive's historical provider adapter for Databento
    (``GLBX.MDP3``)."""

    def __init__(self, api_key: str, dataset: str = DEFAULT_DATASET, client: Optional[Any] = None) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderNotConfiguredError("Databento provider requires a non-empty API key")
        if not isinstance(dataset, str) or not dataset.strip():
            raise ProviderNotConfiguredError("Databento provider requires a non-empty dataset code")

        self._dataset = dataset.strip()

        if client is not None:
            # Dependency injection path: every test in
            # tests/test_historical_data_databento.py uses this, so
            # the real `databento` package is never required to be
            # installed to verify this adapter's own logic. Phase 3.2
            # §15: still validate the interface this adapter actually
            # needs before accepting it.
            _require_databento_client_interface(client)
            self._client = client
        else:
            try:
                import databento  # Phase 3 third-party import safety: lazy, only here.
            except ImportError as exc:
                raise ProviderNotConfiguredError(
                    "OLIVE_HISTORICAL_PROVIDER=databento is selected, but the 'databento' "
                    "package is not installed in this environment."
                ) from exc
            self._client = databento.Historical(key=api_key)
        # api_key is intentionally never stored as an attribute beyond
        # this point -- it cannot leak through repr()/logging/errors
        # raised by this adapter because it is nowhere to leak from.

    @property
    def name(self) -> str:
        return "databento"

    @property
    def dataset(self) -> str:
        return self._dataset

    def estimate_cost(self, request: HistoricalBarRequest) -> CostEstimate:
        request = _require_historical_bar_request(request, context="DatabentoHistoricalProvider.estimate_cost")
        raw_symbol = request.contract.display_code
        schema = request.timeframe.databento_schema

        try:
            raw_cost = self._client.metadata.get_cost(
                dataset=self._dataset,
                symbols=[raw_symbol],
                schema=schema,
                start=request.start,
                end=request.end,
                stype_in="raw_symbol",
            )
        except Exception as exc:
            if _is_databento_exception(exc):
                raise CostEstimationFailedError(
                    "Databento cost estimation failed: " + str(_translate_databento_exception(exc))
                ) from None
            raise

        try:
            cost_decimal = Decimal(str(raw_cost))
        except (DecimalException, TypeError, ValueError):
            raise CostEstimationFailedError(
                f"Databento returned a non-numeric cost estimate (type {type(raw_cost).__name__})"
            ) from None

        # A malformed-but-parseable cost (NaN/Infinity/negative) is a
        # PROVIDER failure, not a caller-input problem -- Phase 3.1 fix:
        # CostEstimate's own validation raises InvalidHistoricalRequestError
        # (a sibling of HistoricalProviderError, not a subclass), which
        # would otherwise escape HistoricalDataService's cost-estimation
        # except clause (which only catches HistoricalProviderError).
        try:
            return CostEstimate(estimated_cost_usd=cost_decimal)
        except InvalidHistoricalRequestError:
            raise CostEstimationFailedError(
                f"Databento returned a malformed cost estimate (must be finite and "
                f"non-negative): got {cost_decimal}"
            ) from None

    def _resolve_instrument_id(self, raw_symbol: str, start_date: date, end_date: date) -> str:
        """Resolve ``raw_symbol`` to its unique instrument ID over
        ``[start_date, end_date]`` via Databento's free symbology
        resolution, failing closed (never calling the paid
        ``timeseries.get_range``) on any missing/ambiguous/partial/
        malformed resolution. See this module's docstring, §13-15 of
        the Phase 3.1 correction, and ``docs/historical_data.md``.
        """
        symbology = getattr(self._client, "symbology", None)
        if symbology is None:
            raise ProviderSymbologyError(
                "The configured Databento client has no symbology-resolution capability; "
                "refusing to fetch without confirmed contract identity."
            )
        try:
            resolution = symbology.resolve(
                dataset=self._dataset,
                symbols=[raw_symbol],
                stype_in="raw_symbol",
                stype_out="instrument_id",
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:
            if _is_databento_exception(exc):
                raise ProviderSymbologyError(
                    "Databento symbology resolution failed; refusing to fetch without "
                    "confirmed contract identity."
                ) from None
            raise
        return _distinct_resolved_instrument_id(resolution, raw_symbol)

    def fetch_bars(self, request: HistoricalBarRequest, instrument: FuturesInstrument) -> tuple[HistoricalBar, ...]:
        request = _require_historical_bar_request(request, context="DatabentoHistoricalProvider.fetch_bars")
        if not isinstance(instrument, FuturesInstrument):
            raise InvalidHistoricalRequestError(
                f"DatabentoHistoricalProvider.fetch_bars expects a FuturesInstrument, got "
                f"{type(instrument).__name__}"
            )
        if instrument.root_symbol != request.contract.root_symbol:
            raise HistoricalBarContractMismatchError(
                f"fetch_bars was given instrument root {instrument.root_symbol!r} but the "
                f"request's contract root is {request.contract.root_symbol!r}."
            )

        raw_symbol = request.contract.display_code
        schema = request.timeframe.databento_schema

        # -- Phase 3.1 identity safety gate (§13-15): resolve the raw
        # symbol's instrument ID BOTH within the contract's own month
        # AND over the request's actual date range, and require they
        # agree, BEFORE any paid get_range call. This is the only
        # defense against Databento's documented raw-symbol reuse
        # across years/decades (NQZ6 means a different contract in
        # 2016/2026/2036) -- the response-level symbol check alone
        # cannot catch it, because a stale/misdirected request can
        # still receive rows that honestly self-report symbol="NQZ6".
        contract_month_start = date(int(request.contract.year), int(request.contract.month), 1)
        expected_instrument_id = self._resolve_instrument_id(
            raw_symbol, contract_month_start, contract_month_start + timedelta(days=1)
        )

        # Phase 3.2 §9 fix: symbology.resolve's end_date is an
        # EXCLUSIVE UTC calendar DATE, but request.end is an exclusive
        # UTC DATETIME that can fall at any time-of-day. The previous
        # implementation only extended request_end_date by one day
        # when it was <= request_start_date (the same-calendar-day
        # case) -- a genuinely cross-day request with an intraday end
        # time (e.g. start=2026-09-30T23:00Z, end=2026-10-01T00:30Z)
        # produced request_end_date=2026-10-01 UNCHANGED, which, being
        # EXCLUSIVE, covers nothing of October 1st at all -- yet
        # October 1st 00:00-00:30 is squarely inside the requested
        # half-open datetime interval. The DATE interval passed to
        # symbology.resolve must fully cover every instant in
        # [request.start, request.end): request.end's own calendar
        # date still contains requested instants unless request.end is
        # EXACTLY that date's first instant (00:00:00.000000) -- the
        # same half-open-boundary reasoning as
        # app.data.storage._months_between's own fix.
        request_start_date = request.start.date()
        if (
            request.end.hour == 0
            and request.end.minute == 0
            and request.end.second == 0
            and request.end.microsecond == 0
        ):
            request_end_date = request.end.date()
        else:
            request_end_date = request.end.date() + timedelta(days=1)
        if request_end_date <= request_start_date:
            # Defensive fallback only; the branch above already
            # handles every reachable case given start < end is
            # already enforced by HistoricalBarRequest.
            request_end_date = request_start_date + timedelta(days=1)
        actual_instrument_id = self._resolve_instrument_id(raw_symbol, request_start_date, request_end_date)

        if actual_instrument_id != expected_instrument_id:
            raise ProviderSymbologyError(
                f"Raw symbol {raw_symbol!r} resolves to a different Databento instrument over "
                f"the requested date range than it does within contract "
                f"{request.contract.identity}'s own month. Databento raw symbols are reused "
                f"across years; refusing to fetch what may be a different contract-year's data."
            )

        try:
            store = self._client.timeseries.get_range(
                dataset=self._dataset,
                symbols=[raw_symbol],
                schema=schema,
                start=request.start,
                end=request.end,
                stype_in="raw_symbol",
                stype_out="instrument_id",
            )
        except Exception as exc:
            if _is_databento_exception(exc):
                raise _translate_databento_exception(exc) from None
            raise

        try:
            data_frame = store.to_df(price_type="decimal", tz="UTC", map_symbols=True)
        except Exception as exc:
            # Phase 3.1 fix: only a genuine Databento-originated decode
            # failure is translated; an unrelated programming error
            # (Olive calling to_df with the wrong signature, say) must
            # propagate unmodified, never be disguised as a provider
            # outage.
            if _is_databento_exception(exc):
                raise ProviderUnavailableError("Databento response could not be decoded.") from None
            raise

        if len(data_frame) == 0:
            return ()

        # Phase 3.1 fix: OHLCV schemas must be indexed by ts_event
        # specifically -- accepting ts_recv here would silently
        # relabel receive time as event time.
        if data_frame.index.name != "ts_event":
            raise ProviderUnavailableError(
                f"Unexpected Databento response shape: DataFrame index name "
                f"{data_frame.index.name!r} (OHLCV schemas must be indexed by 'ts_event')."
            )

        bars: list[HistoricalBar] = []
        for ts_event, row in data_frame.iterrows():
            # Phase 3.1 fix: with map_symbols=True explicitly requested,
            # a missing symbol column/value means response identity
            # cannot be verified at all -- fail closed rather than
            # silently skipping the check (the previous `is not None`
            # guard let a None symbol slip straight through).
            row_symbol = getattr(row, "symbol", None)
            if row_symbol is None:
                raise ProviderResponseIdentityError(
                    "Databento response did not include a resolved 'symbol' for this row "
                    "(map_symbols=True was requested); refusing to store data whose identity "
                    "cannot be verified."
                )
            if row_symbol != raw_symbol:
                raise ProviderResponseIdentityError(
                    f"Databento returned data identified as symbol {row_symbol!r}, but Olive "
                    f"requested {raw_symbol!r}. Refusing to store a response that does not "
                    f"identify itself as the requested contract."
                )

            # Phase 3.2 §8 fix (critical): a MISSING instrument_id must
            # fail closed, never be silently replaced with the
            # symbology-resolved expected_instrument_id. Doing so
            # defeated the entire point of this defense-in-depth
            # per-row check: it let a response row with no
            # instrument_id at all sail through storage with Olive's
            # own provenance substituted for what Databento actually
            # (didn't) report. A pandas-missing numeric value surfaces
            # as NaN, not None, so that is checked too.
            row_instrument_id = getattr(row, "instrument_id", None)
            is_missing = row_instrument_id is None or (
                isinstance(row_instrument_id, float) and row_instrument_id != row_instrument_id  # NaN
            )
            if is_missing:
                raise ProviderResponseIdentityError(
                    "Databento response row did not include an instrument_id; refusing to "
                    "fabricate provider provenance for a response that did not actually "
                    "report one (defense-in-depth symbology verification requires it)."
                )
            row_instrument_id_str = str(row_instrument_id).strip()
            if not row_instrument_id_str:
                raise ProviderResponseIdentityError(
                    "Databento response row has an empty/blank instrument_id; refusing to "
                    "store data whose identity cannot be verified."
                )
            if row_instrument_id_str != expected_instrument_id:
                raise ProviderSymbologyError(
                    f"Databento returned a row whose instrument_id {row_instrument_id!r} does "
                    f"not match the instrument_id {expected_instrument_id!r} resolved for "
                    f"contract {request.contract.identity}; refusing to store a bar that may "
                    f"belong to a different contract-year instrument sharing the same raw "
                    f"symbol."
                )

            ts_event_utc = ts_event.to_pydatetime() if hasattr(ts_event, "to_pydatetime") else ts_event

            # Phase 3.1 fix (§9): an expected provider-row data-quality
            # problem (tick-misaligned price, bad volume, malformed
            # OHLC, ...) must become a structured provider failure, not
            # escape as a raw HistoricalDataError that
            # HistoricalDataService's `except HistoricalProviderError`
            # around fetch_bars does not catch.
            try:
                volume = _safe_volume(row.volume)
                bar = HistoricalBar.from_decimal_prices(
                    contract=request.contract,
                    timeframe=request.timeframe,
                    ts_event=ts_event_utc,
                    open_price=row.open,
                    high_price=row.high,
                    low_price=row.low,
                    close_price=row.close,
                    volume=volume,
                    tick_size=instrument.tick_size,
                    provider=self.name,
                    dataset=self._dataset,
                    provider_raw_symbol=raw_symbol,
                    provider_instrument_id=row_instrument_id_str,
                )
            except ProviderDataError:
                raise
            except HistoricalDataError as exc:
                raise ProviderDataError(f"Databento row failed Olive's bar normalization: {exc}") from None
            bars.append(bar)

        return tuple(bars)
