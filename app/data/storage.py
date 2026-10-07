"""Local Parquet storage for Olive's historical market data (Phase 3).

Deterministic, idempotent, atomic, and provider-independent: nothing
in this module talks to Databento or any other provider, and every
test in ``tests/test_historical_data_storage.py`` constructs plain
:class:`~app.data.models.HistoricalBar` fixtures and writes/reads them
with no network, no API key, and no provider at all.

Storage layout (deterministic, never dependent on the current working
directory, a random UUID, or a secret value -- only on validated
canonical fields and a configurable root directory):

    <root_dir>/<provider>/<dataset>/<root_symbol>/<contract_identity>/
        <timeframe_schema_segment>/year=<YYYY>/month=<MM>/bars.parquet
        <same dir>/manifest.json

e.g. ``data/historical/databento/GLBX.MDP3/NQ/NQ-2026-12/ohlcv-1m/year=2026/month=09/``.
The partition's year/month is the bar data's OWN calendar year/month
(``ts_event``), not the contract's expiration month -- a December 2026
contract's September-2026 trading activity is stored under
``year=2026/month=09``.

Idempotency and conflict handling: writing the same (or an
overlapping) request twice never duplicates records. Two bars sharing
a canonical key (contract + timeframe + ``ts_event``) with identical
content are de-duplicated to one; two bars sharing a key with
CONFLICTING content raise :class:`~app.data.models.HistoricalStorageIntegrityError`
-- checked for EVERY affected partition before any partition is
written, so a conflict discovered in one partition never leaves
another partition in this same call partially written.

Atomicity: each partition's Parquet file (and its manifest) is written
to a temporary file in the same directory and then atomically renamed
into place (``os.replace``) -- a failure partway through a write never
leaves a corrupted or half-written file where a good one used to be,
and any temporary file is cleaned up on failure.

Third-party import safety: ``pyarrow`` is imported lazily, only inside
the methods that actually need it -- importing this module, or
constructing a :class:`HistoricalBarStore`, never requires ``pyarrow``
to be installed; only an actual read/write call does.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Optional, Sequence

from app.data.models import (
    DataLabel,
    HistoricalBar,
    HistoricalStorageError,
    HistoricalStorageIntegrityError,
    HistoricalTimeframe,
    OLIVE_HISTORICAL_BAR_SCHEMA_VERSION,
    _coerce_timeframe,
    _require_aware_datetime,
)

# Phase 3.1 §24: the set of schema versions this storage layer knows
# how to read/write. Currently only the single current version -- no
# migration logic exists yet, so an unrecognized version (older or
# newer) must fail closed rather than be silently accepted as if it
# were compatible.
_SUPPORTED_BAR_SCHEMA_VERSIONS = frozenset({OLIVE_HISTORICAL_BAR_SCHEMA_VERSION})

_PARQUET_FILENAME = "bars.parquet"
_MANIFEST_FILENAME = "manifest.json"
_FORBIDDEN_SEGMENT_CHARS = ("/", "\\", "\0")


def _import_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise HistoricalStorageError(
            "The 'pyarrow' package is not installed in this environment; it is required for "
            "Olive's historical Parquet storage layer. Install it (see requirements.txt) to use "
            "HistoricalBarStore."
        ) from exc
    return pa, pq


def _require_storage_int(value: object, *, field_name: str) -> int:
    """Require a true (non-bool) ``int`` for one of this module's own
    public-API parameters, raising :class:`HistoricalStorageError`
    (never the mismatched ``app.data.models`` ``HistoricalBar.<field>``
    -worded errors that module's own ``_require_true_int`` raises --
    these are storage-layer caller parameters, not ``HistoricalBar``
    fields). Rejects ``bool`` (an ``int`` subtype), ``float`` (even a
    whole-number one such as ``3.0``), and numeric strings -- none of
    those are ever silently coerced."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalStorageError(
            f"{field_name} must be a true int (never bool, float, or str); got {value!r} "
            f"({type(value).__name__})"
        )
    return value


def _require_storage_decimal(value: object, *, field_name: str, allow_zero: bool) -> Decimal:
    """Require a finite, non-negative (optionally strictly positive)
    ``Decimal`` for one of this module's own public-API parameters,
    raising :class:`HistoricalStorageError`."""
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise HistoricalStorageError(
            f"{field_name} must be a Decimal (never bool/float/int/str); got {value!r} "
            f"({type(value).__name__})"
        )
    if not value.is_finite():
        raise HistoricalStorageError(f"{field_name} must be finite (not NaN/Infinity); got {value}")
    if allow_zero:
        if value < 0:
            raise HistoricalStorageError(f"{field_name} must not be negative; got {value}")
    elif value <= 0:
        raise HistoricalStorageError(f"{field_name} must be strictly positive; got {value}")
    return value


def _safe_path_segment(value: object, *, field_name: str) -> str:
    """Defend storage paths against path traversal, even though every
    current caller already supplies pre-validated canonical values
    (a Phase 2 root symbol/contract identity, Olive's own enum
    values, a fixed provider/dataset string) -- CLAUDE.md's standing
    rule is to never assume a loader's/caller's validation makes a
    boundary safe on its own."""
    if not isinstance(value, str) or not value.strip():
        raise HistoricalStorageError(f"{field_name} must be a non-empty string, got {value!r}")
    candidate = value.strip()
    if candidate in (".", ".."):
        raise HistoricalStorageError(f"{field_name} must not be a path-traversal segment: {candidate!r}")
    for bad_char in _FORBIDDEN_SEGMENT_CHARS:
        if bad_char in candidate:
            raise HistoricalStorageError(f"{field_name} must not contain {bad_char!r}: {candidate!r}")
    return candidate


# -- Phase 3.3 QA compliance rework (items 4-6): a single, reusable manifest
# schema-validation boundary -------------------------------------------------
#
# Independent reproduction tampered a valid one-row manifest to
# {"record_count": true, "schema_version": true} and the partition was still
# ACCEPTED, because every existing check compared manifest values with plain
# Python `==`/`!=`/`in` -- and `True == 1` and `True in {1}` are both `True`
# in Python; `1.0 == 1` is `True` too. Generic equality is not a type check.
# These helpers validate each durable manifest field's RUNTIME TYPE and
# basic invariant explicitly, rejecting a bool/float masquerading as an int
# (or any other wrong-shaped value) BEFORE it is ever compared against a
# requested/expected/content value -- never silently coerced.

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_manifest_int(value: object, *, field_name: str, partition_dir: Path) -> int:
    """A manifest integer field must be a true (non-bool) ``int`` --
    ``True``/``False`` (``int`` subtypes) and any ``float`` (even a
    whole-number one such as ``1.0``) are rejected explicitly, never
    silently treated as equal to the int they resemble."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r} must be a true int "
            f"(never bool/float/str/None); got {value!r} ({type(value).__name__})."
        )
    return value


def _require_manifest_nonempty_str(value: object, *, field_name: str, partition_dir: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r} must be a non-empty "
            f"string; got {value!r} ({type(value).__name__})."
        )
    return value


def _require_manifest_checksum(value: object, *, partition_dir: Path) -> str:
    if not isinstance(value, str) or not _SHA256_HEX_RE.match(value):
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field 'checksum_sha256' must be a 64-character "
            f"lowercase hexadecimal SHA-256 digest; got {value!r}."
        )
    return value


def _require_manifest_decimal(
    value: object, *, field_name: str, partition_dir: Path, allow_zero: bool, nullable: bool
) -> Optional[Decimal]:
    """A manifest Decimal-valued field (``tick_size``/
    ``contract_multiplier``/``estimated_cost_usd``) is always written
    as its ``str(Decimal(...))`` serialization -- never a bare JSON
    number, which would silently lose Decimal's exact-precision
    guarantee. Rejects ``bool``/``int``/``float`` explicitly (even
    though ``Decimal("1")`` would gladly accept an ``int``), and
    requires the parsed value to be finite and correctly signed."""
    if value is None:
        if nullable:
            return None
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r} must not be null."
        )
    if isinstance(value, bool) or not isinstance(value, str):
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r} must be a Decimal-"
            f"serialized string (never bool/int/float); got {value!r} ({type(value).__name__})."
        )
    try:
        decimal_value = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r}={value!r} is not a valid "
            f"Decimal serialization."
        ) from exc
    if not decimal_value.is_finite():
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r}={value!r} must be finite "
            f"(never NaN/Infinity)."
        )
    if allow_zero:
        if decimal_value < 0:
            raise HistoricalStorageIntegrityError(
                f"Partition {partition_dir} manifest field {field_name!r}={value!r} must not be "
                f"negative."
            )
    elif decimal_value <= 0:
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r}={value!r} must be "
            f"strictly positive."
        )
    return decimal_value


def _require_manifest_utc_timestamp(
    value: object, *, field_name: str, partition_dir: Path, nullable: bool
) -> Optional[datetime]:
    if value is None:
        if nullable:
            return None
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r} must not be null."
        )
    if isinstance(value, bool) or not isinstance(value, str):
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r} must be an ISO-8601 "
            f"timestamp string; got {value!r} ({type(value).__name__})."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r}={value!r} is not a valid "
            f"ISO-8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field {field_name!r}={value!r} must be a "
            f"timezone-aware UTC timestamp."
        )
    return parsed


def _require_valid_manifest_schema(manifest: dict, *, partition_dir: Path) -> None:
    """The single, reusable manifest-schema validation boundary: every
    durable manifest field's runtime TYPE and basic invariant is
    checked here, explicitly, BEFORE ``_verify_partition_manifest``
    compares any of these values against requested/expected values and
    before ``_verify_manifest_content_matches_bars`` compares them
    against actually-decoded bar content. Called immediately after a
    manifest is parsed as JSON and confirmed to be a ``dict`` -- never
    after, since every comparison downstream assumes these types
    already hold. Raises :class:`HistoricalStorageIntegrityError` on
    the first malformed field found; never silently coerces a value to
    the type it resembles."""
    _require_manifest_int(manifest.get("schema_version"), field_name="schema_version", partition_dir=partition_dir)
    _require_manifest_int(manifest.get("contract_year"), field_name="contract_year", partition_dir=partition_dir)
    _require_manifest_int(manifest.get("contract_month"), field_name="contract_month", partition_dir=partition_dir)
    _require_manifest_int(manifest.get("partition_year"), field_name="partition_year", partition_dir=partition_dir)
    _require_manifest_int(manifest.get("partition_month"), field_name="partition_month", partition_dir=partition_dir)

    record_count = _require_manifest_int(
        manifest.get("record_count"), field_name="record_count", partition_dir=partition_dir
    )
    # write_bars never creates a partition directory without at least one
    # bar to write (every planned partition's merged bar set is non-empty by
    # construction -- see write_bars's own invariant comment) -- a stored
    # record_count of zero (or negative, already rejected by being a true
    # int check above only ruling out non-ints, not sign) is not a
    # legitimate "empty partition" state, it is an impossible/corrupt one.
    if record_count <= 0:
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest declares record_count={record_count!r}; a "
            f"persisted partition must contain at least one record. Refusing to trust an "
            f"impossible/corrupt manifest state."
        )

    _require_manifest_nonempty_str(manifest.get("provider"), field_name="provider", partition_dir=partition_dir)
    _require_manifest_nonempty_str(manifest.get("dataset"), field_name="dataset", partition_dir=partition_dir)
    _require_manifest_nonempty_str(manifest.get("root_symbol"), field_name="root_symbol", partition_dir=partition_dir)
    _require_manifest_nonempty_str(
        manifest.get("olive_contract_identity"), field_name="olive_contract_identity", partition_dir=partition_dir
    )

    timeframe_value = _require_manifest_nonempty_str(
        manifest.get("timeframe"), field_name="timeframe", partition_dir=partition_dir
    )
    try:
        HistoricalTimeframe(timeframe_value)
    except ValueError:
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field 'timeframe'={timeframe_value!r} is not a "
            f"recognized HistoricalTimeframe value."
        )

    _require_manifest_nonempty_str(
        manifest.get("provider_raw_symbol"), field_name="provider_raw_symbol", partition_dir=partition_dir
    )

    data_label_value = _require_manifest_nonempty_str(
        manifest.get("data_label"), field_name="data_label", partition_dir=partition_dir
    )
    try:
        DataLabel(data_label_value)
    except ValueError:
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field 'data_label'={data_label_value!r} is not "
            f"a recognized DataLabel value."
        )

    storage_format = manifest.get("storage_format")
    if storage_format != "parquet":
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest field 'storage_format' must be exactly "
            f"'parquet'; got {storage_format!r}."
        )

    _require_manifest_checksum(manifest.get("checksum_sha256"), partition_dir=partition_dir)

    first_ts = _require_manifest_utc_timestamp(
        manifest.get("actual_first_timestamp_utc"),
        field_name="actual_first_timestamp_utc",
        partition_dir=partition_dir,
        nullable=False,
    )
    last_ts = _require_manifest_utc_timestamp(
        manifest.get("actual_last_timestamp_utc"),
        field_name="actual_last_timestamp_utc",
        partition_dir=partition_dir,
        nullable=False,
    )
    if first_ts > last_ts:
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest declares actual_first_timestamp_utc="
            f"{first_ts.isoformat()!r} AFTER actual_last_timestamp_utc={last_ts.isoformat()!r}; "
            f"refusing to trust an internally impossible manifest."
        )
    requested_start = _require_manifest_utc_timestamp(
        manifest.get("requested_start_utc"),
        field_name="requested_start_utc",
        partition_dir=partition_dir,
        nullable=True,
    )
    requested_end = _require_manifest_utc_timestamp(
        manifest.get("requested_end_utc"), field_name="requested_end_utc", partition_dir=partition_dir, nullable=True
    )
    # Phase 3 final completion pass (item B): each of requested_start_utc/
    # requested_end_utc was already individually validated above as a
    # well-formed nullable UTC timestamp, but the PAIR was never
    # compared -- a forged manifest with requested_start_utc=2026-10-01
    # and requested_end_utc=2026-09-01 (both individually valid) passed
    # undetected. write_bars's own public boundary already requires
    # `requested_start < requested_end` (strictly) before either value is
    # ever written into a manifest in the first place, so this mirrors
    # that same strict contract on read -- a manifest that reaches disk
    # with a pair the public boundary would have rejected is an
    # integrity gap, not a cosmetic one. One-sided (only one of the two
    # present) remains a legitimate manifest state and is left alone.
    if requested_start is not None and requested_end is not None and not requested_start < requested_end:
        raise HistoricalStorageIntegrityError(
            f"Partition {partition_dir} manifest declares requested_start_utc="
            f"{requested_start.isoformat()!r} not strictly before requested_end_utc="
            f"{requested_end.isoformat()!r}; refusing to trust an internally impossible "
            f"manifest."
        )

    _require_manifest_decimal(
        manifest.get("tick_size"),
        field_name="tick_size",
        partition_dir=partition_dir,
        allow_zero=False,
        nullable=False,
    )
    _require_manifest_decimal(
        manifest.get("contract_multiplier"),
        field_name="contract_multiplier",
        partition_dir=partition_dir,
        allow_zero=False,
        nullable=True,
    )
    _require_manifest_decimal(
        manifest.get("estimated_cost_usd"),
        field_name="estimated_cost_usd",
        partition_dir=partition_dir,
        allow_zero=True,
        nullable=True,
    )

    # Phase 3 final completion pass (item A): last_written_at_utc is
    # written into every manifest (see the manifest-construction dict in
    # write_bars) but was never validated at all -- independent audit
    # confirmed both last_written_at_utc=True and
    # last_written_at_utc="not-a-date" were silently accepted. It carries
    # no cross-field relationship to any other field (it records when
    # this manifest was written, not a fact about the bars themselves),
    # so only the individual non-nullable-UTC-timestamp invariant is
    # required, using the same reusable validator as every other
    # timestamp field.
    _require_manifest_utc_timestamp(
        manifest.get("last_written_at_utc"),
        field_name="last_written_at_utc",
        partition_dir=partition_dir,
        nullable=False,
    )


@dataclass(frozen=True)
class HistoricalWriteResult:
    """The outcome of one :meth:`HistoricalBarStore.write_bars` call.

    Phase 3.2 §3 hardening: independent review constructed
    ``HistoricalWriteResult(partitions_written=-1, new_records=-2,
    total_records=-3)`` successfully -- an impossible result for a
    domain object whose entire purpose is to be a trustworthy
    structured outcome ``HistoricalDataService`` and callers rely on
    without re-checking. Every field is now validated in
    ``__post_init__``, exactly like every other Phase 2/3 domain
    object -- including the cross-field invariant that
    ``new_records`` (bars genuinely new this call) can never exceed
    ``total_records`` (everything now on disk for the affected
    partitions, new and pre-existing combined).
    """

    partitions_written: int
    new_records: int
    total_records: int

    def __post_init__(self) -> None:
        partitions_written = _require_storage_int(self.partitions_written, field_name="partitions_written")
        new_records = _require_storage_int(self.new_records, field_name="new_records")
        total_records = _require_storage_int(self.total_records, field_name="total_records")
        if partitions_written < 0:
            raise HistoricalStorageError(
                f"HistoricalWriteResult.partitions_written must not be negative; got {partitions_written}"
            )
        if new_records < 0:
            raise HistoricalStorageError(
                f"HistoricalWriteResult.new_records must not be negative; got {new_records}"
            )
        if total_records < 0:
            raise HistoricalStorageError(
                f"HistoricalWriteResult.total_records must not be negative; got {total_records}"
            )
        if new_records > total_records:
            raise HistoricalStorageError(
                f"HistoricalWriteResult is internally inconsistent: new_records "
                f"({new_records}) cannot exceed total_records ({total_records}) -- a write "
                f"can never report more NEW records than the total now on disk."
            )
        # Phase 3.3 §16 fix: a second cross-field invariant -- every
        # WRITTEN partition must contain at least one record, so
        # partitions_written can never exceed total_records. This also
        # means the only valid all-zero result is
        # (partitions_written=0, total_records=0) -- if total_records
        # is 0, partitions_written > 0 is caught by this same check.
        if partitions_written > total_records:
            raise HistoricalStorageError(
                f"HistoricalWriteResult is internally inconsistent: partitions_written "
                f"({partitions_written}) cannot exceed total_records ({total_records}) -- "
                f"every partition reported as written must contain at least one record."
            )
        # Phase 3.3 QA compliance rework (item 10): the inverse relationship was
        # still unconstrained -- HistoricalWriteResult(partitions_written=0,
        # new_records=0, total_records=5) passed every check above (0 is not
        # negative, 0 <= 5, 0 <= 5) despite being impossible for write_bars to
        # ever actually produce (a non-empty write always writes at least one
        # partition). Combined with the checks above, this one new invariant
        # (total_records > 0 implies partitions_written > 0) is sufficient to
        # also imply new_records > 0 -> partitions_written > 0 (since
        # new_records <= total_records already forces total_records > 0
        # whenever new_records > 0) -- without over-constraining the
        # legitimate idempotent-duplicate-write case (partitions_written > 0,
        # new_records == 0, total_records > 0), which remains valid.
        if total_records > 0 and partitions_written == 0:
            raise HistoricalStorageError(
                f"HistoricalWriteResult is internally inconsistent: total_records "
                f"({total_records}) is positive but partitions_written is 0 -- a non-empty "
                f"write always writes at least one partition."
            )


@dataclass(frozen=True)
class HistoricalReadResult:
    """The outcome of one :meth:`HistoricalBarStore.read_bars` call.

    ``partitions_found == 0`` means no monthly partition file existed
    anywhere in the requested range (nothing has ever been stored for
    this contract/timeframe/window) -- distinct from
    ``partitions_found > 0`` with an empty ``bars`` (partitions
    existed, but every stored bar fell outside the exact requested
    sub-range). Both are legitimate, explicit "no data" outcomes, never
    ambiguous with a storage failure (which raises instead).
    """

    bars: tuple[HistoricalBar, ...]
    partitions_found: int

    def __post_init__(self) -> None:
        # Phase 3.2 §4 hardening: independent review constructed
        # HistoricalReadResult(bars=(), partitions_found=-1)
        # successfully, and direct construction could accept malformed
        # `bars` (non-sequence, or containing non-HistoricalBar
        # elements). found_any_data must never be capable of failing
        # or lying because an impossible object was allowed to exist.
        bars = self.bars
        if not isinstance(bars, (tuple, list)):
            raise HistoricalStorageError(
                f"HistoricalReadResult.bars must be a tuple/list of HistoricalBar, got "
                f"{bars!r} ({type(bars).__name__})"
            )
        for bar in bars:
            if not isinstance(bar, HistoricalBar):
                raise HistoricalStorageError(
                    f"HistoricalReadResult.bars must contain only HistoricalBar instances, "
                    f"got an element of type {type(bar).__name__}"
                )
        object.__setattr__(self, "bars", tuple(bars))

        partitions_found = _require_storage_int(self.partitions_found, field_name="partitions_found")
        if partitions_found < 0:
            raise HistoricalStorageError(
                f"HistoricalReadResult.partitions_found must not be negative; got {partitions_found}"
            )
        # Phase 3.3 QA compliance rework (item 9): independent reproduction
        # constructed HistoricalReadResult(bars=(real_bar,), partitions_found=0)
        # successfully -- impossible given this object's own documented
        # semantics (partitions_found == 0 means no partition file existed at
        # all; a bar cannot have been read from zero partitions).
        if bars and partitions_found < 1:
            raise HistoricalStorageError(
                f"HistoricalReadResult is internally inconsistent: {len(bars)} bar(s) were "
                f"returned but partitions_found={partitions_found} -- it is impossible to "
                f"legitimately return stored bars from zero partitions."
            )
        object.__setattr__(self, "partitions_found", partitions_found)

    @property
    def found_any_data(self) -> bool:
        return len(self.bars) > 0


def _months_between(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Enumerate the (year, month) pairs any bar within the half-open
    interval ``[start, end)`` could possibly fall into.

    Phase 3.1 §29 fix: ``end`` is EXCLUSIVE. If ``end`` lands exactly
    on the first instant of a month (e.g. ``2026-10-01T00:00:00Z``),
    that month contributes NO possible bar (nothing can be both
    ``>= 2026-10-01T00:00:00Z`` and ``< 2026-10-01T00:00:00Z``), so it
    must be excluded from enumeration -- the previous implementation
    always included ``end``'s month, needlessly reading (and, more
    importantly, potentially misattributing provenance to) a
    partition that is guaranteed to contribute zero matching rows."""
    end_year, end_month = end.year, end.month
    if (
        end.day == 1
        and end.hour == 0
        and end.minute == 0
        and end.second == 0
        and end.microsecond == 0
    ):
        end_month -= 1
        if end_month < 1:
            end_month = 12
            end_year -= 1

    months: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end_year, end_month):
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def _normalize_stored_timestamp(value: object) -> datetime:
    """Normalize a timestamp read back from Parquet to exactly UTC,
    regardless of which UTC-equivalent ``tzinfo`` implementation the
    installed pyarrow/pandas version happens to attach (stdlib
    ``datetime.timezone.utc`` and other UTC implementations are
    numerically equivalent but not always the same object).

    Phase 3.1 §26 fix: a NAIVE (tzinfo-less) timestamp read back from
    storage must fail closed as corrupt/incompatible data -- Olive's
    storage schema declares ``ts_event`` as UTC-timestamped
    (``pa.timestamp("us", tz="UTC")``), so a naive value can only mean
    the Parquet file is corrupt or was written by an incompatible
    schema. The previous implementation silently assumed UTC, which
    could silently mislabel a timestamp that was actually written in
    a different, unknown timezone."""
    if not isinstance(value, datetime):
        raise HistoricalStorageIntegrityError(
            f"Stored ts_event must be a datetime, got {value!r} ({type(value).__name__}); the "
            f"Parquet data is corrupt or incompatible with Olive's storage schema."
        )
    if value.tzinfo is None:
        raise HistoricalStorageIntegrityError(
            "Stored ts_event is a naive (timezone-less) timestamp. Olive's historical storage "
            "schema declares ts_event as UTC; a naive value means the Parquet data is corrupt "
            "or was written by an incompatible schema. Refusing to silently assume UTC."
        )
    return value.astimezone(timezone.utc)


class HistoricalBarStore:
    """Olive's local Parquet-backed historical bar repository.

    ``root_dir`` must be supplied explicitly by the caller (typically
    ``Settings.historical_data_dir``) -- this class never falls back
    to the current working directory or a hard-coded user home
    directory.
    """

    def __init__(self, root_dir: Path | str) -> None:
        if isinstance(root_dir, Path):
            path = root_dir
        elif isinstance(root_dir, str):
            if not root_dir.strip():
                raise HistoricalStorageError("HistoricalBarStore root_dir must not be an empty string")
            path = Path(root_dir)
        else:
            raise HistoricalStorageError(
                f"HistoricalBarStore root_dir must be a Path or str, got {type(root_dir).__name__}"
            )
        # Phase 3.2 §16 fix: resolve to an absolute path ONCE, right
        # here at construction time, against the CWD as it is RIGHT
        # NOW. Previously a relative Path was stored verbatim and
        # re-resolved against the process's CURRENT working directory
        # on every later call (`_partition_dir` calls `.resolve()`
        # against `self._root_dir` each time) -- contradicting this
        # module's own documented guarantee that storage is never
        # dependent on the current working directory. The SAME store
        # instance must always resolve to the SAME physical location,
        # regardless of any later os.chdir() elsewhere in the process
        # (mirrors the Phase 3.1 fix already applied to
        # Settings.historical_data_dir).
        self._root_dir = path.resolve()

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    # -- path layout -----------------------------------------------------

    def _partition_dir(
        self,
        *,
        provider: str,
        dataset: str,
        root_symbol: str,
        contract_identity: str,
        schema_segment: str,
        year: int,
        month: int,
    ) -> Path:
        segments = [
            _safe_path_segment(provider, field_name="provider"),
            _safe_path_segment(dataset, field_name="dataset"),
            _safe_path_segment(root_symbol, field_name="root_symbol"),
            _safe_path_segment(contract_identity, field_name="contract_identity"),
            _safe_path_segment(schema_segment, field_name="timeframe"),
            f"year={year:04d}",
            f"month={month:02d}",
        ]
        partition_dir = self._root_dir
        for segment in segments:
            partition_dir = partition_dir / segment

        resolved_root = self._root_dir.resolve()
        resolved_partition = partition_dir.resolve()
        if resolved_partition != resolved_root and resolved_root not in resolved_partition.parents:
            raise HistoricalStorageError(
                f"Computed partition path {resolved_partition} escapes the storage root "
                f"{resolved_root}; refusing to read or write it."
            )
        return partition_dir

    # -- Arrow <-> HistoricalBar conversion -------------------------------

    @staticmethod
    def _arrow_schema(pa):
        return pa.schema(
            [
                pa.field("root_symbol", pa.string()),
                pa.field("contract_year", pa.int32()),
                pa.field("contract_month", pa.int32()),
                pa.field("timeframe", pa.string()),
                pa.field("ts_event", pa.timestamp("us", tz="UTC")),
                pa.field("open_ticks", pa.int64()),
                pa.field("high_ticks", pa.int64()),
                pa.field("low_ticks", pa.int64()),
                pa.field("close_ticks", pa.int64()),
                pa.field("volume", pa.int64()),
                pa.field("tick_size", pa.string()),
                pa.field("data_label", pa.string()),
                pa.field("provider", pa.string()),
                pa.field("dataset", pa.string()),
                pa.field("provider_raw_symbol", pa.string()),
                pa.field("provider_instrument_id", pa.string()),
                pa.field("schema_version", pa.int32()),
            ]
        )

    def _bars_to_table(self, bars: Sequence[HistoricalBar], pa):
        schema = self._arrow_schema(pa)
        columns: dict[str, list] = {f.name: [] for f in schema}
        for bar in bars:
            # Olive's own attribute access -- deliberately NOT wrapped
            # in the try/except below. A bug here (e.g. a typo'd
            # attribute name) is a genuine Olive programming error and
            # must propagate unmodified, never be disguised as a
            # third-party pyarrow failure.
            columns["root_symbol"].append(bar.root_symbol)
            columns["contract_year"].append(bar.contract_year)
            columns["contract_month"].append(bar.contract_month)
            columns["timeframe"].append(bar.timeframe.value)
            columns["ts_event"].append(bar.ts_event)
            columns["open_ticks"].append(bar.open_ticks)
            columns["high_ticks"].append(bar.high_ticks)
            columns["low_ticks"].append(bar.low_ticks)
            columns["close_ticks"].append(bar.close_ticks)
            columns["volume"].append(bar.volume)
            columns["tick_size"].append(str(bar.tick_size))
            columns["data_label"].append(bar.data_label.value)
            columns["provider"].append(bar.provider)
            columns["dataset"].append(bar.dataset)
            columns["provider_raw_symbol"].append(bar.provider_raw_symbol)
            columns["provider_instrument_id"].append(bar.provider_instrument_id)
            columns["schema_version"].append(bar.schema_version)
        # Phase 3.3 §14 fix: pa.array()/pa.Table.from_arrays() are the
        # actual third-party PyArrow conversion calls -- an expected
        # Arrow representation/conversion failure here (e.g. a value
        # that cannot be represented by the declared column type) must
        # become a HistoricalStorageError, never a raw pyarrow/Arrow
        # exception leaking through this public boundary. Scoped
        # NARROWLY to just these two calls, not the column-gathering
        # loop above (an Olive bug there must still propagate
        # unmodified).
        try:
            arrays = [pa.array(columns[f.name], type=f.type) for f in schema]
            return pa.Table.from_arrays(arrays, schema=schema)
        except HistoricalStorageError:
            raise
        except Exception as exc:
            raise HistoricalStorageError(f"Failed to convert bars to an Arrow table: {exc}") from exc

    def _table_to_bars(self, table) -> list[HistoricalBar]:
        bars: list[HistoricalBar] = []
        for row in table.to_pylist():
            # Phase 3.1 §24 fix: a schema version this storage layer
            # does not know how to interpret must fail closed, not be
            # silently accepted as if any positive int were fine.
            schema_version = row["schema_version"]
            if schema_version not in _SUPPORTED_BAR_SCHEMA_VERSIONS:
                raise HistoricalStorageIntegrityError(
                    f"Stored bar has schema_version={schema_version!r}, which this build of "
                    f"Olive does not support (supported: {sorted(_SUPPORTED_BAR_SCHEMA_VERSIONS)}). "
                    f"Refusing to read data written by an incompatible schema version."
                )
            bars.append(
                HistoricalBar(
                    root_symbol=row["root_symbol"],
                    contract_year=row["contract_year"],
                    contract_month=row["contract_month"],
                    timeframe=HistoricalTimeframe(row["timeframe"]),
                    ts_event=_normalize_stored_timestamp(row["ts_event"]),
                    open_ticks=row["open_ticks"],
                    high_ticks=row["high_ticks"],
                    low_ticks=row["low_ticks"],
                    close_ticks=row["close_ticks"],
                    volume=row["volume"],
                    tick_size=Decimal(row["tick_size"]),
                    data_label=DataLabel(row["data_label"]),
                    provider=row["provider"],
                    dataset=row["dataset"],
                    provider_raw_symbol=row["provider_raw_symbol"],
                    provider_instrument_id=row["provider_instrument_id"],
                    schema_version=schema_version,
                )
            )
        return bars

    def _read_partition_file(self, path: Path, pa, pq) -> list[HistoricalBar]:
        try:
            table = pq.read_table(path)
        except Exception as exc:
            raise HistoricalStorageError(f"Failed to read Parquet file {path}: {exc}") from exc
        # Phase 3.1 §25 fix: _table_to_bars previously ran OUTSIDE this
        # try/except -- a malformed Parquet file that reads fine as a
        # raw Arrow table but is missing an expected column (a raw
        # KeyError), has a value of the wrong type for a column (a raw
        # TypeError/ValueError/decimal.InvalidOperation), or otherwise
        # cannot be converted to valid HistoricalBar objects must
        # become a HistoricalStorageError too -- never leak a raw
        # builtin exception from this public boundary. A failure that
        # is already one of Olive's own storage errors (e.g. the
        # schema-version or naive-timestamp checks above) is passed
        # through unchanged rather than being re-wrapped.
        try:
            return self._table_to_bars(table)
        except HistoricalStorageError:
            raise
        except Exception as exc:
            raise HistoricalStorageError(
                f"Parquet file {path} could not be decoded into valid historical bars "
                f"(malformed or corrupt data): {exc}"
            ) from exc

    # -- atomic write helpers ----------------------------------------------

    @staticmethod
    def _atomic_write_bytes(final_path: Path, content: bytes) -> None:
        # Phase 3.2 §14/§28 fix: an expected local-filesystem failure
        # (disk full, permission denied, a missing/removed parent
        # directory, ...) is an OSError (or a subclass) -- it must
        # become a HistoricalStorageError at THIS boundary, exactly
        # like the Databento adapter never lets a raw vendor exception
        # escape its boundary. A non-OSError exception here would be a
        # genuine programming bug and is deliberately left to
        # propagate unmodified (narrow translation, never a broad
        # `except Exception`).
        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HistoricalStorageError(
                f"Could not create directory {final_path.parent}: {exc}"
            ) from exc
        tmp_path = final_path.parent / f".{final_path.name}.tmp-{uuid.uuid4().hex}"
        try:
            with open(tmp_path, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, final_path)
        except OSError as exc:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise HistoricalStorageError(f"Local filesystem write to {final_path} failed: {exc}") from exc
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def _write_partition_file(
        self,
        partition_dir: Path,
        merged_bars: Sequence[HistoricalBar],
        pa,
        pq,
        *,
        partition_year: int,
        partition_month: int,
        requested_start: Optional[datetime],
        requested_end: Optional[datetime],
        estimated_cost_usd: Optional[Decimal],
        contract_multiplier: Optional[Decimal],
    ) -> None:
        import io

        table = self._bars_to_table(merged_bars, pa)
        buffer = io.BytesIO()
        # Phase 3.2 §14/§28 fix: an expected pyarrow serialization
        # failure must become a HistoricalStorageError too -- never a
        # raw exception from a third-party library leaking through
        # this public boundary. This wraps ONLY the actual
        # third-party serialization call, not Olive's own
        # `_bars_to_table` construction above it, so a genuine Olive
        # programming bug there still propagates unmodified.
        try:
            pq.write_table(table, buffer)
        except Exception as exc:
            raise HistoricalStorageError(f"Failed to serialize bars to Parquet: {exc}") from exc
        parquet_bytes = buffer.getvalue()
        checksum = hashlib.sha256(parquet_bytes).hexdigest()

        final_parquet_path = partition_dir / _PARQUET_FILENAME
        final_manifest_path = partition_dir / _MANIFEST_FILENAME

        # Phase 3.1 §30 fix: the bars.parquet + manifest.json pair is
        # NOT atomic as two separate writes -- a failure between them
        # (e.g. the manifest write failing after the parquet write
        # already succeeded) previously left a NEW parquet file paired
        # with a STALE or missing manifest. Capture the previous valid
        # pair's bytes BEFORE touching anything, so a failure writing
        # the manifest can roll the parquet file back to its previous
        # state (or remove it, if there was no previous pair at all),
        # guaranteeing this partition is left with either the complete
        # previous valid pair or the complete new valid pair -- never
        # a mismatched mix of the two.
        previous_parquet_bytes = final_parquet_path.read_bytes() if final_parquet_path.exists() else None

        first_bar = merged_bars[0]
        last_bar = merged_bars[-1]
        manifest = {
            "schema_version": first_bar.schema_version,
            "olive_contract_identity": first_bar.contract_identity,
            "provider_raw_symbol": first_bar.provider_raw_symbol,
            "root_symbol": first_bar.root_symbol,
            "contract_year": first_bar.contract_year,
            "contract_month": first_bar.contract_month,
            "provider": first_bar.provider,
            "dataset": first_bar.dataset,
            "timeframe": first_bar.timeframe.value,
            # Phase 3.3 §7 fix: the partition's OWN directory identity
            # (year=YYYY/month=MM) is recorded explicitly, decoupled
            # from any single bar's ts_event -- this is the partition
            # DIRECTORY's claimed identity, cross-checked separately
            # (§6/§8) against what the bars themselves actually
            # contain. No migration compatibility is required for
            # these new fields (Phase 3 has no committed/released
            # production dataset yet).
            "partition_year": partition_year,
            "partition_month": partition_month,
            "requested_start_utc": requested_start.isoformat() if requested_start is not None else None,
            "requested_end_utc": requested_end.isoformat() if requested_end is not None else None,
            "actual_first_timestamp_utc": first_bar.ts_event.isoformat(),
            "actual_last_timestamp_utc": last_bar.ts_event.isoformat(),
            "record_count": len(merged_bars),
            "data_label": first_bar.data_label.value,
            "last_written_at_utc": datetime.now(timezone.utc).isoformat(),
            "tick_size": str(first_bar.tick_size),
            "contract_multiplier": str(contract_multiplier) if contract_multiplier is not None else None,
            "estimated_cost_usd": str(estimated_cost_usd) if estimated_cost_usd is not None else None,
            "storage_format": "parquet",
            "checksum_sha256": checksum,
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

        self._atomic_write_bytes(final_parquet_path, parquet_bytes)
        try:
            self._atomic_write_bytes(final_manifest_path, manifest_bytes)
        except Exception as manifest_exc:
            # Roll the parquet file back to the previous valid pair's
            # state (or remove it if there was none) so this partition
            # never ends up with a NEW parquet file paired with a
            # STALE or missing manifest.
            #
            # Phase 3.2 §20 fix: a rollback failure here must NOT be
            # silently swallowed (the previous `except Exception: pass`
            # could leave this partition with a NEW parquet file and a
            # STALE/missing manifest, while reporting only the
            # original manifest-write failure as if storage were
            # otherwise consistent). When rollback itself also fails,
            # raise a dedicated integrity error that preserves BOTH the
            # original failure and the rollback failure, telling the
            # caller this partition may now need operator intervention
            # -- never masking either failure.
            try:
                if previous_parquet_bytes is not None:
                    self._atomic_write_bytes(final_parquet_path, previous_parquet_bytes)
                else:
                    final_parquet_path.unlink(missing_ok=True)
            except Exception as rollback_exc:
                raise HistoricalStorageIntegrityError(
                    f"Partition {partition_dir}: manifest write failed AND rollback of its "
                    f"parquet file also failed; this partition may now have a mismatched or "
                    f"corrupt parquet+manifest pair and requires operator intervention. "
                    f"Original manifest-write failure: {manifest_exc}. Rollback failure: "
                    f"{rollback_exc}"
                ) from manifest_exc
            raise

    @staticmethod
    def _canonicalize_bars(bars: Sequence[HistoricalBar], *, context: str) -> list[HistoricalBar]:
        """Phase 3.3 §2/§4/§15 (supersedes Phase 3.2 §18's
        validate-only ``_check_no_internal_conflicts``): deduplicate
        bars sharing a canonical key down to ONE record when they are
        IDENTICAL, raising :class:`HistoricalStorageIntegrityError`
        when they CONFLICT -- never silently picking a winner.

        This is the single, explicit policy for "what happens when the
        same canonical key appears more than once" used everywhere
        that question can arise: an existing partition's bars before
        they are trusted as write-side MERGE input (so pre-existing
        identical duplicates never corrupt the write's own new/total
        record accounting -- see ``write_bars``), one partition's own
        decoded bars before ``read_bars`` returns them, and the final
        cross-partition result of ``read_bars`` as defense in depth.
        Read and write deliberately never disagree on this policy.
        Preserves first-occurrence order."""
        seen: dict[tuple, HistoricalBar] = {}
        order: list[tuple] = []
        for bar in bars:
            key = bar.canonical_key
            existing = seen.get(key)
            if existing is None:
                seen[key] = bar
                order.append(key)
            elif existing.conflicts_with(bar):
                raise HistoricalStorageIntegrityError(
                    f"{context} contains CONFLICTING bars for canonical key {key} (OHLCV="
                    f"({existing.open}, {existing.high}, {existing.low}, {existing.close}, "
                    f"vol={existing.volume}) vs. ({bar.open}, {bar.high}, {bar.low}, "
                    f"{bar.close}, vol={bar.volume})); this indicates storage corruption. "
                    f"Refusing to silently pick one."
                )
            # else: identical duplicate -- tolerated, canonicalized away.
        return [seen[key] for key in order]

    @staticmethod
    def _check_bars_match_partition_period(
        bars: Sequence[HistoricalBar], partition_dir: Path, *, year: int, month: int
    ) -> None:
        """Phase 3.3 §6 (critical): a partition's ``year=YYYY/month=MM``
        directory is part of its canonical storage identity, exactly
        like the contract/timeframe/provider/dataset identity already
        checked elsewhere. Independent reproduction copied a whole
        valid parquet+manifest pair from a September partition into an
        October directory: the manifest and checksum both verified
        (the pair really was internally consistent), and the misplaced
        bar was merely FILTERED OUT by read_bars's own timestamp-range
        filter -- silently treating corruption as an empty result
        rather than failing closed. Every bar trusted from (or merged
        into) a partition must have its OWN ``ts_event`` fall in that
        exact calendar year/month, or this partition is misplaced or
        corrupted and must never be trusted."""
        for bar in bars:
            if bar.ts_event.year != year or bar.ts_event.month != month:
                raise HistoricalStorageIntegrityError(
                    f"Partition {partition_dir} (year={year:04d}, month={month:02d}) contains "
                    f"a bar with ts_event {bar.ts_event.isoformat()}, whose own calendar "
                    f"year/month does not match this partition directory; refusing to trust "
                    f"what appears to be a misplaced or corrupted partition."
                )

    @staticmethod
    def _check_partition_coherence(bars: Sequence[HistoricalBar], partition_dir: Path) -> None:
        """Phase 3.3 §9/§10: one partition must represent ONE coherent
        market series. The write-side grouping key (provider, dataset,
        root_symbol, contract_identity, timeframe, year, month) already
        guarantees new bars written together in one ``write_bars`` call
        agree on those fields -- but ``tick_size``,
        ``provider_raw_symbol``, ``provider_instrument_id``,
        ``data_label``, and ``schema_version`` are NOT part of that
        key. Independent reproduction successfully wrote (and later
        read back) two individually well-formed bars into the same
        partition with different ``tick_size``/``provider_raw_symbol``/
        ``provider_instrument_id`` -- each bar was fine on its own, but
        the PARTITION was internally incoherent. Enforced on every
        WRITE (before any commit -- see ``write_bars``) and on every
        READ (before any bar from an existing partition is trusted or
        returned -- see ``read_bars``); storage is a trust boundary in
        both directions."""
        if not bars:
            return
        first = bars[0]
        for bar in bars[1:]:
            mismatched = [
                field_name
                for field_name, a, b in (
                    ("tick_size", first.tick_size, bar.tick_size),
                    ("provider_raw_symbol", first.provider_raw_symbol, bar.provider_raw_symbol),
                    ("provider_instrument_id", first.provider_instrument_id, bar.provider_instrument_id),
                    ("data_label", first.data_label, bar.data_label),
                    ("schema_version", first.schema_version, bar.schema_version),
                )
                if a != b
            ]
            if mismatched:
                raise HistoricalStorageIntegrityError(
                    f"Partition {partition_dir} contains internally INCOHERENT bars (mismatched "
                    f"{', '.join(mismatched)} between ts_event={first.ts_event.isoformat()} and "
                    f"ts_event={bar.ts_event.isoformat()}); one partition must represent one "
                    f"coherent market series. This indicates storage corruption or a bug that "
                    f"allowed mixed-provenance/economics bars into the same partition."
                )

    @staticmethod
    def _verify_manifest_content_matches_bars(
        manifest: dict, bars: Sequence[HistoricalBar], partition_dir: Path
    ) -> None:
        """Phase 3.3 §8: a manifest that is written with rich
        descriptive fields (``record_count``, first/last timestamp,
        ``provider_raw_symbol``, ``tick_size``, ``data_label``,
        ``schema_version``, ``storage_format``) but never subsequently
        CROSS-CHECKED against the content actually decoded from its
        own partition is not a real integrity guarantee for those
        fields -- only the raw-bytes checksum was ever verified
        (Phase 3.1 §28). Called only after this partition's bars have
        already passed :meth:`_check_partition_coherence`, so a single
        ``bars[0]`` sample is representative of the whole partition for
        every per-bar field being compared."""
        if len(bars) != manifest.get("record_count"):
            raise HistoricalStorageIntegrityError(
                f"Partition {partition_dir} manifest declares record_count="
                f"{manifest.get('record_count')!r}, but {len(bars)} bar(s) were actually "
                f"decoded from its data file; refusing to trust a manifest that disagrees "
                f"with its own partition's content."
            )
        if not bars:
            return
        first_ts = min(bar.ts_event for bar in bars)
        last_ts = max(bar.ts_event for bar in bars)
        sample = bars[0]
        expected_fields = {
            "actual_first_timestamp_utc": first_ts.isoformat(),
            "actual_last_timestamp_utc": last_ts.isoformat(),
            "provider_raw_symbol": sample.provider_raw_symbol,
            "tick_size": str(sample.tick_size),
            "data_label": sample.data_label.value,
            "schema_version": sample.schema_version,
            "storage_format": "parquet",
        }
        for field_name, expected_value in expected_fields.items():
            if manifest.get(field_name) != expected_value:
                raise HistoricalStorageIntegrityError(
                    f"Partition {partition_dir} manifest field {field_name!r}="
                    f"{manifest.get(field_name)!r} does not match this partition's actually "
                    f"decoded content ({expected_value!r}); refusing to trust a manifest that "
                    f"disagrees with its own partition's content."
                )

    @staticmethod
    def _merge_bars(existing: Sequence[HistoricalBar], new: Sequence[HistoricalBar]) -> tuple[HistoricalBar, ...]:
        by_key: dict[tuple, HistoricalBar] = {}
        for bar in existing:
            by_key[bar.canonical_key] = bar
        for bar in new:
            key = bar.canonical_key
            current = by_key.get(key)
            if current is None:
                by_key[key] = bar
            elif current.conflicts_with(bar):
                raise HistoricalStorageIntegrityError(
                    f"Conflicting bar for canonical key {key}: existing OHLCV="
                    f"({current.open}, {current.high}, {current.low}, {current.close}, "
                    f"vol={current.volume}) vs. incoming OHLCV=({bar.open}, {bar.high}, "
                    f"{bar.low}, {bar.close}, vol={bar.volume}). Refusing to silently "
                    f"overwrite conflicting history."
                )
            # else: identical duplicate -- keep the existing record.
        return tuple(sorted(by_key.values(), key=lambda b: b.ts_event))

    @staticmethod
    def _group_by_partition(bars: Sequence[HistoricalBar]) -> dict[tuple, list[HistoricalBar]]:
        groups: dict[tuple, list[HistoricalBar]] = {}
        for bar in bars:
            key = (
                bar.provider,
                bar.dataset,
                bar.root_symbol,
                bar.contract_identity,
                bar.timeframe,
                bar.ts_event.year,
                bar.ts_event.month,
            )
            groups.setdefault(key, []).append(bar)
        return groups

    # -- public API --------------------------------------------------------

    def write_bars(
        self,
        bars: Sequence[HistoricalBar],
        *,
        requested_start: Optional[datetime] = None,
        requested_end: Optional[datetime] = None,
        estimated_cost_usd: Optional[Decimal] = None,
        contract_multiplier: Optional[Decimal] = None,
    ) -> HistoricalWriteResult:
        """Durably, idempotently, atomically persist ``bars``.

        Safe to call repeatedly with the same or overlapping data:
        identical duplicates are merged to one record; conflicting
        data for the same canonical key raises
        :class:`~app.data.models.HistoricalStorageIntegrityError`
        BEFORE any partition in this call is written (a conflict
        found while planning partition 2 of 3 leaves partition 1's
        existing file untouched, even though it would otherwise have
        been fine to write).
        """
        # Phase 3.1 §19 fix: `tuple(bars)` on a non-iterable caller
        # value (None, an int, a bool, ...) raises a raw TypeError --
        # never let that escape this public boundary.
        if not isinstance(bars, (list, tuple)):
            try:
                bars = tuple(bars)
            except TypeError as exc:
                raise HistoricalStorageError(
                    f"HistoricalBarStore.write_bars expects an iterable of HistoricalBar, got "
                    f"{type(bars).__name__} ({bars!r}), which is not iterable."
                ) from exc
        for bar in bars:
            if not isinstance(bar, HistoricalBar):
                raise HistoricalStorageError(
                    f"HistoricalBarStore.write_bars expects every element to be a HistoricalBar, "
                    f"got {type(bar).__name__}"
                )
            # Phase 3.1 §24 fix (write side): never durably persist a
            # bar whose schema_version this storage layer does not
            # know how to read back.
            if bar.schema_version not in _SUPPORTED_BAR_SCHEMA_VERSIONS:
                raise HistoricalStorageError(
                    f"HistoricalBarStore.write_bars refuses to store a bar with unsupported "
                    f"schema_version={bar.schema_version!r} (supported: "
                    f"{sorted(_SUPPORTED_BAR_SCHEMA_VERSIONS)})."
                )

        # Phase 3.1 §21 fix: validate the metadata that will be written
        # into every affected partition's manifest BEFORE any write
        # happens -- a malformed requested_start/requested_end/
        # estimated_cost_usd/contract_multiplier must never reach
        # `.isoformat()`/`str(...)` deep inside `_write_partition_file`
        # (where a raw AttributeError/TypeError could leak, or a
        # silently-wrong value could be written into a durable
        # manifest).
        if requested_start is not None:
            requested_start = _require_aware_datetime(requested_start, field_name="requested_start").astimezone(
                timezone.utc
            )
        if requested_end is not None:
            requested_end = _require_aware_datetime(requested_end, field_name="requested_end").astimezone(
                timezone.utc
            )
        if requested_start is not None and requested_end is not None and not requested_start < requested_end:
            raise HistoricalStorageError(
                f"write_bars requires requested_start < requested_end; got "
                f"start={requested_start.isoformat()}, end={requested_end.isoformat()}"
            )
        if estimated_cost_usd is not None:
            estimated_cost_usd = _require_storage_decimal(
                estimated_cost_usd, field_name="estimated_cost_usd", allow_zero=True
            )
        if contract_multiplier is not None:
            contract_multiplier = _require_storage_decimal(
                contract_multiplier, field_name="contract_multiplier", allow_zero=False
            )

        if not bars:
            return HistoricalWriteResult(partitions_written=0, new_records=0, total_records=0)

        pa, pq = _import_pyarrow()
        groups = self._group_by_partition(bars)

        # Phase 1: plan every partition (read existing + canonicalize +
        # verify + merge + conflict check) WITHOUT writing anything
        # yet. Phase 3.3 §2/§3 fix: every quantity
        # HistoricalWriteResult's own __post_init__ could possibly
        # reject (partitions_written/new_records/total_records, and
        # their cross-field relationships) is computed HERE, entirely
        # from this read-only planning pass -- never incrementally
        # during the commit loop below. This makes it mechanically
        # impossible for anything to fail AFTER a successful commit:
        # phase 2 only performs I/O and never computes a value that
        # feeds HistoricalWriteResult's construction.
        plans: dict[tuple, tuple[Path, tuple[HistoricalBar, ...], int, int]] = {}
        for key, new_bars in groups.items():
            provider, dataset, root_symbol, contract_identity, timeframe, year, month = key
            partition_dir = self._partition_dir(
                provider=provider,
                dataset=dataset,
                root_symbol=root_symbol,
                contract_identity=contract_identity,
                schema_segment=timeframe.databento_schema,
                year=year,
                month=month,
            )
            existing_path = partition_dir / _PARQUET_FILENAME
            existing_manifest_path = partition_dir / _MANIFEST_FILENAME

            # Phase 3.2 §17 fix: write_bars previously read an existing
            # partition's bars.parquet directly, WITHOUT ever calling
            # _verify_partition_manifest -- the same integrity gate
            # read_bars already enforces. That let a later write
            # "legitimize" previously unverified or corrupt data: a
            # probe could drop a bars.parquet with NO manifest.json (or
            # a tampered one) into a partition directory, and the next
            # write_bars call would happily read it, merge it, and
            # produce a brand-new TRUSTED manifest over it. Every read
            # of persisted data used as trusted MERGE input must pass
            # the exact same integrity gate as a public read_bars call.
            if existing_path.exists():
                first_new_bar = new_bars[0]
                if not existing_manifest_path.exists():
                    raise HistoricalStorageIntegrityError(
                        f"Partition {partition_dir} has a {_PARQUET_FILENAME} file but no "
                        f"{_MANIFEST_FILENAME}; refusing to merge new data onto unverified, "
                        f"orphaned existing data. This requires operator intervention -- "
                        f"Olive never silently repairs or overwrites an orphaned partition."
                    )
                existing_manifest = self._verify_partition_manifest(
                    partition_dir,
                    existing_path,
                    expected_provider=provider,
                    expected_dataset=dataset,
                    expected_root_symbol=root_symbol,
                    expected_contract_identity=contract_identity,
                    expected_contract_year=first_new_bar.contract_year,
                    expected_contract_month=first_new_bar.contract_month,
                    expected_timeframe=timeframe,
                    expected_partition_year=year,
                    expected_partition_month=month,
                )
                existing_bars_raw = self._read_partition_file(existing_path, pa, pq)
                # Phase 3.3 §6/§8/§9/§10/§18 fix: every content-level
                # check below runs against the RAW decoded rows, BEFORE
                # canonicalization -- a pre-existing IDENTICAL
                # duplicate (tolerated storage state per §2/§4) is, by
                # definition, two rows that agree on every one of these
                # checks, so running them pre-canonicalization changes
                # nothing for that legitimate case. It matters for what
                # it does NOT do: it keeps record_count (§8) meaning
                # "how many rows are actually in this file" (what the
                # manifest was written to describe), never silently
                # redefined as "how many UNIQUE canonical keys," which
                # would make a tolerated pre-existing duplicate look
                # like manifest/content disagreement.
                #
                # Defense in depth, mirroring read_bars's per-bar
                # identity cross-check: a partition's manifest can
                # verify, yet an individual stored bar could still
                # (through corruption) identify as a different
                # contract/timeframe/provider/dataset than this
                # partition's own path implies.
                for bar in existing_bars_raw:
                    if (
                        bar.root_symbol != root_symbol
                        or bar.contract_identity != contract_identity
                        or bar.timeframe is not timeframe
                        or bar.provider != provider
                        or bar.dataset != dataset
                    ):
                        raise HistoricalStorageIntegrityError(
                            f"Partition {partition_dir} contains a stored bar identified as "
                            f"{bar.contract_identity}/{bar.timeframe.value}/{bar.provider}/"
                            f"{bar.dataset}, which does not match this partition's own "
                            f"{contract_identity}/{timeframe.value}/{provider}/{dataset}. "
                            f"Refusing to merge onto what appears to be a misplaced or "
                            f"corrupted file."
                        )
                # Phase 3.3 §6 fix (critical): every existing stored
                # bar's OWN ts_event must fall within THIS partition's
                # year/month -- never trusted as merge input otherwise
                # (a whole valid parquet+manifest pair copied from a
                # different month's directory would otherwise pass
                # every check above and be silently merged onto).
                self._check_bars_match_partition_period(existing_bars_raw, partition_dir, year=year, month=month)
                # Phase 3.3 §9/§10 fix: the existing partition's own
                # stored bars must already be internally coherent (one
                # market series) before anything is merged onto them.
                self._check_partition_coherence(existing_bars_raw, partition_dir)
                # Phase 3.3 §8 fix: the manifest's own descriptive
                # claims must agree with what was actually decoded.
                self._verify_manifest_content_matches_bars(existing_manifest, existing_bars_raw, partition_dir)
                # Phase 3.3 §2/§4 fix (supersedes Phase 3.2 §18's
                # validate-only check): ONLY NOW, after every raw-
                # content check above has passed, canonicalize the
                # existing bars -- deduplicating identical duplicates
                # down to ONE record, raising on genuinely CONFLICTING
                # ones. Canonicalizing here, before existing_count is
                # computed below, is exactly what makes new_records/
                # total_records mathematically incapable of going
                # negative: a duplicate-only existing partition (e.g.
                # two identical stored copies of one bar, itself an
                # already-tolerated storage state) must count as ONE
                # existing record, never two, or merging the SAME
                # incoming bar again would compute
                # len(merged) - existing_count == 1 - 2 == -1.
                existing_bars = self._canonicalize_bars(
                    existing_bars_raw, context=f"Partition {partition_dir}"
                )
            elif existing_manifest_path.exists():
                raise HistoricalStorageIntegrityError(
                    f"Partition {partition_dir} has a {_MANIFEST_FILENAME} but no "
                    f"{_PARQUET_FILENAME}; refusing to write onto this unverified/corrupt "
                    f"partition state. This requires operator intervention."
                )
            else:
                existing_bars = []

            merged = self._merge_bars(existing_bars, new_bars)
            # Phase 3.3 §9/§10 fix: re-check coherence on the MERGED
            # result too -- new_bars already agree with each other and
            # with existing_bars on the partition's grouping-key fields
            # (enforced by construction via _group_by_partition), but
            # tick_size/provider_raw_symbol/provider_instrument_id/
            # data_label/schema_version are not part of that key, so an
            # incoherent WRITE must still be caught here, during
            # planning, before any partition in this call is written.
            self._check_partition_coherence(merged, partition_dir)
            existing_unique_count = len(existing_bars)
            plans[key] = (partition_dir, merged, existing_unique_count, year, month)

        # Every quantity HistoricalWriteResult needs is already fully
        # determined from the read-only planning pass above.
        # len(merged) >= existing_unique_count always holds (merging
        # can only ever ADD canonical keys relative to the
        # already-canonicalized existing set, never remove one), so
        # total_new is mathematically guaranteed non-negative and
        # new_records can never exceed total_records; each planned
        # partition's `merged` is non-empty (it contains at least the
        # new bar(s) that put it in `groups`), so partitions_written
        # can never exceed total_records either. HistoricalWriteResult
        # construction below is therefore guaranteed to succeed.
        partitions_written = len(plans)
        total_new = sum(len(merged) - existing_count for _, merged, existing_count, _, _ in plans.values())
        total_records = sum(len(merged) for _, merged, _, _, _ in plans.values())

        # Phase 2: every partition is conflict-free and coherent -- now
        # actually write. This phase performs I/O ONLY; it computes
        # nothing that feeds HistoricalWriteResult's construction (see
        # above), so nothing capable of raising happens between the
        # last successful commit and the structured result being
        # returned. Phase 3.1 §31 fix: a plain per-partition loop left
        # partition 1 already updated on disk if an I/O error occurred
        # while COMMITTING a later partition's write (the two-phase
        # planner above only protects against a data CONFLICT doing
        # that -- it says nothing about an I/O failure during the
        # write itself). Record each partition's pre-call state as we
        # go, and if any partition's write raises, roll back EVERY
        # partition already written during this same call to its
        # pre-call state before re-raising, so one `write_bars` call
        # is never left half-committed across partitions.
        written_so_far: list[tuple[Path, Optional[bytes], Optional[bytes]]] = []
        try:
            for partition_dir, merged, _existing_count, partition_year, partition_month in plans.values():
                parquet_path = partition_dir / _PARQUET_FILENAME
                manifest_path = partition_dir / _MANIFEST_FILENAME
                pre_call_parquet_bytes = parquet_path.read_bytes() if parquet_path.exists() else None
                pre_call_manifest_bytes = manifest_path.read_bytes() if manifest_path.exists() else None

                self._write_partition_file(
                    partition_dir,
                    merged,
                    pa,
                    pq,
                    partition_year=partition_year,
                    partition_month=partition_month,
                    requested_start=requested_start,
                    requested_end=requested_end,
                    estimated_cost_usd=estimated_cost_usd,
                    contract_multiplier=contract_multiplier,
                )
                written_so_far.append((partition_dir, pre_call_parquet_bytes, pre_call_manifest_bytes))
        except Exception as original_exc:
            # Phase 3.2 §20 fix: a rollback failure during this
            # cross-partition recovery must not be silently ignored
            # either -- collect every rollback error instead of
            # swallowing it, and if any partition's rollback failed,
            # raise a dedicated integrity error chaining BOTH the
            # original write failure and every rollback failure so the
            # caller knows storage may now be inconsistent across
            # partitions and needs operator attention, rather than
            # believing the operation "failed cleanly."
            rollback_errors: list[str] = []
            for partition_dir, pre_call_parquet_bytes, pre_call_manifest_bytes in reversed(written_so_far):
                rollback_errors.extend(
                    self._rollback_partition_to(partition_dir, pre_call_parquet_bytes, pre_call_manifest_bytes)
                )
            if rollback_errors:
                raise HistoricalStorageIntegrityError(
                    "write_bars failed AND rollback of previously-committed partitions in "
                    "this same call also failed; storage may now be inconsistent across "
                    f"partitions and requires operator intervention. Original failure: "
                    f"{original_exc}. Rollback failures: {'; '.join(rollback_errors)}"
                ) from original_exc
            raise

        return HistoricalWriteResult(
            partitions_written=partitions_written, new_records=total_new, total_records=total_records
        )

    @staticmethod
    def _rollback_partition_to(
        partition_dir: Path,
        parquet_bytes: Optional[bytes],
        manifest_bytes: Optional[bytes],
    ) -> list[str]:
        """Best-effort restore of one partition's bars.parquet/
        manifest.json to a captured prior state (``None`` meaning "did
        not exist"), used by :meth:`write_bars`'s cross-partition
        rollback (Phase 3.1 §31). Deliberately never RAISES itself --
        a rollback failure must not mask the original write failure
        that triggered it -- but, as of Phase 3.2 §20, it no longer
        SILENTLY swallows a rollback failure either: it returns a list
        of human-readable descriptions of whichever half (parquet/
        manifest) could not be restored, empty when both succeeded, so
        the caller can surface a rollback failure rather than lose it."""
        parquet_path = partition_dir / _PARQUET_FILENAME
        manifest_path = partition_dir / _MANIFEST_FILENAME
        errors: list[str] = []
        try:
            if parquet_bytes is not None:
                HistoricalBarStore._atomic_write_bytes(parquet_path, parquet_bytes)
            else:
                parquet_path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"{parquet_path}: {exc}")
        try:
            if manifest_bytes is not None:
                HistoricalBarStore._atomic_write_bytes(manifest_path, manifest_bytes)
            else:
                manifest_path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"{manifest_path}: {exc}")
        return errors

    def read_bars(
        self,
        *,
        root_symbol: str,
        contract_year: int,
        contract_month: int,
        timeframe: object,
        start: datetime,
        end: datetime,
        provider: str = "databento",
        dataset: str = "GLBX.MDP3",
    ) -> HistoricalReadResult:
        """Read back stored bars for one contract/timeframe over a
        half-open ``[start, end)`` UTC interval.

        Never requires a provider, an API key, or network access --
        this is a purely local filesystem operation. See
        :class:`HistoricalReadResult` for how a missing storage
        path/data is represented explicitly, never ambiguously.
        """
        timeframe = _coerce_timeframe(timeframe)
        start_aware = _require_aware_datetime(start, field_name="start").astimezone(timezone.utc)
        end_aware = _require_aware_datetime(end, field_name="end").astimezone(timezone.utc)
        if not start_aware < end_aware:
            raise HistoricalStorageError(
                f"read_bars requires start < end; got start={start_aware.isoformat()}, "
                f"end={end_aware.isoformat()}"
            )
        if not isinstance(root_symbol, str) or not root_symbol.strip():
            raise HistoricalStorageError(f"read_bars root_symbol must be a non-empty string, got {root_symbol!r}")
        if not isinstance(provider, str) or not provider.strip():
            raise HistoricalStorageError(f"read_bars provider must be a non-empty string, got {provider!r}")
        if not isinstance(dataset, str) or not dataset.strip():
            raise HistoricalStorageError(f"read_bars dataset must be a non-empty string, got {dataset!r}")

        # Phase 3.1 §20 fix: `int(True)` silently coercing to 1,
        # `int(3.5)` silently truncating to 3, and `int("12")` silently
        # accepting a numeric string must never be mistaken for a real
        # caller-supplied contract_year/contract_month -- require true
        # (non-bool) ints, and require contract_month to be one of
        # Olive's quarterly months exactly, never any coercible value.
        contract_year = _require_storage_int(contract_year, field_name="contract_year")
        contract_month = _require_storage_int(contract_month, field_name="contract_month")
        if not (1900 <= contract_year <= 2300):
            raise HistoricalStorageError(f"read_bars contract_year out of reasonable range: {contract_year}")
        if contract_month not in (3, 6, 9, 12):
            raise HistoricalStorageError(
                f"read_bars contract_month must be one of Olive's quarterly months (3, 6, 9, "
                f"12); got {contract_month}"
            )

        root_symbol_upper = root_symbol.strip().upper()
        contract_identity = f"{root_symbol_upper}-{contract_year:04d}-{contract_month:02d}"

        pa, pq = _import_pyarrow()
        partitions_found = 0
        collected: list[HistoricalBar] = []
        for year, month in _months_between(start_aware, end_aware):
            partition_dir = self._partition_dir(
                provider=provider,
                dataset=dataset,
                root_symbol=root_symbol_upper,
                contract_identity=contract_identity,
                schema_segment=timeframe.databento_schema,
                year=year,
                month=month,
            )
            path = partition_dir / _PARQUET_FILENAME
            manifest_path = partition_dir / _MANIFEST_FILENAME
            if not path.exists():
                # Phase 3.3 §5 fix: a manifest with NO accompanying
                # parquet file is NOT a legitimate "nothing was ever
                # written here" state -- it is an orphaned/corrupt
                # partition, and must be reported as such rather than
                # silently treated as if this partition had never been
                # written at all.
                if manifest_path.exists():
                    raise HistoricalStorageIntegrityError(
                        f"Partition {partition_dir} has a {_MANIFEST_FILENAME} but no "
                        f"{_PARQUET_FILENAME}; refusing to treat this as a legitimate missing "
                        f"partition. This requires operator intervention."
                    )
                continue

            # Phase 3.1 §28 fix: verify the manifest exists, its schema
            # version is supported, its recorded identity (including,
            # as of Phase 3.3 §6/§7, its own claimed partition
            # year/month) matches the partition we are about to trust,
            # and its checksum matches bars.parquet's ACTUAL bytes on
            # disk -- a manifest that was written but never
            # subsequently verified on read is not a real safety
            # guarantee at all.
            manifest = self._verify_partition_manifest(
                partition_dir,
                path,
                expected_provider=provider,
                expected_dataset=dataset,
                expected_root_symbol=root_symbol_upper,
                expected_contract_identity=contract_identity,
                expected_contract_year=contract_year,
                expected_contract_month=contract_month,
                expected_timeframe=timeframe,
                expected_partition_year=year,
                expected_partition_month=month,
            )

            partitions_found += 1
            bars_in_partition_raw = self._read_partition_file(path, pa, pq)

            # Phase 3.1 §27 fix: even with a verified manifest, defend
            # in depth against a misplaced/corrupted file by checking
            # every individual loaded bar's OWN identity fields against
            # the partition we requested -- a bar silently belonging
            # to a different contract/timeframe/provider/dataset must
            # never be returned as if it were the requested data.
            # Phase 3.3 §6/§8/§9/§10 fix: this and every content-level
            # check below run against the RAW decoded rows, BEFORE
            # canonicalization -- see the matching comment in
            # write_bars for why (keeps record_count/§8 meaning "rows
            # actually in this file," so a tolerated pre-existing
            # IDENTICAL duplicate is never mistaken for manifest/content
            # disagreement).
            for bar in bars_in_partition_raw:
                if (
                    bar.root_symbol != root_symbol_upper
                    or bar.contract_year != contract_year
                    or bar.contract_month != contract_month
                    or bar.timeframe is not timeframe
                    or bar.provider != provider
                    or bar.dataset != dataset
                ):
                    raise HistoricalStorageIntegrityError(
                        f"Partition {partition_dir} contains a bar identified as "
                        f"{bar.contract_identity}/{bar.timeframe.value}/{bar.provider}/"
                        f"{bar.dataset}, which does not match the requested partition "
                        f"{contract_identity}/{timeframe.value}/{provider}/{dataset}. Refusing "
                        f"to return data from what appears to be a misplaced or corrupted file."
                    )
            # Phase 3.3 §6 fix (critical): every loaded bar's OWN
            # ts_event must fall within THIS partition's year/month --
            # a whole valid parquet+manifest pair copied wholesale from
            # a different month's directory must fail closed here,
            # never be silently filtered away by the half-open-range
            # filter at the end of this method (which previously turned
            # this exact corruption into an innocent-looking empty
            # result).
            self._check_bars_match_partition_period(bars_in_partition_raw, partition_dir, year=year, month=month)
            # Phase 3.3 §9/§10 fix: a partition being READ must already
            # be internally coherent (one market series) -- never
            # trusted otherwise, independent of whether this read also
            # happens to be feeding a write's merge input.
            self._check_partition_coherence(bars_in_partition_raw, partition_dir)
            # Phase 3.3 §8 fix: the manifest's own descriptive claims
            # must agree with what was actually decoded from its data
            # file, not merely share the same raw-bytes checksum.
            self._verify_manifest_content_matches_bars(manifest, bars_in_partition_raw, partition_dir)
            # Phase 3.3 §4 fix: ONLY NOW, after every raw-content check
            # above has passed, canonicalize this partition's own
            # decoded bars -- dedupe IDENTICAL duplicates, raise on
            # CONFLICTING ones -- using the exact same policy
            # write_bars uses, so read and write never disagree on what
            # a duplicate canonical key means.
            bars_in_partition = self._canonicalize_bars(
                bars_in_partition_raw, context=f"Partition {partition_dir}"
            )

            collected.extend(bars_in_partition)

        # Phase 3.3 §15 fix: defense in depth, even after every
        # per-partition check above -- canonicalize the FULL
        # cross-partition result one more time before filtering/
        # sorting, so HistoricalReadResult can never contain two bars
        # for the same canonical key (identical -> collapsed to one per
        # the same policy as everywhere else; conflicting -> fails
        # closed) even if they somehow came from two different
        # partition files.
        canonical_collected = self._canonicalize_bars(
            collected, context="HistoricalBarStore.read_bars's cross-partition result"
        )
        filtered = tuple(
            sorted(
                (bar for bar in canonical_collected if start_aware <= bar.ts_event < end_aware),
                key=lambda b: b.ts_event,
            )
        )
        return HistoricalReadResult(bars=filtered, partitions_found=partitions_found)

    def _verify_partition_manifest(
        self,
        partition_dir: Path,
        parquet_path: Path,
        *,
        expected_provider: str,
        expected_dataset: str,
        expected_root_symbol: str,
        expected_contract_identity: str,
        expected_contract_year: int,
        expected_contract_month: int,
        expected_timeframe: HistoricalTimeframe,
        expected_partition_year: int,
        expected_partition_month: int,
    ) -> dict:
        """Phase 3.1 §28: verify ``partition_dir``'s manifest.json
        before any bar from it is trusted -- existence, a supported
        schema version, identity agreement with the partition we are
        about to read, that ``checksum_sha256`` matches
        ``bars.parquet``'s actual on-disk bytes right now (not merely
        what it was at write time), and (Phase 3.3 §7) that the
        manifest's own recorded ``partition_year``/``partition_month``
        agree with the directory being accessed. Raises
        :class:`~app.data.models.HistoricalStorageIntegrityError` on
        any discrepancy -- a manifest that is never actually checked
        on read is not a real integrity guarantee. Returns the parsed,
        now-verified manifest dict so callers can further cross-check
        it against the partition's actually-decoded bar content (see
        :meth:`_verify_manifest_content_matches_bars`, Phase 3.3 §8)."""
        manifest_path = partition_dir / _MANIFEST_FILENAME
        if not manifest_path.exists():
            raise HistoricalStorageIntegrityError(
                f"Partition {partition_dir} has a {_PARQUET_FILENAME} file but no "
                f"{_MANIFEST_FILENAME}; refusing to trust unverified historical data."
            )
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except Exception as exc:
            raise HistoricalStorageIntegrityError(
                f"Partition {partition_dir} manifest is unreadable or not valid JSON: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise HistoricalStorageIntegrityError(
                f"Partition {partition_dir} manifest is not a JSON object (got "
                f"{type(manifest).__name__})."
            )

        # Phase 3.3 QA compliance rework (items 4/5/6): validate every durable
        # manifest field's runtime TYPE and basic invariant BEFORE any
        # downstream equality comparison -- plain `!=`/`not in` comparisons
        # (used below and in _verify_manifest_content_matches_bars) do not
        # distinguish `True` from `1` or `1.0` from `1` in Python, so without
        # this upfront check a tampered manifest with e.g.
        # schema_version=True or contract_year=2026.0 would otherwise pass
        # undetected.
        _require_valid_manifest_schema(manifest, partition_dir=partition_dir)

        if manifest.get("schema_version") not in _SUPPORTED_BAR_SCHEMA_VERSIONS:
            raise HistoricalStorageIntegrityError(
                f"Partition {partition_dir} manifest declares schema_version="
                f"{manifest.get('schema_version')!r}, which this build of Olive does not "
                f"support (supported: {sorted(_SUPPORTED_BAR_SCHEMA_VERSIONS)})."
            )

        expected_fields = {
            "provider": expected_provider,
            "dataset": expected_dataset,
            "root_symbol": expected_root_symbol,
            "olive_contract_identity": expected_contract_identity,
            "contract_year": expected_contract_year,
            "contract_month": expected_contract_month,
            "timeframe": expected_timeframe.value,
            # Phase 3.3 §6/§7 fix: the manifest's OWN claimed
            # partition directory identity must agree with the
            # directory it was actually found in -- a parquet+manifest
            # pair copied wholesale from a different month's directory
            # would otherwise still pass every OTHER identity field
            # above (they describe the CONTRACT, not the calendar
            # partition) and the checksum (the pair is internally
            # self-consistent) while being misplaced.
            "partition_year": expected_partition_year,
            "partition_month": expected_partition_month,
        }
        for field_name, expected_value in expected_fields.items():
            if manifest.get(field_name) != expected_value:
                raise HistoricalStorageIntegrityError(
                    f"Partition {partition_dir} manifest field {field_name!r}="
                    f"{manifest.get(field_name)!r} does not match the requested partition's "
                    f"{expected_value!r}; refusing to trust a manifest that does not identify "
                    f"itself as the partition we asked for."
                )

        expected_checksum = manifest.get("checksum_sha256")
        try:
            actual_bytes = parquet_path.read_bytes()
        except OSError as exc:
            raise HistoricalStorageIntegrityError(
                f"Partition {partition_dir}'s {_PARQUET_FILENAME} could not be read to verify "
                f"its checksum: {exc}"
            ) from exc
        actual_checksum = hashlib.sha256(actual_bytes).hexdigest()
        if not isinstance(expected_checksum, str) or expected_checksum != actual_checksum:
            raise HistoricalStorageIntegrityError(
                f"Partition {partition_dir}'s {_PARQUET_FILENAME} checksum ({actual_checksum}) "
                f"does not match its manifest's recorded checksum ({expected_checksum!r}); "
                f"refusing to trust data that may be corrupted or was modified outside of "
                f"Olive's own write path."
            )
        return manifest
