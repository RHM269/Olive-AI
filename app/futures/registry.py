"""Futures instrument registry: loads and validates instrument definitions.

Generic loader vs. Olive's tradable universe (read this before assuming
every loadable instrument is tradable):

``FuturesInstrumentRegistry`` / ``load_instrument_registry`` are
intentionally GENERIC: they will load and validate any
well-formed instrument configuration, not just NQ/MNQ. This genericity
exists for testability (malformed-configuration tests use a throwaway
custom config) and so a future instrument could be added without a
rewrite here. It means the registry loader by itself does **not**
enforce "Olive only trades NQ/MNQ."

That production constraint -- Olive's Phase 2 tradable universe is
*exactly* ``{"NQ", "MNQ"}``, and each of those roots must match
Olive's canonical NQ/MNQ financial specification exactly -- is enforced
separately, by ``app.futures.validation.validate_olive_tradable_registry``,
against the *real*, loaded ``config/instruments/nq_mnq.json``. (This
previously lived directly in ``app.health._check_futures_domain()``;
Phase 2.4 moved it into ``app.futures.validation`` so there is exactly
one home for "is this Olive's required production domain?" --
``app.health`` now only *consumes* that validator's result.) A config
that loads successfully but contains an unexpected extra root (e.g.
"ES"), or a self-consistent but factually wrong NQ/MNQ definition, is
still rejected there, even though this module would have happily
loaded it. Do not assume that "loads without error here" implies
"tradable by Olive" -- check ``app.futures.validation`` instead.

Phase 2.5 hardening note: an internal QA process correction found that
``app.futures.validation``'s production validators took a ``registry``
parameter entirely on faith, so a wrong-type or wrong-kind argument
(e.g. a ``CycleDateCalendar`` passed where a registry was expected)
leaked a raw ``AttributeError`` instead of a domain error. This module
now exposes ``_require_registry``, a shared validator mirroring
``app.futures.models._require_instrument`` / ``_require_contract`` /
``_require_cycle_dates`` and ``app.futures.calendar._require_cycle_calendar``,
for any public function elsewhere in the futures domain that accepts a
``FuturesInstrumentRegistry`` parameter.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from app.futures.models import (
    ContractMonth,
    FuturesConfigurationError,
    FuturesInstrument,
    InvalidRegistryError,
    SettlementType,
    UnknownInstrumentError,
)

# app/futures/registry.py -> app/futures -> app -> project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_INSTRUMENT_CONFIG_PATH = _PROJECT_ROOT / "config" / "instruments" / "nq_mnq.json"

_REQUIRED_STRING_FIELDS = ("root_symbol", "display_name", "exchange", "underlying", "currency")
_REQUIRED_DECIMAL_FIELDS = ("multiplier", "tick_size", "tick_value")


class FuturesInstrumentRegistry:
    """An immutable, validated collection of :class:`FuturesInstrument` objects.

    Phase 2.2 hardening: the constructor itself validates that this is
    genuinely a self-consistent collection, regardless of how it was
    built (not only via :func:`load_instrument_registry`) --

    - ``instruments`` must be a mapping.
    - Every key must be a non-empty root-symbol string.
    - Every value must be a :class:`FuturesInstrument`.
    - A key's canonical form (stripped, upper-cased) must equal its
      instrument's own ``root_symbol`` -- a registry entry filed under
      the "wrong" key is rejected rather than silently stored.
    - Duplicate roots after canonicalization are rejected.

    This remains a deliberately GENERIC container: it does not enforce
    Olive's "only NQ/MNQ" production policy. That policy is enforced
    separately by ``app.futures.validation.validate_olive_tradable_registry``
    (consumed, in turn, by ``app.health``) -- never by this class itself.
    """

    def __init__(self, instruments: Mapping[str, FuturesInstrument]):
        if not isinstance(instruments, Mapping):
            raise FuturesConfigurationError(
                f"FuturesInstrumentRegistry requires a mapping of root symbol -> FuturesInstrument, "
                f"got {type(instruments).__name__}"
            )

        validated: dict[str, FuturesInstrument] = {}
        for key, value in instruments.items():
            if not isinstance(key, str) or not key.strip():
                raise FuturesConfigurationError(
                    f"FuturesInstrumentRegistry: root symbol keys must be non-empty strings, got {key!r}"
                )
            canonical_key = key.strip().upper()

            if not isinstance(value, FuturesInstrument):
                raise FuturesConfigurationError(
                    f"FuturesInstrumentRegistry: value for {key!r} must be a FuturesInstrument, "
                    f"got {type(value).__name__}"
                )
            if value.root_symbol != canonical_key:
                raise FuturesConfigurationError(
                    f"FuturesInstrumentRegistry: key {key!r} (canonical {canonical_key!r}) does not "
                    f"match its instrument's own root_symbol {value.root_symbol!r}"
                )
            if canonical_key in validated:
                raise FuturesConfigurationError(
                    f"FuturesInstrumentRegistry: duplicate root symbol after canonicalization: "
                    f"{canonical_key}"
                )
            validated[canonical_key] = value

        self._instruments: dict[str, FuturesInstrument] = validated

    def get(self, root_symbol: str) -> FuturesInstrument:
        """Look up an instrument by root symbol (case-insensitive).

        Raises :class:`UnknownInstrumentError` for anything not
        explicitly loaded into the registry -- Olive never fabricates
        an instrument definition for an unrecognized symbol. This
        includes a non-string ``root_symbol`` (e.g. ``None`` or an
        int): it is simply unknown, never a raw ``AttributeError``
        from calling ``.strip()`` on it.
        """
        if not isinstance(root_symbol, str):
            known = ", ".join(sorted(self._instruments)) or "(none)"
            raise UnknownInstrumentError(
                f"Unknown futures root symbol: {root_symbol!r} (root symbols must be strings). "
                f"Known instruments: {known}"
            )
        key = root_symbol.strip().upper()
        try:
            return self._instruments[key]
        except KeyError as exc:
            known = ", ".join(sorted(self._instruments)) or "(none)"
            raise UnknownInstrumentError(
                f"Unknown futures root symbol: {root_symbol!r}. Known instruments: {known}"
            ) from exc

    def contains(self, root_symbol: object) -> bool:
        """Whether ``root_symbol`` is a known instrument (case-insensitive).

        A non-string ``root_symbol`` simply returns ``False`` rather
        than leaking a raw ``AttributeError`` -- "is this a known
        instrument?" has an unambiguous "no" for anything that isn't
        even a string.
        """
        if not isinstance(root_symbol, str):
            return False
        return root_symbol.strip().upper() in self._instruments

    def all(self) -> tuple[FuturesInstrument, ...]:
        return tuple(self._instruments.values())

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(self._instruments.keys())

    def __len__(self) -> int:
        return len(self._instruments)

    def __contains__(self, root_symbol: object) -> bool:
        return isinstance(root_symbol, str) and self.contains(root_symbol)


# -- Shared public-API object validator --------------------------------------
#
# Phase 2.5 hardening: mirrors app.futures.models._require_instrument /
# _require_contract / _require_cycle_dates and
# app.futures.calendar._require_cycle_calendar exactly. Defined here
# (rather than in models.py) because FuturesInstrumentRegistry is defined
# here, and models.py has no dependency on this module (avoiding a
# circular import). Any public function elsewhere in the futures domain
# that accepts a FuturesInstrumentRegistry parameter -- currently the
# Phase 2.4 production validators in app.futures.validation -- should use
# this instead of touching the parameter's attributes on faith.
#
# Deliberately a genuine isinstance() check, not a hasattr()/duck-typing
# check: a hand-built object that merely happens to expose a `.roots`
# attribute (correctly or with its own bug) must still be rejected, since
# only a real FuturesInstrumentRegistry has already self-validated its
# own internal consistency at construction time.


def _require_registry(value: object, *, context: str) -> FuturesInstrumentRegistry:
    if not isinstance(value, FuturesInstrumentRegistry):
        raise InvalidRegistryError(
            f"{context} expects a FuturesInstrumentRegistry, got {type(value).__name__}"
        )
    return value


def load_instrument_registry(config_path: Path | str | os.PathLike | None = None) -> FuturesInstrumentRegistry:
    """Load and validate the futures instrument registry from JSON configuration.

    Args:
        config_path: Override the configuration file location (used by
            tests to exercise malformed-configuration handling without
            touching the real ``config/instruments/nq_mnq.json``).
            Accepts anything path-like -- a ``Path``, a plain ``str``,
            or any other ``os.PathLike`` -- normalized to a ``Path``
            before use (previously a plain ``str`` would leak a raw
            ``AttributeError`` the first time ``.exists()`` was called
            on it). ``None`` (the default) means the real config file.
    """
    if config_path is None:
        path = DEFAULT_INSTRUMENT_CONFIG_PATH
    else:
        try:
            path = Path(config_path)
        except TypeError as exc:
            raise FuturesConfigurationError(
                f"Instrument configuration path must be path-like (a Path, str, or os.PathLike), "
                f"got {type(config_path).__name__}"
            ) from exc

    if not path.exists():
        raise FuturesConfigurationError(f"Instrument configuration file not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FuturesConfigurationError(f"Could not read instrument configuration file {path}: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise FuturesConfigurationError(f"Instrument configuration at {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise FuturesConfigurationError(f"Instrument configuration at {path} must be a JSON object")

    instruments_raw = raw.get("instruments")
    if not isinstance(instruments_raw, list) or not instruments_raw:
        raise FuturesConfigurationError(
            f"Instrument configuration at {path} must contain a non-empty 'instruments' list"
        )

    instruments: dict[str, FuturesInstrument] = {}
    for index, entry in enumerate(instruments_raw):
        instrument = _parse_instrument(entry, source=f"{path}[instruments][{index}]")
        if instrument.root_symbol in instruments:
            raise FuturesConfigurationError(
                f"Duplicate instrument root symbol in configuration: {instrument.root_symbol}"
            )
        instruments[instrument.root_symbol] = instrument

    return FuturesInstrumentRegistry(instruments)


def _parse_instrument(entry: Any, source: str) -> FuturesInstrument:
    if not isinstance(entry, dict):
        raise FuturesConfigurationError(f"{source}: instrument entry must be a JSON object")

    values: dict[str, str] = {}
    for field in _REQUIRED_STRING_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            raise FuturesConfigurationError(f"{source}: '{field}' must be a non-empty string")
        values[field] = value.strip()

    decimals: dict[str, Decimal] = {}
    for field in _REQUIRED_DECIMAL_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str):
            raise FuturesConfigurationError(
                f"{source}: '{field}' must be a JSON string (e.g. \"0.25\"), not {type(value).__name__}, "
                "to avoid floating-point precision issues in contract economics"
            )
        try:
            decimals[field] = Decimal(value)
        except InvalidOperation as exc:
            raise FuturesConfigurationError(f"{source}: '{field}' is not a valid decimal: {value!r}") from exc

    settlement_raw = entry.get("settlement_type")
    try:
        settlement = SettlementType(settlement_raw)
    except ValueError as exc:
        valid = ", ".join(member.value for member in SettlementType)
        raise FuturesConfigurationError(
            f"{source}: unknown settlement_type {settlement_raw!r}. Must be one of: {valid}"
        ) from exc

    months_raw = entry.get("contract_months")
    if not isinstance(months_raw, list) or not months_raw:
        raise FuturesConfigurationError(f"{source}: 'contract_months' must be a non-empty list")

    months: list[ContractMonth] = []
    for month_value in months_raw:
        # ContractMonth(3.0) succeeds (Python's IntEnum lookup treats a
        # float equal to a member's value as a match), which would let a
        # JSON float like 3.0 slip through as if it were the true int 3.
        # Require a true (non-bool) int in the raw JSON value BEFORE
        # attempting the ContractMonth conversion.
        if isinstance(month_value, bool) or not isinstance(month_value, int):
            raise FuturesConfigurationError(
                f"{source}: contract month must be an integer, got {month_value!r} "
                f"({type(month_value).__name__})"
            )
        try:
            months.append(ContractMonth(month_value))
        except ValueError as exc:
            valid = ", ".join(str(int(m)) for m in ContractMonth)
            raise FuturesConfigurationError(
                f"{source}: unsupported contract month {month_value!r}. Must be one of: {valid}"
            ) from exc

    try:
        return FuturesInstrument(
            root_symbol=values["root_symbol"].upper(),
            display_name=values["display_name"],
            exchange=values["exchange"],
            underlying=values["underlying"],
            currency=values["currency"],
            multiplier=decimals["multiplier"],
            tick_size=decimals["tick_size"],
            tick_value=decimals["tick_value"],
            settlement_type=settlement,
            contract_months=tuple(months),
        )
    except FuturesConfigurationError as exc:
        # Re-raise with source context so a malformed entry is locatable.
        raise FuturesConfigurationError(f"{source}: {exc}") from exc
