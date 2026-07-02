# cjson.py
from __future__ import annotations

import json
from typing import Any, Iterable, Iterator, TextIO


# ---------------------------------------------------------------------------
# Wire-format constants
# ---------------------------------------------------------------------------
#
# This library stores a list of dictionaries as a compact table-like JSON:
#
#   {
#       "$c": ["fieldA", "fieldB"],
#       "$r": [
#           [1, 2],
#           [3, 4]
#       ]
#   }
#
# Instead of repeating object keys in every row:
#
#   [
#       {"fieldA": 1, "fieldB": 2},
#       {"fieldA": 3, "fieldB": 4}
#   ]
#
# The short keys are intentional. They reduce payload size and are still
# readable enough when inspected manually.
#
# "$c" = columns
# "$r" = rows
# "$i" = indexes
# "$t" = type marker
# "$m" = missing value marker
#
# Index sections:
#
# "$i": {
#     "b": { ... },   # by-value indexes
#     "s": { ... },   # sort-order indexes
#     "x": { ... }    # min/max indexes
# }
#
# Indexes use column positions as keys, not column names. This is smaller:
#
#   "0" instead of "monitorId"
#
# The column name can always be resolved through "$c".
# ---------------------------------------------------------------------------

C = "$c"
R = "$r"
I = "$i"

BY = "b"
SORT = "s"
MINMAX = "x"

T = "$t"
M = "$m"


# ---------------------------------------------------------------------------
# Internal missing-value sentinel
# ---------------------------------------------------------------------------
#
# JSON has `null`, but `null` means Python `None`.
#
# Missing field is semantically different from:
#
#   {"a": None}
#
# Example:
#
#   [
#       {"a": 1, "b": None},
#       {"a": 2}
#   ]
#
# The second row does not contain "b". We must not restore it as:
#
#   {"a": 2, "b": None}
#
# Therefore missing values are encoded explicitly as:
#
#   {"$t":"$m"}
#
# This is slightly more verbose, but only appears when rows have uneven shape.
# For uniform telemetry/logging records it usually never appears.
# ---------------------------------------------------------------------------

class _Missing:
    pass


_MISSING = _Missing()


# ---------------------------------------------------------------------------
# Value key encoding for by-value indexes
# ---------------------------------------------------------------------------
#
# JSON object keys must be strings. Index values may be bool/int/str/None/etc.
# We encode indexed values with a type prefix so these do not collide:
#
#   True      -> "b:true"
#   "true"    -> "s:true"
#   1         -> "i:1"
#   "1"       -> "s:1"
#   None      -> "n:null"
#
# For complex values we fallback to canonical compact JSON:
#
#   {"a":1}   -> "j:{\"a\":1}"
#
# In practice, index simple scalar columns only.
# ---------------------------------------------------------------------------

def _index_key(value: Any) -> str:
    """Convert a Python value into a stable string key for JSON indexes."""

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

    return "j:" + json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_index_key(key: str) -> Any:
    """Decode a value previously encoded with _index_key()."""

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


# ---------------------------------------------------------------------------
# Shape detection
# ---------------------------------------------------------------------------
#
# A "record list" is a non-empty list where every item is a dictionary.
# Such list can be encoded as "$c/$r".
#
# We intentionally skip dictionaries containing reserved transport keys:
#
#   "$c", "$r", "$i", "$t", "$m"
#
# This avoids ambiguity between user data and the cjson transport format.
# If your application must support user fields named "$c" or "$r", use a
# namespace/escaping strategy before passing data into this library.
# ---------------------------------------------------------------------------

def _is_reserved_key(key: Any) -> bool:
    return isinstance(key, str) and key in {C, R, I, T, M}


def _is_plain_record(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and not any(_is_reserved_key(key) for key in obj.keys())
    )


def _is_record_list(obj: Any) -> bool:
    return (
        isinstance(obj, list)
        and len(obj) > 0
        and all(_is_plain_record(item) for item in obj)
    )


def _is_packed_table(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and C in obj
        and R in obj
        and isinstance(obj[C], list)
        and isinstance(obj[R], list)
    )


def _is_missing_marker(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get(T) == M


# ---------------------------------------------------------------------------
# Column handling
# ---------------------------------------------------------------------------
#
# Column order is deterministic and human-friendly:
#
#   first occurrence wins
#
# Example:
#
#   [{"b": 1, "a": 2}, {"c": 3}]
#
# produces:
#
#   "$c": ["b", "a", "c"]
#
# This avoids alphabetic sorting, which can be surprising and may slightly
# reduce readability when records are inspected by humans.
# ---------------------------------------------------------------------------

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
    return {column: position for position, column in enumerate(columns)}


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------
#
# Three index types are supported:
#
# 1. By-value index:
#
#      "$i": {
#          "b": {
#              "0": {
#                  "s:monitor-1": [0, 2, 3],
#                  "s:monitor-2": [1]
#              }
#          }
#      }
#
#    Meaning:
#      column 0 value "monitor-1" appears in rows 0, 2, 3.
#
#    Good for:
#      monitorId, ok, status, type, region, serviceName
#
#
# 2. Sort index:
#
#      "$i": {
#          "s": {
#              "4": [3, 1, 0, 2]
#          }
#      }
#
#    Meaning:
#      rows sorted by column 4 should be read in this order.
#
#    Good for:
#      checkedTs, totalResponseTimeMs, createdAt timestamp
#
#
# 3. Min/max index:
#
#      "$i": {
#          "x": {
#              "4": [1778883489, 1778885416]
#          }
#      }
#
#    Meaning:
#      column 4 has min/max values.
#
#    Good for:
#      quick range checks before scanning rows.
#
# Important:
#   Indexes increase payload size. Add them only for columns that are queried
#   frequently. For small payloads, scanning rows may be cheaper.
# ---------------------------------------------------------------------------

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

        position = positions[column]
        position_key = str(position)
        value_to_rows: dict[str, list[int]] = {}

        for row_id, row in enumerate(rows):
            if position >= len(row):
                continue

            value = row[position]

            if _is_missing_marker(value):
                continue

            key = _index_key(value)
            value_to_rows.setdefault(key, []).append(row_id)

        by_indexes[position_key] = value_to_rows

    if by_indexes:
        indexes[BY] = by_indexes

    sort_indexes: dict[str, list[int]] = {}

    for column in index_sort:
        if column not in positions:
            continue

        position = positions[column]
        position_key = str(position)

        def sort_key(row_id: int) -> tuple[int, Any]:
            value = rows[row_id][position]

            # Missing values should sort last.
            if _is_missing_marker(value):
                return (1, None)

            return (0, value)

        try:
            ordered_row_ids = sorted(range(len(rows)), key=sort_key)
        except TypeError:
            # Mixed Python types cannot always be compared directly:
            #
            #   1 < "abc"  # TypeError
            #
            # Fallback to repr() gives deterministic ordering, even if it is
            # not semantically perfect.
            ordered_row_ids = sorted(
                range(len(rows)),
                key=lambda row_id: repr(rows[row_id][position]),
            )

        sort_indexes[position_key] = ordered_row_ids

    if sort_indexes:
        indexes[SORT] = sort_indexes

    minmax_indexes: dict[str, list[Any]] = {}

    for column in index_minmax:
        if column not in positions:
            continue

        position = positions[column]
        position_key = str(position)

        values = [
            row[position]
            for row in rows
            if position < len(row) and not _is_missing_marker(row[position])
        ]

        if not values:
            continue

        try:
            minmax_indexes[position_key] = [min(values), max(values)]
        except TypeError:
            # Min/max makes sense only for mutually comparable values.
            # Mixed values are ignored.
            pass

    if minmax_indexes:
        indexes[MINMAX] = minmax_indexes

    return indexes


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------
#
# pack() converts normal Python data into compact cjson-compatible data.
#
# Primary case:
#
#   [
#       {"monitorId": "m1", "ok": True},
#       {"monitorId": "m2", "ok": False}
#   ]
#
# becomes:
#
#   {
#       "$c": ["monitorId", "ok"],
#       "$r": [
#           ["m1", true],
#           ["m2", false]
#       ]
#   }
#
# recursive=True means that nested record lists are also packed.
#
# Example:
#
#   {
#       "monitorId": "m1",
#       "checks": [
#           {"name": "http", "ok": True},
#           {"name": "dns", "ok": True}
#       ]
#   }
#
# The "checks" list will also become a "$c/$r" table.
# ---------------------------------------------------------------------------

def pack(
    obj: Any,
    *,
    recursive: bool = True,
    index_by: Iterable[str] = (),
    index_sort: Iterable[str] = (),
    index_minmax: Iterable[str] = (),
) -> Any:
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

        packed: dict[str, Any] = {
            C: columns,
            R: rows,
        }

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
            str(key): pack(
                value,
                recursive=recursive,
                index_by=index_by,
                index_sort=index_sort,
                index_minmax=index_minmax,
            )
            for key, value in obj.items()
        }

    if recursive and isinstance(obj, list):
        return [
            pack(
                value,
                recursive=recursive,
                index_by=index_by,
                index_sort=index_sort,
                index_minmax=index_minmax,
            )
            for value in obj
        ]

    return obj


# ---------------------------------------------------------------------------
# Unpacking
# ---------------------------------------------------------------------------
#
# unpack() restores normal Python dictionaries/lists.
#
# Indexes are intentionally ignored during unpacking because they are transport
# metadata, not user data.
#
# Missing markers are skipped, not converted to None.
# ---------------------------------------------------------------------------

def unpack(obj: Any) -> Any:
    if _is_missing_marker(obj):
        return _MISSING

    if _is_packed_table(obj):
        columns = obj[C]
        records: list[dict[str, Any]] = []

        for row in obj[R]:
            record: dict[str, Any] = {}

            for column, value in zip(columns, row):
                unpacked_value = unpack(value)

                if unpacked_value is not _MISSING:
                    record[column] = unpacked_value

            records.append(record)

        return records

    if isinstance(obj, dict):
        return {
            key: unpack(value)
            for key, value in obj.items()
            if key != I
        }

    if isinstance(obj, list):
        return [unpack(value) for value in obj]

    return obj


# ---------------------------------------------------------------------------
# json-like API
# ---------------------------------------------------------------------------
#
# These functions mimic the standard json module:
#
#   cjson.dumps(...)
#   cjson.loads(...)
#   cjson.dump(...)
#   cjson.load(...)
#
# dumps() and dump() pack before writing.
# loads() and load() unpack after reading.
# ---------------------------------------------------------------------------

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
    packed = json.loads(s, **json_kwargs)
    return unpack(packed)


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


# ---------------------------------------------------------------------------
# Low-level packed-table helpers
# ---------------------------------------------------------------------------
#
# These functions operate on packed data directly, before full unpacking.
# This is useful when the payload is large and you only need a small subset.
# ---------------------------------------------------------------------------

def is_packed_table(obj: Any) -> bool:
    """Return True if obj looks like a cjson packed table."""

    return _is_packed_table(obj)


def column_index(packed: dict[str, Any], column: str) -> int:
    """Return numeric position of a column in a packed table."""

    return packed[C].index(column)


def get_column(packed: dict[str, Any], column: str) -> list[Any]:
    """
    Extract one column from a packed table.

    This avoids building full dictionaries for every row.
    """

    position = column_index(packed, column)

    return [
        unpack(row[position])
        for row in packed[R]
        if position < len(row) and not _is_missing_marker(row[position])
    ]


def row_to_record(packed: dict[str, Any], row: list[Any]) -> dict[str, Any]:
    """Convert a single packed row into a normal dictionary."""

    record: dict[str, Any] = {}

    for column, value in zip(packed[C], row):
        unpacked_value = unpack(value)

        if unpacked_value is not _MISSING:
            record[column] = unpacked_value

    return record


def iter_records(packed: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Iterate normal dictionaries from a packed table."""

    for row in packed[R]:
        yield row_to_record(packed, row)


# ---------------------------------------------------------------------------
# Indexed filtering
# ---------------------------------------------------------------------------
#
# get_row_ids_by_value() first tries to use "$i.b".
# If the index is missing, it falls back to scanning rows.
#
# This means callers can use the same API regardless of whether the producer
# included indexes.
# ---------------------------------------------------------------------------

def get_row_ids_by_value(
    packed: dict[str, Any],
    column: str,
    value: Any,
) -> list[int]:
    position = column_index(packed, column)
    position_key = str(position)
    value_key = _index_key(value)

    indexed = (
        packed
        .get(I, {})
        .get(BY, {})
        .get(position_key, {})
        .get(value_key)
    )

    if indexed is not None:
        return list(indexed)

    row_ids: list[int] = []

    for row_id, row in enumerate(packed[R]):
        if position >= len(row):
            continue

        cell = row[position]

        if _is_missing_marker(cell):
            continue

        if unpack(cell) == value:
            row_ids.append(row_id)

    return row_ids


def get_rows_by_value(
    packed: dict[str, Any],
    column: str,
    value: Any,
) -> list[list[Any]]:
    """Return packed rows matching column == value."""

    row_ids = get_row_ids_by_value(packed, column, value)
    rows = packed[R]

    return [rows[row_id] for row_id in row_ids]


def get_records_by_value(
    packed: dict[str, Any],
    column: str,
    value: Any,
) -> list[dict[str, Any]]:
    """Return normal dictionaries matching column == value."""

    return [
        row_to_record(packed, row)
        for row in get_rows_by_value(packed, column, value)
    ]


# ---------------------------------------------------------------------------
# Range filtering
# ---------------------------------------------------------------------------
#
# Min/max metadata can quickly reject impossible range queries.
#
# Example:
#
#   checkedTs min/max = [100, 200]
#
# Query:
#
#   checkedTs between 300 and 400
#
# can return immediately without scanning rows.
#
# If the range overlaps min/max, we still scan rows because min/max only tells
# us the global table boundary, not exact matching row ids.
# ---------------------------------------------------------------------------

def maybe_range_exists(
    packed: dict[str, Any],
    column: str,
    *,
    min_value: Any | None = None,
    max_value: Any | None = None,
) -> bool:
    position = column_index(packed, column)
    position_key = str(position)

    bounds = (
        packed
        .get(I, {})
        .get(MINMAX, {})
        .get(position_key)
    )

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
    """
    Return records where:

        min_value <= column <= max_value

    Either bound may be omitted.
    """

    if not maybe_range_exists(
        packed,
        column,
        min_value=min_value,
        max_value=max_value,
    ):
        return []

    position = column_index(packed, column)
    result: list[dict[str, Any]] = []

    for row in packed[R]:
        if position >= len(row):
            continue

        value = row[position]

        if _is_missing_marker(value):
            continue

        value = unpack(value)

        if min_value is not None and value < min_value:
            continue

        if max_value is not None and value > max_value:
            continue

        result.append(row_to_record(packed, row))

    return result


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------
#
# iter_sorted_records() first tries to use "$i.s".
# If the sort index is missing, it sorts row ids on demand.
#
# Precomputed sort indexes are useful when:
#
#   - payload is queried multiple times,
#   - clients are weak,
#   - sorting columns are known in advance,
#   - row count is large.
# ---------------------------------------------------------------------------

def get_sorted_row_ids(
    packed: dict[str, Any],
    column: str,
    *,
    reverse: bool = False,
) -> list[int]:
    position = column_index(packed, column)
    position_key = str(position)

    indexed = (
        packed
        .get(I, {})
        .get(SORT, {})
        .get(position_key)
    )

    if indexed is not None:
        row_ids = list(indexed)
    else:
        rows = packed[R]

        def sort_key(row_id: int) -> tuple[int, Any]:
            value = rows[row_id][position]

            if _is_missing_marker(value):
                return (1, None)

            return (0, unpack(value))

        try:
            row_ids = sorted(range(len(rows)), key=sort_key)
        except TypeError:
            row_ids = sorted(
                range(len(rows)),
                key=lambda row_id: repr(rows[row_id][position]),
            )

    if reverse:
        row_ids.reverse()

    return row_ids


def iter_sorted_records(
    packed: dict[str, Any],
    column: str,
    *,
    reverse: bool = False,
) -> Iterator[dict[str, Any]]:
    """Iterate records sorted by a column."""

    rows = packed[R]

    for row_id in get_sorted_row_ids(packed, column, reverse=reverse):
        yield row_to_record(packed, rows[row_id])


# ---------------------------------------------------------------------------
# Convenience helpers for working with raw JSON strings
# ---------------------------------------------------------------------------

def loads_packed(s: str | bytes | bytearray, **json_kwargs: Any) -> Any:
    """
    Parse JSON but do not unpack it.

    Use this when you want to query indexes directly.
    """

    return json.loads(s, **json_kwargs)


def dumps_packed(obj: Any, **json_kwargs: Any) -> str:
    """
    Dump already-packed data.

    This does not call pack().
    """

    json_kwargs.setdefault("separators", (",", ":"))
    json_kwargs.setdefault("ensure_ascii", False)

    return json.dumps(obj, **json_kwargs)

# ---------------------------------------------------------------------------
# Escape hatch / decompression helpers
# ---------------------------------------------------------------------------
#
# These functions are the "escape hatch" from cjson back to regular JSON.
#
# They are intentionally boring and explicit:
#
#   - to_normal() converts already-loaded packed data to regular Python data.
#   - dumps_normal() converts packed data to a regular JSON string.
#   - load_normal() reads a cjson file and returns regular Python data.
#   - dump_normal() writes regular JSON, not cjson.
#   - convert_file_to_normal_json() converts a cjson file into a normal JSON file.
#
# This is useful when:
#
#   - another system does not understand cjson,
#   - debugging with standard JSON tooling,
#   - migrating away from cjson,
#   - exporting data to humans,
#   - using jq, pandas, Spark, BigQuery importers, etc.
#
# Important:
#   These functions remove transport metadata such as "$i" indexes.
#   The output is semantically normal JSON data.
# ---------------------------------------------------------------------------

def to_normal(obj: Any) -> Any:
    """
    Convert cjson-packed data into regular Python data.

    This is an explicit alias for unpack(), provided as an escape-hatch API.

    Example:
        packed = {
            "$c": ["id", "name"],
            "$r": [[1, "A"], [2, "B"]]
        }

        normal = to_normal(packed)

        assert normal == [
            {"id": 1, "name": "A"},
            {"id": 2, "name": "B"},
        ]
    """

    return unpack(obj)


def dumps_normal(
    obj: Any,
    *,
    compact: bool = True,
    **json_kwargs: Any,
) -> str:
    """
    Serialize cjson-packed data as normal JSON.

    Unlike dumps(), this function does NOT pack the object.
    It first unpacks cjson structures and then writes regular JSON.

    Example:
        packed = {
            "$c": ["id", "name"],
            "$r": [[1, "A"], [2, "B"]]
        }

        s = dumps_normal(packed)

        # s is:
        # [{"id":1,"name":"A"},{"id":2,"name":"B"}]
    """

    normal = to_normal(obj)

    if compact:
        json_kwargs.setdefault("separators", (",", ":"))

    json_kwargs.setdefault("ensure_ascii", False)

    return json.dumps(normal, **json_kwargs)


def loads_normal(
    s: str | bytes | bytearray,
    *,
    compact_output: bool | None = None,
    **json_kwargs: Any,
) -> Any:
    """
    Parse a JSON string and return normal Python data.

    If the input is cjson-packed, it is unpacked.
    If the input is already normal JSON, it is returned as normal Python data.

    compact_output exists only for API symmetry and future compatibility.
    It is ignored because this function returns Python data, not a string.
    """

    parsed = json.loads(s, **json_kwargs)
    return to_normal(parsed)


def dump_normal(
    obj: Any,
    fp: TextIO,
    *,
    compact: bool = True,
    **json_kwargs: Any,
) -> None:
    """
    Write cjson-packed data as normal JSON into a file-like object.

    This is the file-based version of dumps_normal().
    """

    fp.write(
        dumps_normal(
            obj,
            compact=compact,
            **json_kwargs,
        )
    )


def load_normal(fp: TextIO, **json_kwargs: Any) -> Any:
    """
    Read a JSON file and return normal Python data.

    The file may contain either:
        - cjson-packed JSON,
        - regular JSON.

    The returned value is always normal Python data.
    """

    parsed = json.load(fp, **json_kwargs)
    return to_normal(parsed)


def convert_file_to_normal_json(
    src_path: str,
    dst_path: str,
    *,
    compact: bool = True,
    encoding: str = "utf-8",
    **json_kwargs: Any,
) -> None:
    """
    Convert a cjson file into a regular JSON file.

    Example:
        convert_file_to_normal_json(
            "data.cjson",
            "data.normal.json",
        )

    The destination file will not contain:
        - "$c"
        - "$r"
        - "$i"
        - "$t":"$m"

    It will contain ordinary JSON objects and arrays.
    """

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
    """
    Convert a cjson file into pretty-printed regular JSON.

    This is mainly for debugging, manual inspection, exports, and tests.
    """

    json_kwargs.setdefault("indent", indent)
    json_kwargs.setdefault("ensure_ascii", False)

    with open(src_path, "r", encoding=encoding) as src:
        normal = load_normal(src)

    with open(dst_path, "w", encoding=encoding) as dst:
        json.dump(normal, dst, **json_kwargs)


def is_cjson(obj: Any) -> bool:
    """
    Return True if the object contains at least one cjson-packed table.

    This performs a recursive check.

    Useful when a caller receives unknown JSON and wants to know whether
    cjson decompression is needed.
    """

    if _is_packed_table(obj):
        return True

    if isinstance(obj, dict):
        return any(is_cjson(value) for value in obj.values())

    if isinstance(obj, list):
        return any(is_cjson(value) for value in obj)

    return False


def normalize_json_string(
    s: str | bytes | bytearray,
    *,
    compact: bool = True,
    **json_kwargs: Any,
) -> str:
    """
    Accept either cjson or regular JSON and always return regular JSON string.

    This is the most direct "escape hatch" for integrations.

    Example:
        normal_json = normalize_json_string(possibly_cjson_payload)
    """

    parsed = json.loads(s)

    normal = to_normal(parsed)

    if compact:
        json_kwargs.setdefault("separators", (",", ":"))

    json_kwargs.setdefault("ensure_ascii", False)

    return json.dumps(normal, **json_kwargs)
