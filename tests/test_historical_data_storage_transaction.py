"""Shipped, pyarrow-INDEPENDENT regression tests for
``app.data.storage.HistoricalBarStore``'s transactional/orchestration
behavior (Phase 3.2 §21).

Phase 3.1 verified this behavior (transactional rollback, manifest
tampering, ...) only with a supplemental scratch-space fake-pyarrow
harness that was never shipped in the delivered project and therefore
provided no PERMANENT regression coverage. This file closes that gap:
it ships ``tests/_fake_pyarrow.py``, a minimal fake of the handful of
pyarrow/pyarrow.parquet calls ``HistoricalBarStore`` actually makes,
and monkeypatches ``app.data.storage._import_pyarrow`` to return it --
so every test below exercises the REAL ``HistoricalBarStore`` methods
(``write_bars``, ``read_bars``, ``_write_partition_file``,
``_verify_partition_manifest``, ``_rollback_partition_to``, ...)
end-to-end, run unconditionally in every environment, including this
sandbox where the real ``pyarrow`` package cannot be installed.

This is deliberately NOT a substitute for real binary Parquet
compatibility testing -- see ``tests/test_historical_data_storage.py``
(``pytest.importorskip("pyarrow")``) for that separately-authoritative
suite, which must be run for real (and is expected to report 0
skipped) once a real ``pyarrow`` is installed, e.g. on the user's own
machine.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import app.data.storage as storage_module
from app.data.models import DataLabel, HistoricalBar, HistoricalTimeframe
from app.data.storage import (
    HistoricalBarStore,
    HistoricalReadResult,
    HistoricalStorageError,
    HistoricalStorageIntegrityError,
    HistoricalWriteResult,
)

from tests import _fake_pyarrow

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _use_fake_pyarrow(monkeypatch):
    """Every test in this file runs against the fake pyarrow surface,
    never the real package (which this sandbox cannot install) --
    patched at the one seam (``_import_pyarrow``) the real
    ``HistoricalBarStore`` uses to obtain it, so the store's own
    production code is exercised unmodified."""
    monkeypatch.setattr(storage_module, "_import_pyarrow", lambda: (_fake_pyarrow, _fake_pyarrow.parquet))


def make_bar(
    minute=0,
    open_t=100000,
    close_t=100010,
    volume=10,
    month=9,
    day=15,
    hour=10,
    year=2026,
    tick_size=Decimal("0.25"),
    provider_raw_symbol="NQZ6",
    provider_instrument_id="999",
):
    return HistoricalBar(
        root_symbol="NQ",
        contract_year=2026,
        contract_month=12,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        ts_event=datetime(year, month, day, hour, minute, tzinfo=UTC),
        open_ticks=open_t,
        high_ticks=open_t + 20,
        low_ticks=open_t - 20,
        close_ticks=close_t,
        volume=volume,
        tick_size=tick_size,
        data_label=DataLabel.HISTORICAL,
        provider="databento",
        dataset="GLBX.MDP3",
        provider_raw_symbol=provider_raw_symbol,
        provider_instrument_id=provider_instrument_id,
    )


def partition_dir_of(tmp_path):
    return (
        tmp_path / "databento" / "GLBX.MDP3" / "NQ" / "NQ-2026-12" / "ohlcv-1m" / "year=2026" / "month=09"
    )


def read_range(store, tmp_path=None, *, start_month=9, end_month=10):
    """Shared ``read_bars`` call covering exactly one calendar month of
    Sept 2026 (``partition_dir_of``'s own partition), used by every
    read-side regression test below that forges or corrupts that one
    partition directly."""
    return store.read_bars(
        root_symbol="NQ",
        contract_year=2026,
        contract_month=12,
        timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, start_month, 1, tzinfo=UTC),
        end=datetime(2026, end_month, 1, tzinfo=UTC),
    )


def _write_raw_partition(tmp_path, bars, store, *, manifest_overrides=None):
    """Write ``bars`` (and a matching, checksum-correct manifest)
    directly to :func:`partition_dir_of`'s own partition directory,
    entirely bypassing ``write_bars``'s own checks -- simulating
    storage that is ALREADY in some state (duplicate, conflicting,
    incoherent) that the next legitimate ``write_bars``/``read_bars``
    call must still catch. Mirrors the pattern Phase 3.2 §18's
    ``test_write_rejects_preexisting_conflicting_duplicates_in_storage``
    established, generalized for reuse. Assumes every bar in ``bars``
    shares the same calendar year/month as ``partition_dir_of``
    (2026-09) -- callers testing a genuinely MISPLACED partition (a
    different month) build their own manifest instead."""
    partition_dir = partition_dir_of(tmp_path)
    partition_dir.mkdir(parents=True, exist_ok=True)
    pa, pq = _fake_pyarrow, _fake_pyarrow.parquet
    table = store._bars_to_table(list(bars), pa)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    parquet_bytes = buffer.getvalue()
    (partition_dir / "bars.parquet").write_bytes(parquet_bytes)

    sample = bars[0]
    manifest = {
        "schema_version": sample.schema_version,
        "olive_contract_identity": sample.contract_identity,
        "provider_raw_symbol": sample.provider_raw_symbol,
        "root_symbol": sample.root_symbol,
        "contract_year": sample.contract_year,
        "contract_month": sample.contract_month,
        "provider": sample.provider,
        "dataset": sample.dataset,
        "timeframe": sample.timeframe.value,
        "partition_year": sample.ts_event.year,
        "partition_month": sample.ts_event.month,
        "requested_start_utc": None,
        "requested_end_utc": None,
        "actual_first_timestamp_utc": min(b.ts_event for b in bars).isoformat(),
        "actual_last_timestamp_utc": max(b.ts_event for b in bars).isoformat(),
        "record_count": len(bars),
        "data_label": sample.data_label.value,
        "last_written_at_utc": datetime.now(UTC).isoformat(),
        "tick_size": str(sample.tick_size),
        "contract_multiplier": None,
        "estimated_cost_usd": None,
        "storage_format": "parquet",
        "checksum_sha256": hashlib.sha256(parquet_bytes).hexdigest(),
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (partition_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return partition_dir


@pytest.fixture
def store(tmp_path):
    return HistoricalBarStore(tmp_path)


# -- §3/§4: HistoricalWriteResult / HistoricalReadResult self-validation -----


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(partitions_written=-1, new_records=0, total_records=0),
        dict(partitions_written=0, new_records=-2, total_records=0),
        dict(partitions_written=0, new_records=0, total_records=-3),
        dict(partitions_written=True, new_records=0, total_records=0),
        dict(partitions_written=1.0, new_records=0, total_records=0),
        dict(partitions_written=0, new_records=5, total_records=3),  # new > total
        dict(partitions_written=5, new_records=0, total_records=3),  # §16: partitions > total
        # QA compliance rework item 10: total_records > 0 but
        # partitions_written == 0 -- unreachable from write_bars (a
        # non-empty write always writes at least one partition).
        dict(partitions_written=0, new_records=0, total_records=5),
    ],
)
def test_write_result_rejects_malformed_or_inconsistent_construction(kwargs):
    with pytest.raises(HistoricalStorageError):
        HistoricalWriteResult(**kwargs)


def test_write_result_accepts_well_formed_construction():
    result = HistoricalWriteResult(partitions_written=2, new_records=3, total_records=5)
    assert (result.partitions_written, result.new_records, result.total_records) == (2, 3, 5)


def test_write_result_accepts_partitions_written_exactly_equal_to_total_records():
    # §16 boundary: every partition written contains exactly one
    # record -- partitions_written == total_records is the legitimate
    # edge, not an inconsistency.
    result = HistoricalWriteResult(partitions_written=3, new_records=3, total_records=3)
    assert (result.partitions_written, result.new_records, result.total_records) == (3, 3, 3)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(bars=(), partitions_found=-1),
        dict(bars=None, partitions_found=0),
        dict(bars="not a sequence", partitions_found=0),
        dict(bars=({"not": "a bar"},), partitions_found=0),
        dict(bars=(), partitions_found=True),
        dict(bars=(), partitions_found=1.5),
    ],
)
def test_read_result_rejects_malformed_construction(kwargs):
    with pytest.raises(HistoricalStorageError):
        HistoricalReadResult(**kwargs)


def test_read_result_rejects_nonempty_bars_with_zero_partitions_found():
    # QA compliance rework item 9: HistoricalReadResult(bars=(real_bar,),
    # partitions_found=0) is impossible -- it is never legitimate to
    # return stored bars from zero partitions.
    with pytest.raises(HistoricalStorageError):
        HistoricalReadResult(bars=(make_bar(),), partitions_found=0)


def test_read_result_accepts_well_formed_construction_and_normalizes_list_to_tuple():
    bar = make_bar()
    result = HistoricalReadResult(bars=[bar], partitions_found=1)
    assert result.bars == (bar,)
    assert isinstance(result.bars, tuple)
    assert result.found_any_data is True


# -- §16: relative store root is CWD-stable ----------------------------------


def test_store_root_is_cwd_independent_once_constructed(tmp_path, monkeypatch):
    import os

    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    relative_target = tmp_path / "olive-data"
    relative_target.mkdir()

    monkeypatch.chdir(tmp_path)
    store_rel = HistoricalBarStore("olive-data")
    resolved_before = store_rel.root_dir

    monkeypatch.chdir(other_dir)
    resolved_after = store_rel.root_dir

    assert resolved_before == resolved_after == relative_target.resolve()

    bar = make_bar()
    store_rel.write_bars([bar])
    read = store_rel.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    assert len(read.bars) == 1


# -- §17: existing partition must be manifest/checksum-verified before merge


def test_write_rejects_existing_parquet_with_no_manifest(store, tmp_path):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    (partition_dir / "manifest.json").unlink()  # simulate an orphaned partition

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([make_bar(minute=1)])


def test_write_rejects_existing_manifest_with_no_parquet(store, tmp_path):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    (partition_dir / "bars.parquet").unlink()  # simulate an orphaned manifest

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([make_bar(minute=1)])


def test_write_rejects_tampered_checksum_before_merge(store, tmp_path):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    manifest_path = partition_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["checksum_sha256"] = "0" * 64  # tamper
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([make_bar(minute=1)])


def test_write_rejects_manifest_identity_mismatch_before_merge(store, tmp_path):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    manifest_path = partition_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["root_symbol"] = "MNQ"  # tamper identity (checksum now also stale, but identity check runs first)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([make_bar(minute=1)])


def test_write_rejects_unsupported_schema_version_in_existing_manifest(store, tmp_path):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    manifest_path = partition_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 999
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([make_bar(minute=1)])


# -- QA compliance rework §4/§5/§6/§12: manifest integer fields are NOT
# strict against Python's bool/float equality traps unless explicitly
# type-checked. `True == 1` and `True in {1}` are both True in Python, and
# `1.0 == 1` is also True -- so a tampered manifest field that is a bool
# or a whole-number float, rather than a genuine int, must still be
# rejected by _require_valid_manifest_schema before any downstream `!=`
# comparison ever runs. Exercised on BOTH the write-merge path (an
# existing manifest read before merging new data) and the read path (an
# existing manifest read directly) -- the same manifest-verification gate
# backs both. --------------------------------------------------------------


def _tamper_and_read(store, tmp_path, field_name, value):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    manifest_path = partition_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field_name] = value
    manifest_path.write_text(json.dumps(manifest))
    return read_range(store, tmp_path)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("record_count", True),
        ("record_count", 1.0),
        ("contract_year", 2026.0),
        ("contract_month", 12.0),
        ("partition_year", 2026.0),
        ("partition_month", 9.0),
    ],
)
def test_read_rejects_manifest_int_field_bool_or_float_equality_trap(store, tmp_path, field_name, value):
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_and_read(store, tmp_path, field_name, value)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("schema_version", True),
        ("record_count", True),
        ("contract_year", 2026.0),
        ("partition_month", 9.0),
    ],
)
def test_write_merge_rejects_manifest_int_field_bool_or_float_equality_trap(store, tmp_path, field_name, value):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    manifest_path = partition_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field_name] = value
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([make_bar(minute=1)])


def test_read_rejects_manifest_record_count_zero_as_impossible_state(store, tmp_path):
    # §6: record_count == 0 is an impossible/corrupt state for a
    # persisted partition -- write_bars never creates a partition
    # directory without at least one bar.
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_and_read(store, tmp_path, "record_count", 0)


def test_read_rejects_manifest_record_count_negative(store, tmp_path):
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_and_read(store, tmp_path, "record_count", -1)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("provider", ""),
        ("provider", "   "),
        ("provider", 123),
        ("dataset", None),
        ("root_symbol", True),
        ("olive_contract_identity", []),
        ("timeframe", "not-a-real-timeframe"),
        ("data_label", "not-a-real-label"),
        ("storage_format", "csv"),
        ("storage_format", None),
        ("checksum_sha256", "not-valid-hex"),
        ("checksum_sha256", 12345),
        ("provider_raw_symbol", ""),
    ],
)
def test_read_rejects_manifest_string_field_malformed(store, tmp_path, field_name, value):
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_and_read(store, tmp_path, field_name, value)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("tick_size", True),
        ("tick_size", 0.25),  # bare JSON number, never a Decimal-serialized string
        ("tick_size", "0"),  # not strictly positive
        ("tick_size", "-0.25"),
        ("tick_size", "NaN"),
        ("tick_size", "Infinity"),
        ("tick_size", None),  # not nullable
        ("contract_multiplier", True),
        ("contract_multiplier", "0"),  # not strictly positive when provided
        ("estimated_cost_usd", True),
        ("estimated_cost_usd", "-1"),  # allow_zero but not negative
    ],
)
def test_read_rejects_manifest_decimal_field_malformed(store, tmp_path, field_name, value):
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_and_read(store, tmp_path, field_name, value)


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("actual_first_timestamp_utc", None),  # not nullable
        ("actual_first_timestamp_utc", True),
        ("actual_first_timestamp_utc", "not-a-timestamp"),
        ("actual_first_timestamp_utc", "2026-09-15T10:00:00"),  # naive, no tzinfo
        ("actual_first_timestamp_utc", "2026-09-15T10:00:00-05:00"),  # aware but not UTC
        ("requested_start_utc", True),  # nullable, but when present must be valid
        ("requested_start_utc", "not-a-timestamp"),
    ],
)
def test_read_rejects_manifest_timestamp_field_malformed(store, tmp_path, field_name, value):
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_and_read(store, tmp_path, field_name, value)


def test_read_rejects_manifest_first_timestamp_after_last_timestamp(store, tmp_path):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    manifest_path = partition_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # A single-bar partition legitimately has first == last, so force a
    # genuine impossible ordering directly rather than swapping (which
    # would tamper nothing in that case).
    manifest["actual_first_timestamp_utc"] = "2026-09-15T23:59:59+00:00"
    manifest["actual_last_timestamp_utc"] = "2026-09-15T00:00:00+00:00"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(HistoricalStorageIntegrityError):
        read_range(store, tmp_path)


# -- Phase 3 final completion pass (item A): last_written_at_utc, written
# into every manifest but previously never validated at all. Independent
# audit reproduced both last_written_at_utc=True and
# last_written_at_utc="not-a-date" being silently accepted. -----------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        1,
        1.0,
        "",
        " ",
        "not-a-date",
        "2026-09-15T10:00:00",  # naive, no tzinfo
        "2026-09-15T10:00:00-05:00",  # aware but not UTC
    ],
)
def test_read_rejects_manifest_last_written_at_utc_malformed(store, tmp_path, value):
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_and_read(store, tmp_path, "last_written_at_utc", value)


def test_read_accepts_manifest_last_written_at_utc_well_formed(store, tmp_path):
    # Sanity check: a genuinely well-formed manifest (as write_bars
    # itself always produces) is not rejected by the new check.
    store.write_bars([make_bar(minute=0)])
    result = read_range(store, tmp_path)
    assert result.found_any_data is True


# -- Phase 3 final completion pass (item B): requested_start_utc/
# requested_end_utc were each individually validated as well-formed
# nullable UTC timestamps, but the PAIR was never compared -- a forged
# manifest with requested_start_utc AFTER requested_end_utc (both
# individually valid) was silently accepted. -------------------------------


def _tamper_requested_range_and_read(store, tmp_path, *, start, end):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    manifest_path = partition_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["requested_start_utc"] = start
    manifest["requested_end_utc"] = end
    manifest_path.write_text(json.dumps(manifest))
    return read_range(store, tmp_path)


def test_read_rejects_manifest_requested_range_reversed(store, tmp_path):
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_requested_range_and_read(
            store, tmp_path,
            start="2026-10-01T00:00:00+00:00",
            end="2026-09-01T00:00:00+00:00",
        )


def test_read_rejects_manifest_requested_range_equal(store, tmp_path):
    # Strictly `<` is required, mirroring write_bars's own public
    # requested_start/requested_end contract -- start == end is rejected,
    # not treated as a legitimate zero-width window.
    with pytest.raises(HistoricalStorageIntegrityError):
        _tamper_requested_range_and_read(
            store, tmp_path,
            start="2026-09-01T00:00:00+00:00",
            end="2026-09-01T00:00:00+00:00",
        )


def test_read_accepts_manifest_requested_range_forward_ordered(store, tmp_path):
    result = _tamper_requested_range_and_read(
        store, tmp_path,
        start="2026-09-01T00:00:00+00:00",
        end="2026-09-30T00:00:00+00:00",
    )
    assert result.found_any_data is True


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-09-01T00:00:00+00:00", None),
        (None, "2026-09-30T00:00:00+00:00"),
        (None, None),
    ],
)
def test_read_accepts_manifest_requested_range_one_sided_or_absent(store, tmp_path, start, end):
    # One-sided (or entirely absent) requested-range metadata remains a
    # legitimate manifest state -- the cross-field check only applies
    # when BOTH are present.
    result = _tamper_requested_range_and_read(store, tmp_path, start=start, end=end)
    assert result.found_any_data is True


# -- §18: existing stored CONFLICTING duplicates are rejected, not silently
# resolved by "last one wins" -------------------------------------------------


def test_write_rejects_preexisting_conflicting_duplicates_in_storage(store, tmp_path, monkeypatch):
    """Simulates storage that is ALREADY corrupt (two conflicting bars
    sharing one canonical key) by writing directly through the fake
    pyarrow layer, bypassing write_bars's own conflict detection --
    the next legitimate write_bars call must still catch it rather
    than silently keeping whichever bar _table_to_bars happens to
    return last."""
    bar_a = make_bar(minute=0, open_t=100000, close_t=100010)
    bar_b = make_bar(minute=0, open_t=500000, close_t=500010)  # same ts_event, different OHLC

    partition_dir = partition_dir_of(tmp_path)
    partition_dir.mkdir(parents=True)
    pa, pq = _fake_pyarrow, _fake_pyarrow.parquet
    table = store._bars_to_table([bar_a, bar_b], pa)
    import io

    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    parquet_bytes = buffer.getvalue()
    (partition_dir / "bars.parquet").write_bytes(parquet_bytes)

    import hashlib

    manifest = {
        "schema_version": bar_a.schema_version,
        "olive_contract_identity": bar_a.contract_identity,
        "provider_raw_symbol": bar_a.provider_raw_symbol,
        "root_symbol": bar_a.root_symbol,
        "contract_year": bar_a.contract_year,
        "contract_month": bar_a.contract_month,
        "provider": bar_a.provider,
        "dataset": bar_a.dataset,
        "timeframe": bar_a.timeframe.value,
        # Phase 3.3 §6/§7: the manifest's own claimed partition
        # identity, matching the year=2026/month=09 directory
        # partition_dir_of() points at -- without these, the Phase
        # 3.3 partition-identity check (not the conflicting-duplicate
        # check this test exists to exercise) would be what raises.
        "partition_year": bar_a.ts_event.year,
        "partition_month": bar_a.ts_event.month,
        "requested_start_utc": None,
        "requested_end_utc": None,
        "actual_first_timestamp_utc": bar_a.ts_event.isoformat(),
        "actual_last_timestamp_utc": bar_a.ts_event.isoformat(),
        "record_count": 2,
        "data_label": bar_a.data_label.value,
        "last_written_at_utc": datetime.now(UTC).isoformat(),
        "tick_size": str(bar_a.tick_size),
        "contract_multiplier": None,
        "estimated_cost_usd": None,
        "storage_format": "parquet",
        "checksum_sha256": hashlib.sha256(parquet_bytes).hexdigest(),
    }
    (partition_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([make_bar(minute=1)])


# -- §manifest-write failure rolls back parquet (single-partition) ----------


def test_manifest_write_failure_rolls_back_parquet_to_previous_state(store, tmp_path, monkeypatch):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    original_parquet_bytes = (partition_dir / "bars.parquet").read_bytes()
    original_manifest_bytes = (partition_dir / "manifest.json").read_bytes()

    # Inject the failure at the underlying os.replace() call (what
    # _atomic_write_bytes itself uses) rather than replacing
    # _atomic_write_bytes wholesale -- that keeps this test exercising
    # _atomic_write_bytes's OWN OSError -> HistoricalStorageError
    # translation (§14/§28), not bypassing it.
    original_os_replace = storage_module.os.replace
    call_count = {"n": 0}

    def flaky_os_replace(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:  # 1st replace = merged parquet (ok); 2nd = manifest (fails)
            raise OSError("simulated manifest write failure")
        return original_os_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", flaky_os_replace)

    with pytest.raises(HistoricalStorageError):
        store.write_bars([make_bar(minute=0), make_bar(minute=1)])

    assert (partition_dir / "bars.parquet").read_bytes() == original_parquet_bytes
    assert (partition_dir / "manifest.json").read_bytes() == original_manifest_bytes


# -- §cross-partition commit failure rolls back earlier partition -----------


def test_cross_partition_failure_rolls_back_earlier_partition(store, tmp_path, monkeypatch):
    sept_bar = make_bar(minute=0, month=9, day=15)
    store.write_bars([sept_bar])
    sept_partition = partition_dir_of(tmp_path)
    original_sept_parquet = (sept_partition / "bars.parquet").read_bytes()
    original_sept_manifest = (sept_partition / "manifest.json").read_bytes()

    # Sept commits first (2 os.replace calls: parquet, manifest) and
    # succeeds; Oct's FIRST os.replace call (its parquet write, the
    # 3rd call overall) is made to fail -- injected at the same
    # underlying os.replace() seam as the test above, so
    # _atomic_write_bytes's own translation still runs for real.
    original_os_replace = storage_module.os.replace
    call_count = {"n": 0}

    def flaky_os_replace(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise OSError("simulated disk failure committing second partition")
        return original_os_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", flaky_os_replace)

    sept_new = make_bar(minute=1, month=9, day=15)
    oct_new = make_bar(minute=0, month=10, day=1)
    with pytest.raises(HistoricalStorageError):
        store.write_bars([sept_new, oct_new])

    # The September partition was committed successfully DURING this
    # same write_bars call, but the call as a whole failed when Oct's
    # commit failed -- it must be rolled back to its pre-call state,
    # never left with sept_new partially merged in.
    assert (sept_partition / "bars.parquet").read_bytes() == original_sept_parquet
    assert (sept_partition / "manifest.json").read_bytes() == original_sept_manifest


# -- §rollback failure itself becomes a dedicated integrity error -----------


def test_rollback_failure_raises_integrity_error_not_silently_ignored(store, tmp_path, monkeypatch):
    sept_bar = make_bar(minute=0, month=9, day=15)
    store.write_bars([sept_bar])

    original_write_partition_file = HistoricalBarStore._write_partition_file
    call_count = {"n": 0}

    def flaky_write_partition_file(self, partition_dir, merged_bars, pa, pq, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated disk failure committing second partition")
        return original_write_partition_file(self, partition_dir, merged_bars, pa, pq, **kwargs)

    monkeypatch.setattr(HistoricalBarStore, "_write_partition_file", flaky_write_partition_file)

    original_rollback = HistoricalBarStore._rollback_partition_to

    def flaky_rollback(partition_dir, parquet_bytes, manifest_bytes):
        return ["simulated rollback failure: disk unavailable"]

    monkeypatch.setattr(HistoricalBarStore, "_rollback_partition_to", staticmethod(flaky_rollback))

    sept_new = make_bar(minute=1, month=9, day=15)
    oct_new = make_bar(minute=0, month=10, day=1)
    with pytest.raises(HistoricalStorageIntegrityError, match="rollback"):
        store.write_bars([sept_new, oct_new])


# -- §storage I/O failures become structured HistoricalStorageError ---------


def test_disk_failure_during_write_becomes_structured_storage_error(store, monkeypatch):
    def raise_disk_full(src, dst):
        raise OSError("[Errno 28] No space left on device")

    # Injected at the os.replace() seam _atomic_write_bytes itself
    # uses, so this test proves _atomic_write_bytes's OWN translation
    # (never a bare monkeypatch of the translating method itself,
    # which would bypass the very behavior under test).
    monkeypatch.setattr(storage_module.os, "replace", raise_disk_full)

    with pytest.raises(HistoricalStorageError):
        store.write_bars([make_bar()])


def test_pyarrow_serialization_failure_becomes_structured_storage_error(store, monkeypatch):
    def raise_serialization_error(table, where):
        raise ValueError("simulated pyarrow serialization failure")

    monkeypatch.setattr(_fake_pyarrow.parquet, "write_table", staticmethod(raise_serialization_error))

    with pytest.raises(HistoricalStorageError):
        store.write_bars([make_bar()])


# -- §14: _bars_to_table's pa.array/pa.Table.from_arrays conversion is the
# ONLY narrowly-scoped try/except -- a genuine Olive bug in the
# attribute-gathering loop above it must still propagate unmodified ---------


def test_bars_to_table_array_conversion_failure_becomes_structured_storage_error(store, monkeypatch):
    def raise_array_error(values, type=None):
        raise ValueError("simulated pyarrow array conversion failure")

    monkeypatch.setattr(_fake_pyarrow, "array", raise_array_error)

    with pytest.raises(HistoricalStorageError):
        store.write_bars([make_bar()])


def test_bars_to_table_from_arrays_failure_becomes_structured_storage_error(store, monkeypatch):
    def raise_from_arrays(cls, arrays, schema):
        raise ValueError("simulated pyarrow Table.from_arrays failure")

    monkeypatch.setattr(_fake_pyarrow.Table, "from_arrays", classmethod(raise_from_arrays))

    with pytest.raises(HistoricalStorageError):
        store.write_bars([make_bar()])


def test_bars_to_table_does_not_disguise_a_genuine_olive_attribute_error(store):
    # The try/except in _bars_to_table is scoped to ONLY the
    # pa.array/pa.Table.from_arrays conversion calls -- a genuine
    # Olive bug in the per-bar attribute-gathering loop above them
    # (simulated here by passing something that is not a real
    # HistoricalBar at all) must propagate as whatever raw error it
    # naturally raises, never be disguised as a HistoricalStorageError
    # about Arrow conversion.
    pa, _pq = _fake_pyarrow, _fake_pyarrow.parquet
    with pytest.raises(AttributeError):
        store._bars_to_table([object()], pa)


# -- §2/§3 (critical): pre-existing identical duplicates never go negative --


def test_write_canonicalizes_preexisting_identical_duplicate_without_negative_new_records(store, tmp_path):
    """A pre-existing partition holding two IDENTICAL copies of the
    same bar (a legitimate, tolerated storage state per the
    canonicalize-before-count policy) must never cause write_bars to
    compute a negative new_records count, nor raise while
    constructing its own HistoricalWriteResult AFTER the write has
    already durably committed to disk."""
    bar_a = make_bar(minute=0)
    _write_raw_partition(tmp_path, [bar_a, bar_a], store)

    result = store.write_bars([make_bar(minute=0)])

    assert result.partitions_written == 1
    assert result.new_records == 0
    assert result.total_records == 1


def test_write_records_genuinely_new_bar_correctly_after_canonicalizing_duplicates(store, tmp_path):
    bar_a = make_bar(minute=0)
    _write_raw_partition(tmp_path, [bar_a, bar_a], store)

    result = store.write_bars([make_bar(minute=1)])  # genuinely new ts_event

    assert result.partitions_written == 1
    assert result.new_records == 1
    assert result.total_records == 2


# -- §4/§15: read canonicalizes identical duplicates, rejects conflicting ---


def test_read_canonicalizes_preexisting_identical_duplicates_in_storage(store, tmp_path):
    bar_a = make_bar(minute=0)
    _write_raw_partition(tmp_path, [bar_a, bar_a], store)

    read = read_range(store)

    assert len(read.bars) == 1
    assert read.bars[0].ts_event == bar_a.ts_event
    assert read.bars[0].open_ticks == bar_a.open_ticks


def test_read_rejects_preexisting_conflicting_duplicates_in_storage(store, tmp_path):
    bar_a = make_bar(minute=0, open_t=100000, close_t=100010)
    bar_b = make_bar(minute=0, open_t=500000, close_t=500010)  # same ts_event, different OHLC
    _write_raw_partition(tmp_path, [bar_a, bar_b], store)

    with pytest.raises(HistoricalStorageIntegrityError):
        read_range(store)


# -- §5: orphan manifest/parquet pairs are rejected on READ too -------------


def test_read_rejects_orphan_manifest_with_no_parquet(store, tmp_path):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    (partition_dir / "bars.parquet").unlink()

    with pytest.raises(HistoricalStorageIntegrityError):
        read_range(store)


def test_read_rejects_orphan_parquet_with_no_manifest(store, tmp_path):
    store.write_bars([make_bar(minute=0)])
    partition_dir = partition_dir_of(tmp_path)
    (partition_dir / "manifest.json").unlink()

    with pytest.raises(HistoricalStorageIntegrityError):
        read_range(store)


# -- §6 (critical): a partition's bars must match ITS OWN directory's
# year/month even when the manifest is forged to correctly claim it --------


def _forge_partition_with_wrong_month_content(tmp_path, store):
    """Place a parquet+manifest pair in :func:`partition_dir_of`'s
    September (year=2026/month=09) directory whose manifest is forged
    to correctly self-report partition_year=2026/partition_month=9 --
    passing the manifest-vs-directory check (§7) -- while the actual
    bar decoded from the parquet file is October-dated. Only the
    independent, manifest-blind per-bar content check
    (``_check_bars_match_partition_period``) can catch this."""
    october_bar = make_bar(minute=0, month=10, day=1)
    partition_dir = partition_dir_of(tmp_path)
    partition_dir.mkdir(parents=True, exist_ok=True)
    pa, pq = _fake_pyarrow, _fake_pyarrow.parquet
    table = store._bars_to_table([october_bar], pa)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    parquet_bytes = buffer.getvalue()
    (partition_dir / "bars.parquet").write_bytes(parquet_bytes)

    manifest = {
        "schema_version": october_bar.schema_version,
        "olive_contract_identity": october_bar.contract_identity,
        "provider_raw_symbol": october_bar.provider_raw_symbol,
        "root_symbol": october_bar.root_symbol,
        "contract_year": october_bar.contract_year,
        "contract_month": october_bar.contract_month,
        "provider": october_bar.provider,
        "dataset": october_bar.dataset,
        "timeframe": october_bar.timeframe.value,
        # Forged to correctly claim the September directory this pair
        # was actually placed in.
        "partition_year": 2026,
        "partition_month": 9,
        "requested_start_utc": None,
        "requested_end_utc": None,
        "actual_first_timestamp_utc": october_bar.ts_event.isoformat(),
        "actual_last_timestamp_utc": october_bar.ts_event.isoformat(),
        "record_count": 1,
        "data_label": october_bar.data_label.value,
        "last_written_at_utc": datetime.now(UTC).isoformat(),
        "tick_size": str(october_bar.tick_size),
        "contract_multiplier": None,
        "estimated_cost_usd": None,
        "storage_format": "parquet",
        "checksum_sha256": hashlib.sha256(parquet_bytes).hexdigest(),
    }
    (partition_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return partition_dir


def test_write_rejects_partition_whose_bar_content_is_a_different_month_even_with_forged_manifest(store, tmp_path):
    _forge_partition_with_wrong_month_content(tmp_path, store)

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([make_bar(minute=1, month=9, day=15)])


def test_read_rejects_partition_whose_bar_content_is_a_different_month_even_with_forged_manifest(store, tmp_path):
    _forge_partition_with_wrong_month_content(tmp_path, store)

    with pytest.raises(HistoricalStorageIntegrityError):
        read_range(store)


# -- §9/§10: one partition must stay internally coherent, on write AND read -


def test_write_rejects_preexisting_partition_with_mixed_tick_size(store, tmp_path):
    bar_a = make_bar(minute=0, tick_size=Decimal("0.25"))
    bar_b = make_bar(minute=1, tick_size=Decimal("0.5"))
    _write_raw_partition(tmp_path, [bar_a, bar_b], store)

    with pytest.raises(HistoricalStorageIntegrityError, match="tick_size"):
        store.write_bars([make_bar(minute=2)])


def test_read_rejects_preexisting_partition_with_mixed_tick_size(store, tmp_path):
    bar_a = make_bar(minute=0, tick_size=Decimal("0.25"))
    bar_b = make_bar(minute=1, tick_size=Decimal("0.5"))
    _write_raw_partition(tmp_path, [bar_a, bar_b], store)

    with pytest.raises(HistoricalStorageIntegrityError, match="tick_size"):
        read_range(store)


def test_write_rejects_preexisting_partition_with_mixed_provider_raw_symbol(store, tmp_path):
    bar_a = make_bar(minute=0, provider_raw_symbol="NQZ6")
    bar_b = make_bar(minute=1, provider_raw_symbol="NQZ26")
    _write_raw_partition(tmp_path, [bar_a, bar_b], store)

    with pytest.raises(HistoricalStorageIntegrityError, match="provider_raw_symbol"):
        store.write_bars([make_bar(minute=2)])


def test_read_rejects_preexisting_partition_with_mixed_provider_raw_symbol(store, tmp_path):
    bar_a = make_bar(minute=0, provider_raw_symbol="NQZ6")
    bar_b = make_bar(minute=1, provider_raw_symbol="NQZ26")
    _write_raw_partition(tmp_path, [bar_a, bar_b], store)

    with pytest.raises(HistoricalStorageIntegrityError, match="provider_raw_symbol"):
        read_range(store)


def test_write_rejects_preexisting_partition_with_mixed_provider_instrument_id(store, tmp_path):
    bar_a = make_bar(minute=0, provider_instrument_id="999")
    bar_b = make_bar(minute=1, provider_instrument_id="1000")
    _write_raw_partition(tmp_path, [bar_a, bar_b], store)

    with pytest.raises(HistoricalStorageIntegrityError, match="provider_instrument_id"):
        store.write_bars([make_bar(minute=2)])


def test_read_rejects_preexisting_partition_with_mixed_provider_instrument_id(store, tmp_path):
    bar_a = make_bar(minute=0, provider_instrument_id="999")
    bar_b = make_bar(minute=1, provider_instrument_id="1000")
    _write_raw_partition(tmp_path, [bar_a, bar_b], store)

    with pytest.raises(HistoricalStorageIntegrityError, match="provider_instrument_id"):
        read_range(store)


# -- §15: the FINAL cross-partition read result can never itself contain
# two bars for the same canonical key (unit-level defense-in-depth check,
# since §6's per-bar partition-period enforcement already makes a genuine
# cross-PARTITION duplicate of the same ts_event structurally unreachable
# through the public read_bars path) ------------------------------------


# -- §21 adversarial sweep: write_bars's rewritten public boundary still
# rejects non-iterable/non-HistoricalBar input and accepts an empty
# sequence as a legitimate no-op, exactly as before the Phase 3.3 rewrite --


@pytest.mark.parametrize("bad_bars", [None, 42, True, "not a sequence of bars", object()])
def test_write_bars_rejects_non_iterable_input(store, bad_bars):
    with pytest.raises(HistoricalStorageError):
        store.write_bars(bad_bars)


def test_write_bars_rejects_non_historicalbar_elements(store):
    with pytest.raises(HistoricalStorageError):
        store.write_bars([{"not": "a bar"}])


def test_write_bars_accepts_empty_sequence(store):
    result = store.write_bars([])
    assert (result.partitions_written, result.new_records, result.total_records) == (0, 0, 0)


def test_canonicalize_bars_collapses_identical_and_rejects_conflicting():
    bar_a = make_bar(minute=0)
    bar_a_copy = make_bar(minute=0)
    bar_b = make_bar(minute=1)

    result = HistoricalBarStore._canonicalize_bars([bar_a, bar_b, bar_a_copy], context="test")
    assert len(result) == 2
    assert {b.ts_event for b in result} == {bar_a.ts_event, bar_b.ts_event}

    conflicting = make_bar(minute=0, open_t=999999, close_t=999999)
    with pytest.raises(HistoricalStorageIntegrityError):
        HistoricalBarStore._canonicalize_bars([bar_a, conflicting], context="test")
