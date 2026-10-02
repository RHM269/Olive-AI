"""Quarterly contract expiration/roll calendar for Olive's futures domain.

Distinguishes OFFICIAL (source-backed) cycle dates from a labeled
CALCULATED_NOMINAL fallback, so Olive never silently presents a
guessed date as exchange-authoritative. See docs/futures_domain.md
for the full rationale, especially the June 2026 holiday exception
(nominal third Friday 2026-06-19 vs. official 2026-06-18).

This module performs no network I/O. ``load_cycle_date_calendar``
reads a local, version-controlled JSON file.

Phase 2.1 hardening note: this calendar represents the NQ/MNQ
*quarterly* cycle only (March/June/September/December). Every public
entry point that accepts a month -- ``nominal_third_friday``,
``CycleDateCalendar.get``, and ``CycleDateCalendar.has_official`` --
validates it before doing anything else, so no internal contract
cycle for a non-quarterly month (e.g. January) can ever be produced
by this module, and no stdlib exception (e.g.
``calendar.IllegalMonthError``) can leak out of it for an invalid
month.

Phase 2.2 hardening note: a second external audit found that
``CycleDateCalendar`` could be constructed *directly* (bypassing
``load_cycle_date_calendar``'s validation) with impossible internal
state -- a malformed ``as_of``, an out-of-range ``covered_years``, or
an ``official_dates`` mapping whose keys disagree with their own
values. ``CycleDateCalendar.__init__`` now performs the full set of
invariant checks itself (see the module-level ``_validate_*``
functions below), so direct construction can no longer produce an
inconsistent calendar. ``load_cycle_date_calendar`` still parses raw
JSON into typed values (it has to -- the constructor expects already
-typed ``ContractCycleDates`` objects, not raw JSON dicts) and wraps
any resulting error with file-path context, but all of the *semantic*
validation lives in exactly one place: the constructor. The calendar's
year/month validation (``nominal_third_friday``, ``CycleDateCalendar.get``,
``CycleDateCalendar.has_official``) also now rejects a non-integer or
bool year (e.g. ``True``, ``"2026"``) with a domain error rather than
leaking a raw ``TypeError`` from the stdlib ``calendar`` module.

Phase 2.3 hardening note: a third external audit found two remaining
gaps. First, ``load_cycle_date_calendar``'s ``config_path`` parameter
required an actual ``Path`` -- passing a plain ``str`` leaked a raw
``AttributeError`` from ``path.exists()``. It now accepts anything
path-like (``Path``, ``str``, or any other ``os.PathLike``),
normalized to a ``Path`` up front; ``None`` still means "use the real
config file." Second, this module now exposes
``_require_cycle_calendar``, a shared validator (mirroring
``app.futures.models._require_instrument`` /
``_require_contract`` / ``_require_cycle_dates``) for public functions
elsewhere in the futures domain (``app.futures.roll``,
``app.futures.sessions``) that accept a ``CycleDateCalendar``
parameter -- it must live here rather than in ``models.py`` because
``CycleDateCalendar`` itself is defined here, and ``models.py`` has no
dependency on this module (avoiding a circular import).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app.futures.models import (
    ContractCycleDates,
    CycleDateError,
    CycleDateSource,
    InvalidDateError,
    _MAX_REASONABLE_YEAR,
    _MIN_REASONABLE_YEAR,
    _require_year,
    nominal_customary_roll,
    nominal_third_friday,
    require_quarterly_month,
)

# nominal_third_friday / nominal_customary_roll are defined in
# app.futures.models (so ContractCycleDates.__post_init__ can use them
# without a circular import) and re-exported here under their
# historical, still-public names -- `from app.futures.calendar import
# nominal_third_friday, nominal_customary_roll` continues to work
# unchanged; the import above is the entire re-export.

CHICAGO_TZ = ZoneInfo("America/Chicago")
FINAL_TRADING_TIME_CT = (8, 30)  # 8:30 a.m. Central Time, per CME quarterly termination time.

_QUARTERLY_MONTHS: tuple[int, ...] = (3, 6, 9, 12)

# app/futures/calendar.py -> app/futures -> app -> project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CYCLE_DATE_CONFIG_PATH = (
    _PROJECT_ROOT / "config" / "calendar" / "cme_equity_index_cycle_dates.json"
)


def final_trading_timestamp(expiration_date: date) -> datetime:
    """Build the timezone-aware final-trading timestamp for an expiration date.

    Quarterly NQ/MNQ contracts normally terminate at 8:30 a.m. Central
    Time on their scheduled final trading day. Always returns an
    ``America/Chicago``-aware datetime -- never a naive one.

    ``expiration_date`` must be a ``date`` (a ``datetime`` is also
    accepted, since it subclasses ``date``, but only its
    year/month/day components are used -- any time-of-day on the
    input is intentionally ignored, since the result's time is always
    the fixed 8:30 a.m. termination time). Anything else (a string,
    ``None``, etc.) raises :class:`~app.futures.models.InvalidDateError`
    rather than leaking a raw ``AttributeError``.
    """
    if not isinstance(expiration_date, date):
        raise InvalidDateError(
            f"final_trading_timestamp expects a date, got {type(expiration_date).__name__}"
        )
    hour, minute = FINAL_TRADING_TIME_CT
    return datetime(
        expiration_date.year,
        expiration_date.month,
        expiration_date.day,
        hour,
        minute,
        tzinfo=CHICAGO_TZ,
    )


def _validate_official_dates(official_dates: object) -> dict[tuple[int, int], ContractCycleDates]:
    """Validate an ``official_dates`` mapping for direct ``CycleDateCalendar``
    construction (shared with the JSON loader, which builds this same
    shape before calling the constructor).

    Every check here is about internal consistency of already-typed
    values (unlike the JSON loader's per-row parsing, which also has to
    convert raw strings into ``date``/``int`` values first):

    - ``official_dates`` itself must be a mapping.
    - Each key must be a ``(year, month)`` tuple of true (non-bool) ints,
      with ``month`` one of Olive's quarterly months.
    - Each value must be a :class:`ContractCycleDates`.
    - The key must agree with the value's own declared ``year``/``month``
      (this is what catches, e.g., a value keyed under September that
      actually holds June's dates).
    - The value's ``source`` must be ``OFFICIAL`` -- this mapping only
      ever holds source-backed entries; a ``CALCULATED_NOMINAL`` value
      has no business being stored as "official" data.
    """
    if not isinstance(official_dates, Mapping):
        raise CycleDateError(
            f"CycleDateCalendar: official_dates must be a mapping, got {type(official_dates).__name__}"
        )

    validated: dict[tuple[int, int], ContractCycleDates] = {}
    for key, value in official_dates.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or isinstance(key[0], bool)
            or not isinstance(key[0], int)
            or isinstance(key[1], bool)
            or not isinstance(key[1], int)
        ):
            raise CycleDateError(
                f"CycleDateCalendar: official_dates keys must be (year, month) int tuples, got {key!r}"
            )
        key_year, key_month = key
        if key_year < _MIN_REASONABLE_YEAR or key_year > _MAX_REASONABLE_YEAR:
            raise CycleDateError(f"CycleDateCalendar: official_dates key year out of range: {key_year}")
        require_quarterly_month(key_month)

        if not isinstance(value, ContractCycleDates):
            raise CycleDateError(
                f"CycleDateCalendar: official_dates value for {key!r} must be a ContractCycleDates, "
                f"got {type(value).__name__}"
            )
        if value.year != key_year or value.month != key_month:
            raise CycleDateError(
                f"CycleDateCalendar: official_dates key {key!r} does not match its value's own "
                f"declared cycle ({value.year}-{value.month:02d})"
            )
        if value.source is not CycleDateSource.OFFICIAL:
            raise CycleDateError(
                f"CycleDateCalendar: official_dates entry for {key!r} must have source OFFICIAL, "
                f"got {value.source!r}"
            )
        validated[(key_year, key_month)] = value

    return validated


def _validate_covered_years(covered_years: object) -> tuple[int, ...]:
    """Validate a ``covered_years`` sequence for direct construction (and,
    via the loader, for JSON-sourced data too)."""
    if not isinstance(covered_years, (tuple, list)) or not covered_years:
        raise CycleDateError("CycleDateCalendar: covered_years must be a non-empty sequence of years")

    years: list[int] = []
    for value in covered_years:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CycleDateError(f"CycleDateCalendar: covered_years entries must be integers, got {value!r}")
        if value < _MIN_REASONABLE_YEAR or value > _MAX_REASONABLE_YEAR:
            raise CycleDateError(f"CycleDateCalendar: covered_years entry out of reasonable range: {value}")
        years.append(value)

    if len(set(years)) != len(years):
        raise CycleDateError("CycleDateCalendar: covered_years must not contain duplicates")

    return tuple(years)


def _validate_as_of(as_of: object) -> str:
    if not isinstance(as_of, str) or not as_of.strip():
        raise CycleDateError("CycleDateCalendar: as_of must be a non-empty ISO date string")
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise CycleDateError(f"CycleDateCalendar: as_of is not a valid ISO date: {as_of!r}") from exc
    return as_of


def _validate_source(source: object) -> str:
    if not isinstance(source, str) or not source.strip():
        raise CycleDateError("CycleDateCalendar: source must be a non-empty string")
    return source


def _cross_validate_coverage(
    *,
    official: dict[tuple[int, int], ContractCycleDates],
    covered_years: tuple[int, ...],
) -> None:
    """Ensure declared ``covered_years`` and actual OFFICIAL rows agree exactly."""
    covered_years_set = set(covered_years)

    for year in covered_years:
        missing_months = [m for m in _QUARTERLY_MONTHS if (year, m) not in official]
        if missing_months:
            raise CycleDateError(
                f"covered_years claims {year} but is missing OFFICIAL entries for month(s) "
                f"{missing_months} -- full quarterly coverage (3, 6, 9, 12) is required for every "
                "claimed year"
            )

    official_years = {year for (year, _month) in official}
    for year in official_years:
        if year not in covered_years_set:
            raise CycleDateError(
                f"OFFICIAL entries exist for year {year}, but {year} is not listed in covered_years "
                "-- declared coverage must not omit actual authoritative data"
            )


class CycleDateCalendar:
    """Quarterly expiration/roll dates, preferring official data over calculation.

    Phase 2.2 hardening: the constructor validates ``official_dates``,
    ``covered_years``, ``as_of``, and ``source`` itself (see the
    module-level ``_validate_*`` helpers), and cross-validates
    declared coverage against the actual official data -- this holds
    regardless of whether the calendar came from
    :func:`load_cycle_date_calendar` or was constructed directly.
    ``official_dates`` is also copied into an immutable
    ``MappingProxyType`` so a caller's continued mutable reference to
    the dict they passed in cannot alter the calendar's truth after
    construction.
    """

    def __init__(
        self,
        official_dates: Mapping[tuple[int, int], ContractCycleDates],
        as_of: str,
        covered_years: tuple[int, ...],
        source: str,
    ):
        official = _validate_official_dates(official_dates)
        covered_years_validated = _validate_covered_years(covered_years)
        _cross_validate_coverage(official=official, covered_years=covered_years_validated)

        self._official: Mapping[tuple[int, int], ContractCycleDates] = MappingProxyType(official)
        self.as_of = _validate_as_of(as_of)
        self.covered_years = covered_years_validated
        self.source = _validate_source(source)

    def get(self, year: int, month: int) -> ContractCycleDates:
        """Return cycle dates for (year, month).

        Returns the OFFICIAL, source-backed entry when one exists;
        otherwise falls back to a CALCULATED_NOMINAL third-Friday /
        customary-roll calculation, clearly labeled as such via
        ``ContractCycleDates.source``.

        Raises :class:`CycleDateError` if ``year`` is not a true
        (non-bool) int in a reasonable range, or if ``month`` is not
        one of Olive's quarterly months -- this calendar never
        generates a contract cycle for e.g. January, and never leaks a
        raw ``TypeError`` for a malformed year such as a string.
        """
        year = _require_year(year, context="CycleDateCalendar.get")
        month = require_quarterly_month(month)
        key = (year, month)
        official = self._official.get(key)
        if official is not None:
            return official

        nominal_expiration = nominal_third_friday(year, month)
        nominal_roll = nominal_customary_roll(nominal_expiration)
        return ContractCycleDates(
            year=year,
            month=month,
            expiration=nominal_expiration,
            roll=nominal_roll,
            source=CycleDateSource.CALCULATED_NOMINAL,
        )

    def has_official(self, year: int, month: int) -> bool:
        """Whether an OFFICIAL (source-backed) entry exists for (year, month).

        Raises :class:`CycleDateError` for a malformed year or a
        non-quarterly month, for the same reason as :meth:`get`.
        """
        year = _require_year(year, context="CycleDateCalendar.has_official")
        month = require_quarterly_month(month)
        return (year, month) in self._official


def _require_cycle_calendar(value: object, *, context: str) -> CycleDateCalendar:
    """Require a :class:`CycleDateCalendar`.

    Phase 2.3 hardening: shared by every public function elsewhere in
    the futures domain (``app.futures.roll``, ``app.futures.sessions``)
    that takes a ``cycle_calendar`` parameter, so a caller's mistake
    (e.g. passing the wrong object, or no object at all) raises a
    predictable :class:`CycleDateError` instead of a raw
    ``AttributeError`` the first time the function calls ``.get(...)``
    or ``.has_official(...)`` on it. Lives here, not in ``models.py``,
    because ``CycleDateCalendar`` is defined here and ``models.py`` has
    no dependency on this module.
    """
    if not isinstance(value, CycleDateCalendar):
        raise CycleDateError(f"{context} expects a CycleDateCalendar, got {type(value).__name__}")
    return value


def load_cycle_date_calendar(config_path: Path | str | os.PathLike | None = None) -> CycleDateCalendar:
    """Load the official quarterly cycle-date table from JSON configuration.

    Args:
        config_path: Override the configuration file location (used by
            tests to exercise malformed-configuration handling).
            Accepts anything path-like -- a ``Path``, a plain ``str``,
            or any other ``os.PathLike`` -- normalized to a ``Path``
            before use (previously a plain ``str`` would leak a raw
            ``AttributeError`` the first time ``.exists()`` was called
            on it). Defaults (``None``) to the real
            ``config/calendar/cme_equity_index_cycle_dates.json``.

    Parses each date row (see :func:`_parse_cycle_entry`) into a typed
    ``ContractCycleDates``, then hands the result -- together with the
    file's raw ``as_of``/``covered_years``/``source`` values -- to
    :class:`CycleDateCalendar`'s constructor, which performs all of the
    semantic validation (metadata cross-checks included). Any error
    the constructor raises is re-raised with this file's path prefixed
    for easier debugging; the validation logic itself lives in exactly
    one place (the constructor), not duplicated here.
    """
    if config_path is None:
        path = DEFAULT_CYCLE_DATE_CONFIG_PATH
    else:
        try:
            path = Path(config_path)
        except TypeError as exc:
            raise CycleDateError(
                f"Cycle-date calendar path must be path-like (a Path, str, or os.PathLike), "
                f"got {type(config_path).__name__}"
            ) from exc

    if not path.exists():
        raise CycleDateError(f"Cycle-date calendar file not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CycleDateError(f"Could not read cycle-date calendar file {path}: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CycleDateError(f"Cycle-date calendar at {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise CycleDateError(f"Cycle-date calendar at {path} must be a JSON object")

    dates_raw = raw.get("dates")
    if not isinstance(dates_raw, list) or not dates_raw:
        raise CycleDateError(f"Cycle-date calendar at {path} must contain a non-empty 'dates' list")

    official: dict[tuple[int, int], ContractCycleDates] = {}
    for index, entry in enumerate(dates_raw):
        cycle = _parse_cycle_entry(entry, source_label=f"{path}[dates][{index}]")
        key = (cycle.year, cycle.month)
        if key in official:
            raise CycleDateError(f"{path}: duplicate cycle-date entry for {cycle.year}-{cycle.month:02d}")
        official[key] = cycle

    try:
        return CycleDateCalendar(
            official_dates=official,
            as_of=raw.get("as_of"),
            covered_years=raw.get("covered_years"),
            source=raw.get("source"),
        )
    except CycleDateError as exc:
        raise CycleDateError(f"{path}: {exc}") from exc


def _parse_cycle_entry(entry: Any, source_label: str) -> ContractCycleDates:
    if not isinstance(entry, dict):
        raise CycleDateError(f"{source_label}: entry must be a JSON object")

    year = entry.get("year")
    month = entry.get("month")
    if isinstance(year, bool) or not isinstance(year, int):
        raise CycleDateError(f"{source_label}: 'year' must be an integer")
    if isinstance(month, bool) or not isinstance(month, int) or month not in _QUARTERLY_MONTHS:
        raise CycleDateError(f"{source_label}: 'month' must be one of {_QUARTERLY_MONTHS}")

    expiration_raw = entry.get("expiration")
    roll_raw = entry.get("roll")
    if not isinstance(expiration_raw, str) or not isinstance(roll_raw, str):
        raise CycleDateError(f"{source_label}: 'expiration' and 'roll' must be ISO date strings")

    try:
        expiration = date.fromisoformat(expiration_raw)
    except ValueError as exc:
        raise CycleDateError(f"{source_label}: invalid 'expiration' date: {expiration_raw!r}") from exc

    try:
        roll = date.fromisoformat(roll_raw)
    except ValueError as exc:
        raise CycleDateError(f"{source_label}: invalid 'roll' date: {roll_raw!r}") from exc

    # ContractCycleDates.__post_init__ enforces the full set of cross-field
    # invariants (expiration/roll actually belong to the declared year/month,
    # roll < expiration, roll is the exact customary roll Monday for this
    # cycle's nominal third Friday, etc.) -- re-raise with the source_label
    # context so a malformed row is locatable in the file.
    try:
        return ContractCycleDates(
            year=year,
            month=month,
            expiration=expiration,
            roll=roll,
            source=CycleDateSource.OFFICIAL,
        )
    except CycleDateError as exc:
        raise CycleDateError(f"{source_label}: {exc}") from exc
