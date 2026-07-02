from __future__ import annotations

"""Legacy batch cjson format: list[dict] <-> {"$c", "$r"}."""

import gzip
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterable, Iterator, Mapping, TextIO


# =============================================================================
# Legacy batch cjson format
# =============================================================================

C = "$c"
R = "$r"
I = "$i"

BY = "b"
SORT = "s"
MINMAX = "x"

T = "$t"
M = "$m"


class _Missing:
    pass


_MISSING = _Missing()


def _index_key(value: Any) -> str:
    if value is None:
        return "n:null"
    if value is True:
        return "b:true"
    if value is False:
        return "b:false"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"i:{value}"
    if isinstance(value, float):
        return f"f:{value!r}"
    if isinstance(value, str):
        return f"s:{value}"
    return "j:" + json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_index_key(key: str) -> Any:
    prefix, _, value = key.partition(":")
    if prefix == "n":
        return None
    if prefix == "b":
        return value == "true"
    if prefix == "i":
        return int(value)
    if prefix == "f":
        return float(value)
    if prefix == "s":
        return value
    if prefix == "j":
        return json.loads(value)
    return key


def _is_reserved_key(key: Any) -> bool:
    return isinstance(key, str) and key in {C, R, I, T, M}


def _is_plain_record(obj: Any) -> bool:
    return isinstance(obj, dict) and not any(_is_reserved_key(k) for k in obj.keys())


def _is_record_list(obj: Any) -> bool:
    return isinstance(obj, list) and len(obj) > 0 and all(_is_plain_record(item) for item in obj)


def _is_packed_table(obj: Any) -> bool:
    return isinstance(obj, dict) and C in obj and R in obj and isinstance(obj[C], list) and isinstance(obj[R], list)


def _is_missing_marker(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get(T) == M


def _collect_columns(records: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    columns: list[str] = []
    for record in records:
        for raw_key in record.keys():
            key = str(raw_key)
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def _column_positions(columns: list[str]) -> dict[str, int]:
    return {column: i for i, column in enumerate(columns)}


def _build_indexes(
    columns: list[str],
    rows: list[list[Any]],
    *,
    index_by: Iterable[str] = (),
    index_sort: Iterable[str] = (),
    index_minmax: Iterable[str] = (),
) -> dict[str, Any]:
    positions = _column_positions(columns)
    indexes: dict[str, Any] = {}

    by_indexes: dict[str, dict[str, list[int]]] = {}
    for column in index_by:
        if column not in positions:
            continue
        pos = positions[column]
        pos_key = str(pos)
        value_to_rows: dict[str, list[int]] = {}
        for row_id, row in enumerate(rows):
            if pos >= len(row):
                continue
            value = row[pos]
            if _is_missing_marker(value):
                continue
            value_to_rows.setdefault(_index_key(value), []).append(row_id)
        by_indexes[pos_key] = value_to_rows
    if by_indexes:
        indexes[BY] = by_indexes

    sort_indexes: dict[str, list[int]] = {}
    for column in index_sort:
        if column not in positions:
            continue
        pos = positions[column]
        pos_key = str(pos)

        def sort_key(row_id: int) -> tuple[int, Any]:
            value = rows[row_id][pos]
            if _is_missing_marker(value):
                return (1, None)
            return (0, value)

        try:
            ordered = sorted(range(len(rows)), key=sort_key)
        except TypeError:
            ordered = sorted(range(len(rows)), key=lambda row_id: repr(rows[row_id][pos]))
        sort_indexes[pos_key] = ordered
    if sort_indexes:
        indexes[SORT] = sort_indexes

    minmax_indexes: dict[str, list[Any]] = {}
    for column in index_minmax:
        if column not in positions:
            continue
        pos = positions[column]
        values = [row[pos] for row in rows if pos < len(row) and not _is_missing_marker(row[pos])]
        if not values:
            continue
        try:
            minmax_indexes[str(pos)] = [min(values), max(values)]
        except TypeError:
            pass
    if minmax_indexes:
        indexes[MINMAX] = minmax_indexes

    return indexes


def pack(
    obj: Any,
    *,
    recursive: bool = True,
    index_by: Iterable[str] = (),
    index_sort: Iterable[str] = (),
    index_minmax: Iterable[str] = (),
) -> Any:
    """Pack list[dict] into legacy cjson {"$c","$r"} format."""
    if _is_record_list(obj):
        records: list[dict[str, Any]] = obj
        columns = _collect_columns(records)
        rows: list[list[Any]] = []
        for record in records:
            row: list[Any] = []
            for column in columns:
                if column in record:
                    value = record[column]
                    if recursive:
                        value = pack(
                            value,
                            recursive=recursive,
                            index_by=index_by,
                            index_sort=index_sort,
                            index_minmax=index_minmax,
                        )
                    row.append(value)
                else:
                    row.append({T: M})
            rows.append(row)

        packed: dict[str, Any] = {C: columns, R: rows}
        indexes = _build_indexes(
            columns,
            rows,
            index_by=index_by,
            index_sort=index_sort,
            index_minmax=index_minmax,
        )
        if indexes:
            packed[I] = indexes
        return packed

    if recursive and isinstance(obj, dict):
        return {
            str(k): pack(v, recursive=recursive, index_by=index_by, index_sort=index_sort, index_minmax=index_minmax)
            for k, v in obj.items()
        }
    if recursive and isinstance(obj, list):
        return [pack(v, recursive=recursive, index_by=index_by, index_sort=index_sort, index_minmax=index_minmax) for v in obj]
    return obj


def unpack(obj: Any) -> Any:
    """Unpack legacy cjson data into normal Python values."""
    if _is_missing_marker(obj):
        return _MISSING

    if _is_packed_table(obj):
        columns = obj[C]
        records: list[dict[str, Any]] = []
        for row in obj[R]:
            record: dict[str, Any] = {}
            for column, value in zip(columns, row):
                unpacked = unpack(value)
                if unpacked is not _MISSING:
                    record[column] = unpacked
            records.append(record)
        return records

    if isinstance(obj, dict):
        return {k: unpack(v) for k, v in obj.items() if k != I}
    if isinstance(obj, list):
        return [unpack(v) for v in obj]
    return obj


def dumps(
    obj: Any,
    *,
    recursive: bool = True,
    compact: bool = True,
    index_by: Iterable[str] = (),
    index_sort: Iterable[str] = (),
    index_minmax: Iterable[str] = (),
    **json_kwargs: Any,
) -> str:
    """json.dumps-like API: pack first, then serialize."""
    packed = pack(
        obj,
        recursive=recursive,
        index_by=index_by,
        index_sort=index_sort,
        index_minmax=index_minmax,
    )
    if compact:
        json_kwargs.setdefault("separators", (",", ":"))
    json_kwargs.setdefault("ensure_ascii", False)
    return json.dumps(packed, **json_kwargs)


def loads(s: str | bytes | bytearray, **json_kwargs: Any) -> Any:
    """json.loads-like API: parse first, then unpack."""
    return unpack(json.loads(s, **json_kwargs))


def dump(
    obj: Any,
    fp: TextIO,
    *,
    recursive: bool = True,
    compact: bool = True,
    index_by: Iterable[str] = (),
    index_sort: Iterable[str] = (),
    index_minmax: Iterable[str] = (),
    **json_kwargs: Any,
) -> None:
    fp.write(
        dumps(
            obj,
            recursive=recursive,
            compact=compact,
            index_by=index_by,
            index_sort=index_sort,
            index_minmax=index_minmax,
            **json_kwargs,
        )
    )


def load(fp: TextIO, **json_kwargs: Any) -> Any:
    return loads(fp.read(), **json_kwargs)


def is_packed_table(obj: Any) -> bool:
    return _is_packed_table(obj)


def column_index(packed: dict[str, Any], column: str) -> int:
    return packed[C].index(column)


def get_column(packed: dict[str, Any], column: str) -> list[Any]:
    pos = column_index(packed, column)
    return [unpack(row[pos]) for row in packed[R] if pos < len(row) and not _is_missing_marker(row[pos])]


def row_to_record(packed: dict[str, Any], row: list[Any]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for column, value in zip(packed[C], row):
        unpacked = unpack(value)
        if unpacked is not _MISSING:
            record[column] = unpacked
    return record


def iter_records(packed: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Iterate records from legacy packed table."""
    for row in packed[R]:
        yield row_to_record(packed, row)


def get_row_ids_by_value(packed: dict[str, Any], column: str, value: Any) -> list[int]:
    pos = column_index(packed, column)
    pos_key = str(pos)
    value_key = _index_key(value)
    indexed = packed.get(I, {}).get(BY, {}).get(pos_key, {}).get(value_key)
    if indexed is not None:
        return list(indexed)

    out: list[int] = []
    for row_id, row in enumerate(packed[R]):
        if pos >= len(row):
            continue
        cell = row[pos]
        if _is_missing_marker(cell):
            continue
        if unpack(cell) == value:
            out.append(row_id)
    return out


def get_rows_by_value(packed: dict[str, Any], column: str, value: Any) -> list[list[Any]]:
    rows = packed[R]
    return [rows[row_id] for row_id in get_row_ids_by_value(packed, column, value)]


def get_records_by_value(packed: dict[str, Any], column: str, value: Any) -> list[dict[str, Any]]:
    return [row_to_record(packed, row) for row in get_rows_by_value(packed, column, value)]


def maybe_range_exists(
    packed: dict[str, Any],
    column: str,
    *,
    min_value: Any | None = None,
    max_value: Any | None = None,
) -> bool:
    pos = column_index(packed, column)
    bounds = packed.get(I, {}).get(MINMAX, {}).get(str(pos))
    if not bounds:
        return True
    table_min, table_max = bounds
    if min_value is not None and table_max < min_value:
        return False
    if max_value is not None and table_min > max_value:
        return False
    return True


def get_records_in_range(
    packed: dict[str, Any],
    column: str,
    *,
    min_value: Any | None = None,
    max_value: Any | None = None,
) -> list[dict[str, Any]]:
    if not maybe_range_exists(packed, column, min_value=min_value, max_value=max_value):
        return []
    pos = column_index(packed, column)
    out: list[dict[str, Any]] = []
    for row in packed[R]:
        if pos >= len(row):
            continue
        value = row[pos]
        if _is_missing_marker(value):
            continue
        value = unpack(value)
        if min_value is not None and value < min_value:
            continue
        if max_value is not None and value > max_value:
            continue
        out.append(row_to_record(packed, row))
    return out


def get_sorted_row_ids(packed: dict[str, Any], column: str, *, reverse: bool = False) -> list[int]:
    pos = column_index(packed, column)
    indexed = packed.get(I, {}).get(SORT, {}).get(str(pos))
    if indexed is not None:
        row_ids = list(indexed)
    else:
        rows = packed[R]

        def sort_key(row_id: int) -> tuple[int, Any]:
            value = rows[row_id][pos]
            if _is_missing_marker(value):
                return (1, None)
            return (0, unpack(value))

        try:
            row_ids = sorted(range(len(rows)), key=sort_key)
        except TypeError:
            row_ids = sorted(range(len(rows)), key=lambda row_id: repr(rows[row_id][pos]))
    if reverse:
        row_ids.reverse()
    return row_ids


def iter_sorted_records(packed: dict[str, Any], column: str, *, reverse: bool = False) -> Iterator[dict[str, Any]]:
    rows = packed[R]
    for row_id in get_sorted_row_ids(packed, column, reverse=reverse):
        yield row_to_record(packed, rows[row_id])


def loads_packed(s: str | bytes | bytearray, **json_kwargs: Any) -> Any:
    return json.loads(s, **json_kwargs)


def dumps_packed(obj: Any, **json_kwargs: Any) -> str:
    json_kwargs.setdefault("separators", (",", ":"))
    json_kwargs.setdefault("ensure_ascii", False)
    return json.dumps(obj, **json_kwargs)


def to_normal(obj: Any) -> Any:
    return unpack(obj)


def dumps_normal(obj: Any, *, compact: bool = True, **json_kwargs: Any) -> str:
    normal = to_normal(obj)
    if compact:
        json_kwargs.setdefault("separators", (",", ":"))
    json_kwargs.setdefault("ensure_ascii", False)
    return json.dumps(normal, **json_kwargs)


def loads_normal(s: str | bytes | bytearray, *, compact_output: bool | None = None, **json_kwargs: Any) -> Any:
    del compact_output
    return to_normal(json.loads(s, **json_kwargs))


def dump_normal(obj: Any, fp: TextIO, *, compact: bool = True, **json_kwargs: Any) -> None:
    fp.write(dumps_normal(obj, compact=compact, **json_kwargs))


def load_normal(fp: TextIO, **json_kwargs: Any) -> Any:
    return to_normal(json.load(fp, **json_kwargs))


def convert_file_to_normal_json(
    src_path: str,
    dst_path: str,
    *,
    compact: bool = True,
    encoding: str = "utf-8",
    **json_kwargs: Any,
) -> None:
    with open(src_path, "r", encoding=encoding) as src:
        normal = load_normal(src)
    if compact:
        json_kwargs.setdefault("separators", (",", ":"))
    json_kwargs.setdefault("ensure_ascii", False)
    with open(dst_path, "w", encoding=encoding) as dst:
        json.dump(normal, dst, **json_kwargs)


def convert_file_to_pretty_normal_json(
    src_path: str,
    dst_path: str,
    *,
    indent: int = 2,
    encoding: str = "utf-8",
    **json_kwargs: Any,
) -> None:
    json_kwargs.setdefault("indent", indent)
    json_kwargs.setdefault("ensure_ascii", False)
    with open(src_path, "r", encoding=encoding) as src:
        normal = load_normal(src)
    with open(dst_path, "w", encoding=encoding) as dst:
        json.dump(normal, dst, **json_kwargs)


def is_cjson(obj: Any) -> bool:
    if _is_packed_table(obj):
        return True
    if isinstance(obj, dict):
        return any(is_cjson(v) for v in obj.values())
    if isinstance(obj, list):
        return any(is_cjson(v) for v in obj)
    return False


def normalize_json_string(s: str | bytes | bytearray, *, compact: bool = True, **json_kwargs: Any) -> str:
    normal = to_normal(json.loads(s))
    if compact:
        json_kwargs.setdefault("separators", (",", ":"))
    json_kwargs.setdefault("ensure_ascii", False)
    return json.dumps(normal, **json_kwargs)


