"""Tests for the pyarrow-INDEPENDENT pure-logic helpers in
app.data.storage: ``_months_between`` and ``_normalize_stored_timestamp``.

Unlike tests/test_historical_data_storage.py (which requires the real
``pyarrow`` package and is skipped where it is not installed), this
file imports only plain Python datetime logic that never touches
pyarrow -- importing app.data.storage itself never requires pyarrow
(it is lazily imported only inside ``_import_pyarrow()``), so these
tests run for real, unconditionally, in every environment including
this sandbox. They are part of the Phase 3.1 regression suite for
issues #26 (naive timestamp must fail closed) and #29 (half-open
month-boundary correctness).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.data.storage import HistoricalStorageIntegrityError, _months_between, _normalize_stored_timestamp

UTC = timezone.utc


# -- _months_between (Phase 3.1 §29) -----------------------------------------


def test_months_between_same_month():
    assert _months_between(
        datetime(2026, 9, 15, tzinfo=UTC), datetime(2026, 9, 20, tzinfo=UTC)
    ) == [(2026, 9)]


def test_months_between_spans_multiple_months_normally():
    assert _months_between(
        datetime(2026, 9, 15, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC)
    ) == [(2026, 9), (2026, 10), (2026, 11)]


def test_months_between_excludes_trailing_month_when_end_is_exactly_its_first_instant():
    """The half-open [start, end) interval: an end of exactly
    2026-10-01T00:00:00Z means nothing in October can ever match, so
    October must not be enumerated at all."""
    result = _months_between(datetime(2026, 9, 15, tzinfo=UTC), datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC))
    assert result == [(2026, 9)]
    assert (2026, 10) not in result


def test_months_between_includes_month_when_end_is_one_microsecond_past_its_first_instant():
    result = _months_between(datetime(2026, 9, 15, tzinfo=UTC), datetime(2026, 10, 1, 0, 0, 0, 1, tzinfo=UTC))
    assert result == [(2026, 9), (2026, 10)]


def test_months_between_excludes_trailing_month_across_a_year_boundary():
    """end landing exactly on Jan 1 00:00:00 of the NEXT year must
    exclude January, decrementing across the year boundary to
    December of the previous year."""
    result = _months_between(datetime(2026, 12, 15, tzinfo=UTC), datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC))
    assert result == [(2026, 12)]
    assert (2027, 1) not in result


def test_months_between_single_instant_month_boundary_start_equals_adjusted_end():
    """start exactly at a month's first instant, end exactly at the
    NEXT month's first instant: exactly one month (the start's) is
    enumerated."""
    result = _months_between(datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC), datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC))
    assert result == [(2026, 9)]


# -- _normalize_stored_timestamp (Phase 3.1 §26) -----------------------------


def test_normalize_stored_timestamp_accepts_aware_utc():
    value = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    assert _normalize_stored_timestamp(value) == value


def test_normalize_stored_timestamp_converts_non_utc_aware_to_utc():
    from datetime import timedelta, timezone as tz_mod

    plus_five = tz_mod(timedelta(hours=5))
    value = datetime(2026, 9, 15, 15, 0, tzinfo=plus_five)
    normalized = _normalize_stored_timestamp(value)
    assert normalized.tzinfo is not None
    assert normalized.utcoffset() == timezone.utc.utcoffset(None)
    assert normalized == datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


def test_normalize_stored_timestamp_rejects_naive_datetime_fails_closed():
    """Phase 3.1 §26, the critical regression: a naive timestamp read
    back from storage must never be silently assumed UTC -- it must
    raise, treating the data as corrupt/incompatible."""
    naive = datetime(2026, 9, 15, 10, 0)  # no tzinfo
    with pytest.raises(HistoricalStorageIntegrityError):
        _normalize_stored_timestamp(naive)


@pytest.mark.parametrize("bad", [None, "2026-09-15", 123, object()])
def test_normalize_stored_timestamp_rejects_non_datetime_values(bad):
    with pytest.raises(HistoricalStorageIntegrityError):
        _normalize_stored_timestamp(bad)
