"""Tests for app.data.storage.HistoricalBarStore.

These tests require the real ``pyarrow`` package and are skipped (not
failed) wherever it is not installed -- this sandbox could not install
pyarrow (see docs/historical_data.md "Known limitations"), so this file
is expected to report "skipped" here. It is written to run for real,
unmodified, on any machine where ``pip install pyarrow`` succeeds (the
user's own machine, CI, ...).

Every test uses ``pytest.tmp_path`` -- never the repository's real
``data/historical`` directory -- and never touches a network or a
provider.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

pytest.importorskip("pyarrow", reason="pyarrow is not installed in this environment")

from app.data.models import DataLabel, HistoricalBar, HistoricalTimeframe
from app.data.storage import HistoricalBarStore, HistoricalStorageError, HistoricalStorageIntegrityError

UTC = timezone.utc


def make_bar(minute=0, open_t=100000, close_t=100010, volume=10, month=9, day=15, hour=10, year=2026):
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
        tick_size=Decimal("0.25"),
        data_label=DataLabel.HISTORICAL,
        provider="databento",
        dataset="GLBX.MDP3",
        provider_raw_symbol="NQZ6",
        provider_instrument_id="999",
    )


@pytest.fixture
def store(tmp_path):
    return HistoricalBarStore(tmp_path)


def test_round_trip_single_bar(store):
    bar = make_bar(minute=0)
    store.write_bars([bar])
    read = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    assert len(read.bars) == 1
    got = read.bars[0]
    assert got.ts_event == bar.ts_event
    assert got.open == bar.open
    assert got.high == bar.high
    assert got.low == bar.low
    assert got.close == bar.close
    assert got.volume == bar.volume
    assert got.data_label == DataLabel.HISTORICAL
    assert got.provider == bar.provider
    assert got.dataset == bar.dataset
    assert got.provider_raw_symbol == bar.provider_raw_symbol
    assert got.tick_size == bar.tick_size
    assert got.contract_identity == bar.contract_identity


def test_round_trip_multi_bar_preserves_order_and_values(store):
    bars = [make_bar(minute=m, open_t=100000 + m * 100, close_t=100010 + m * 100) for m in range(5)]
    store.write_bars(bars)
    read = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    assert [b.ts_event.minute for b in read.bars] == [0, 1, 2, 3, 4]
    assert [b.open_ticks for b in read.bars] == [100000, 100100, 100200, 100300, 100400]


def test_utc_timestamp_preserved_exactly(store):
    bar = make_bar(minute=30)
    store.write_bars([bar])
    read = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    assert read.bars[0].ts_event.tzinfo is not None
    assert read.bars[0].ts_event.utcoffset() == timezone.utc.utcoffset(None)
    assert read.bars[0].ts_event == bar.ts_event


def test_decimal_tick_size_preserved_exactly(store):
    bar = make_bar()
    store.write_bars([bar])
    read = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    assert read.bars[0].tick_size == Decimal("0.25")
    assert str(read.bars[0].tick_size) == "0.25"


def test_volume_preserved_as_int(store):
    bar = make_bar(volume=123456)
    store.write_bars([bar])
    read = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    assert read.bars[0].volume == 123456
    assert isinstance(read.bars[0].volume, int)


def test_manifest_created_with_required_fields_and_no_secret(store, tmp_path):
    bar = make_bar()
    store.write_bars(
        [bar],
        requested_start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        requested_end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
        estimated_cost_usd=Decimal("2.50"),
        contract_multiplier=Decimal("20"),
    )
    manifest_path = (
        tmp_path / "databento" / "GLBX.MDP3" / "NQ" / "NQ-2026-12" / "ohlcv-1m" / "year=2026" / "month=09" / "manifest.json"
    )
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())

    required_fields = {
        "schema_version", "olive_contract_identity", "provider_raw_symbol", "root_symbol",
        "contract_year", "contract_month", "provider", "dataset", "timeframe",
        "requested_start_utc", "requested_end_utc", "actual_first_timestamp_utc",
        "actual_last_timestamp_utc", "record_count", "data_label", "tick_size",
        "contract_multiplier", "estimated_cost_usd", "storage_format", "checksum_sha256",
    }
    assert required_fields <= set(manifest.keys())
    assert manifest["olive_contract_identity"] == "NQ-2026-12"
    assert manifest["record_count"] == 1
    assert manifest["data_label"] == "HISTORICAL"

    manifest_text_lower = json.dumps(manifest).lower()
    assert "api_key" not in manifest_text_lower
    assert "secret" not in manifest_text_lower
    assert "token" not in manifest_text_lower


def test_manifest_checksum_matches_parquet_file(store, tmp_path):
    import hashlib

    bar = make_bar()
    store.write_bars([bar])
    partition_dir = (
        tmp_path / "databento" / "GLBX.MDP3" / "NQ" / "NQ-2026-12" / "ohlcv-1m" / "year=2026" / "month=09"
    )
    parquet_bytes = (partition_dir / "bars.parquet").read_bytes()
    manifest = json.loads((partition_dir / "manifest.json").read_text())
    assert manifest["checksum_sha256"] == hashlib.sha256(parquet_bytes).hexdigest()


def test_idempotent_rewrite_of_identical_data_does_not_duplicate(store):
    bars = [make_bar(minute=m) for m in range(3)]
    result1 = store.write_bars(bars)
    assert result1.new_records == 3
    result2 = store.write_bars(bars)
    assert result2.new_records == 0
    assert result2.total_records == 3


def test_overlapping_write_merges_new_records_only(store):
    first_batch = [make_bar(minute=m) for m in range(3)]
    store.write_bars(first_batch)
    second_batch = [make_bar(minute=m) for m in range(2, 5)]  # overlaps at minute=2
    result = store.write_bars(second_batch)
    assert result.new_records == 2  # minutes 3, 4 are new; minute 2 is identical dup
    assert result.total_records == 5


def test_conflicting_overlap_raises_and_does_not_corrupt_existing(store, tmp_path):
    original = make_bar(minute=0, open_t=100000, close_t=100010)
    store.write_bars([original])

    conflicting = make_bar(minute=0, open_t=500000, close_t=500010)
    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([conflicting])

    read = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    assert len(read.bars) == 1
    assert read.bars[0].open_ticks == 100000  # unchanged -- never silently overwritten


def test_partial_write_across_partitions_does_not_corrupt_existing_partition(store):
    """A conflict discovered in one partition (October) must never leave
    another partition in the SAME write_bars call (September) partially
    written -- the two-phase plan-then-commit design."""
    sept_original = make_bar(minute=0, month=9, day=15)
    store.write_bars([sept_original])

    oct_original = make_bar(minute=0, month=10, day=1)
    store.write_bars([oct_original])

    sept_new = make_bar(minute=1, month=9, day=15)
    oct_conflict = make_bar(minute=0, month=10, day=1, open_t=777770, close_t=777780)

    with pytest.raises(HistoricalStorageIntegrityError):
        store.write_bars([sept_new, oct_conflict])

    read_sept = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
    )
    assert len(read_sept.bars) == 1  # sept_new was NOT written


def test_read_half_open_range_inclusive_start_exclusive_end(store):
    bars = [make_bar(minute=m) for m in range(5)]
    store.write_bars(bars)
    read = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 10, 1, tzinfo=UTC), end=datetime(2026, 9, 15, 10, 4, tzinfo=UTC),
    )
    assert [b.ts_event.minute for b in read.bars] == [1, 2, 3]


def test_read_returns_explicit_empty_for_never_written_partition(store):
    read = store.read_bars(
        root_symbol="NQ", contract_year=2099, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2099, 1, 1, tzinfo=UTC), end=datetime(2099, 2, 1, tzinfo=UTC),
    )
    assert read.partitions_found == 0
    assert read.bars == ()
    assert not read.found_any_data


def test_read_distinguishes_partition_exists_but_empty_range_from_never_written(store):
    bar = make_bar(minute=0)
    store.write_bars([bar])
    # Partition file exists (September written), but we ask for a
    # sub-range with no bars in it.
    read = store.read_bars(
        root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
        start=datetime(2026, 9, 15, 23, 0, tzinfo=UTC), end=datetime(2026, 9, 16, 0, 0, tzinfo=UTC),
    )
    assert read.partitions_found == 1
    assert read.bars == ()
    assert not read.found_any_data


def test_write_bars_rejects_non_historicalbar_elements(store):
    with pytest.raises(HistoricalStorageError):
        store.write_bars([{"not": "a bar"}])


def test_write_bars_accepts_empty_sequence(store):
    result = store.write_bars([])
    assert result.partitions_written == 0
    assert result.new_records == 0


@pytest.mark.parametrize("bad_root_dir", [None, 123, object()])
def test_store_rejects_malformed_root_dir(bad_root_dir):
    with pytest.raises(HistoricalStorageError):
        HistoricalBarStore(bad_root_dir)


def test_store_rejects_empty_string_root_dir():
    with pytest.raises(HistoricalStorageError):
        HistoricalBarStore("")


@pytest.mark.parametrize("bad_segment", ["../escape", "a/b", "a\\b", "", "   ", ".", ".."])
def test_partition_dir_rejects_path_traversal_segments(store, bad_segment):
    with pytest.raises(HistoricalStorageError):
        store._partition_dir(
            provider=bad_segment, dataset="GLBX.MDP3", root_symbol="NQ", contract_identity="NQ-2026-12",
            schema_segment="ohlcv-1m", year=2026, month=9,
        )


def test_read_rejects_naive_start(store):
    with pytest.raises(Exception):
        store.read_bars(
            root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 1), end=datetime(2026, 9, 2, tzinfo=UTC),
        )


def test_read_rejects_start_after_end(store):
    with pytest.raises(HistoricalStorageError):
        store.read_bars(
            root_symbol="NQ", contract_year=2026, contract_month=12, timeframe=HistoricalTimeframe.ONE_MINUTE,
            start=datetime(2026, 9, 2, tzinfo=UTC), end=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_atomic_write_leaves_no_temp_file_behind(store, tmp_path):
    bar = make_bar()
    store.write_bars([bar])
    partition_dir = (
        tmp_path / "databento" / "GLBX.MDP3" / "NQ" / "NQ-2026-12" / "ohlcv-1m" / "year=2026" / "month=09"
    )
    leftover_tmp_files = list(partition_dir.glob("*.tmp-*"))
    assert leftover_tmp_files == []
