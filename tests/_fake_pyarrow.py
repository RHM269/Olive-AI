"""A minimal, self-contained fake of the ``pyarrow``/``pyarrow.parquet``
surface that :class:`app.data.storage.HistoricalBarStore` actually uses,
so that ``tests/test_historical_data_storage_transaction.py`` can
exercise the REAL storage orchestration logic (merge, manifest
verification, transactional rollback at both single- and
cross-partition granularity, exception translation, ...) even in an
environment where the real ``pyarrow`` package cannot be installed
(this sandbox could not install it -- see
docs/historical_data.md "Known limitations").

This is explicitly NOT a substitute for real binary Parquet
compatibility testing. ``tests/test_historical_data_storage.py``
(gated by ``pytest.importorskip("pyarrow")``) remains the separately
authoritative suite for that, and is expected to run for real,
unmodified, on any machine where the real package installs (the
user's own machine, CI, ...) -- see docs/historical_data.md §22/§14.

This fake only needs to round-trip exactly the handful of pyarrow
calls ``app.data.storage`` makes (``pa.schema``/``pa.field``/
``pa.string``/``pa.int32``/``pa.int64``/``pa.timestamp``/``pa.array``/
``pa.Table.from_arrays``/``Table.to_pylist``/``pq.write_table``/
``pq.read_table``) well enough for ORCHESTRATION-level behavior, using
plain ``pickle`` serialization of a dict-of-columns rather than any
real columnar/binary Parquet format.
"""

from __future__ import annotations

import pickle
from typing import Any


class _FakeType:
    def __init__(self, name: str) -> None:
        self._name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeType) and other._name == self._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"FakeType({self._name!r})"


def string() -> _FakeType:
    return _FakeType("string")


def int32() -> _FakeType:
    return _FakeType("int32")


def int64() -> _FakeType:
    return _FakeType("int64")


def timestamp(unit: str, tz: object = None) -> _FakeType:
    return _FakeType(f"timestamp[{unit},{tz}]")


class _Field:
    def __init__(self, name: str, type_: _FakeType) -> None:
        self.name = name
        self.type = type_


def field(name: str, type_: _FakeType) -> _Field:
    return _Field(name, type_)


class _Schema:
    def __init__(self, fields: list) -> None:
        self._fields = list(fields)

    def __iter__(self):
        return iter(self._fields)

    def __len__(self) -> int:
        return len(self._fields)


def schema(fields: list) -> _Schema:
    return _Schema(fields)


class _Array:
    def __init__(self, values: list) -> None:
        self.values = list(values)


def array(values: list, type: Any = None) -> _Array:  # noqa: A002 - matches pyarrow's own kwarg name
    return _Array(values)


class Table:
    def __init__(self, columns: dict, schema_: _Schema) -> None:
        self._columns = columns
        self._schema = schema_

    @classmethod
    def from_arrays(cls, arrays: list, schema: _Schema) -> "Table":  # noqa: A002
        columns = {f.name: arr.values for f, arr in zip(schema, arrays)}
        return cls(columns, schema)

    def to_pylist(self) -> list:
        names = [f.name for f in self._schema]
        n = len(next(iter(self._columns.values()))) if self._columns else 0
        return [{name: self._columns[name][i] for name in names} for i in range(n)]


class _ReadTable:
    """What ``parquet.read_table`` hands back -- exposes exactly the
    one method ``app.data.storage`` calls on a real pyarrow Table."""

    def __init__(self, columns: dict, field_names: list) -> None:
        self._columns = columns
        self._field_names = field_names

    def to_pylist(self) -> list:
        n = len(next(iter(self._columns.values()))) if self._columns else 0
        return [{name: self._columns[name][i] for name in self._field_names} for i in range(n)]


class _FakeParquetModule:
    @staticmethod
    def write_table(table: Table, where: Any) -> None:
        payload = pickle.dumps(
            {"columns": table._columns, "field_names": [f.name for f in table._schema]}
        )
        if hasattr(where, "write"):
            where.write(payload)
        else:
            with open(where, "wb") as handle:
                handle.write(payload)

    @staticmethod
    def read_table(path: Any) -> _ReadTable:
        with open(path, "rb") as handle:
            payload = pickle.load(handle)
        return _ReadTable(payload["columns"], payload["field_names"])


parquet = _FakeParquetModule()
