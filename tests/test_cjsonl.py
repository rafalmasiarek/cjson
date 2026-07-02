"""Comprehensive cjsonl tests: recovery, lifecycle, schema-less, round-trips."""

import json
import os
import tempfile

import pytest

import cjson


# ── shared fixtures ───────────────────────────────────────────────────────────

SCHEMA = cjson.Schema(
    id=1,
    columns=["ts", "ok", "status", "ms", "err"],
    bases={"ts": 1_000_000},
    bool_int={"ok"},
    defaults={"err": None},
    value_aliases={"status": {"ok": 1, "error": 2}},
)
SCHEMAS = {1: SCHEMA}

RECORDS = [
    {"ts": 1_000_000, "ok": True,  "status": "ok",    "ms": 10, "err": None},
    {"ts": 1_000_060, "ok": False, "status": "error",  "ms": 20, "err": "timeout"},
]


# ── recovery ──────────────────────────────────────────────────────────────────

def test_torn_final_line(tmp_path):
    """Partial JSON line at EOF (crash mid-write) is silently skipped."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    with open(path, "a") as f:
        f.write('{"torn":')
    assert cjson.load_cjsonl(path, schemas=SCHEMAS) == RECORDS


def test_torn_data_row(tmp_path):
    """Torn data row is skipped; prior records are still returned."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS[:1], path, schema=SCHEMA, seal=False)
    with open(path, "a") as f:
        f.write("[1000060,0,")  # incomplete array
    result = cjson.load_cjsonl(path, schemas=SCHEMAS)
    assert result == RECORDS[:1]


def test_empty_file(tmp_path):
    """Empty file returns empty list without error."""
    path = str(tmp_path / "empty.cjsonl")
    open(path, "w").close()
    assert cjson.load_cjsonl(path) == []


def test_header_only(tmp_path):
    """File with only a header line and no data rows returns empty list."""
    path = str(tmp_path / "header_only.cjsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"v": 1, "c": ["ts", "ok"]}) + "\n")
    assert cjson.load_cjsonl(path) == []


def test_unsealed_file_reads_correctly(tmp_path):
    """File without footer still returns all written records."""
    path = str(tmp_path / "unsealed.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, schema=SCHEMA, append=False)
        for r in RECORDS:
            w.write(r)
        # no seal
    assert cjson.load_cjsonl(path, schemas=SCHEMAS) == RECORDS


# ── seal lifecycle ────────────────────────────────────────────────────────────

def test_seal_idempotent(tmp_path):
    """seal() on an already-sealed file is a no-op — no second footer line."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    cjson.seal(path, schemas=SCHEMAS)  # second call
    with open(path) as f:
        footer_lines = [l for l in f if '"$"' in l]
    assert len(footer_lines) == 1


def test_seal_adds_footer(tmp_path):
    """seal() on an unsealed file appends footer with correct count."""
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, schema=SCHEMA, append=False)
        for r in RECORDS:
            w.write(r)
    meta = cjson.seal(path, schemas=SCHEMAS)
    assert meta.sealed
    assert meta.count == len(RECORDS)


def test_dump_cjsonl_default_seals(tmp_path):
    """dump_cjsonl writes footer by default."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    meta = cjson.scan(path, schemas=SCHEMAS)
    assert meta.sealed
    assert meta.count == len(RECORDS)


def test_dump_cjsonl_seal_false(tmp_path):
    """dump_cjsonl with seal=False leaves file without footer."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA, seal=False)
    meta = cjson.scan(path, schemas=SCHEMAS)
    assert not meta.sealed
    assert meta.count == len(RECORDS)


# ── scan ─────────────────────────────────────────────────────────────────────

def test_scan_unsealed_counts_rows(tmp_path):
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, schema=SCHEMA, append=False)
        for r in RECORDS:
            w.write(r)
    meta = cjson.scan(path, schemas=SCHEMAS)
    assert not meta.sealed
    assert meta.count == len(RECORDS)


def test_scan_minmax(tmp_path):
    """scan() reports min/max for numeric columns in encoded space."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    meta = cjson.scan(path, schemas=SCHEMAS)
    # ts column (pos 0) is delta-encoded: base=1_000_000
    # stored: [0, 60]
    assert 0 in meta.minmax
    assert meta.minmax[0] == [0, 60]


# ── schema-less ───────────────────────────────────────────────────────────────

def test_schema_less_roundtrip(tmp_path):
    """columns= without a Schema object round-trips correctly."""
    path = str(tmp_path / "noschema.cjsonl")
    records = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    cjson.dump_cjsonl(records, path, columns=["a", "b"])
    assert cjson.load_cjsonl(path) == records


def test_schema_less_missing_column_is_none(tmp_path):
    """Record missing a column produces None for that position on read."""
    path = str(tmp_path / "sparse.cjsonl")
    cjson.dump_cjsonl([{"a": 1, "b": "x"}, {"a": 2}], path, columns=["a", "b"])
    result = cjson.load_cjsonl(path)
    assert result[1]["b"] is None


# ── append mode ───────────────────────────────────────────────────────────────

def test_writer_append_no_second_header(tmp_path):
    """Writer in append mode must not write a second header to a non-empty file."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS[:1], path, schema=SCHEMA, seal=False)
    with open(path, "a+") as f:
        w = cjson.Writer(f, schema=SCHEMA, append=True)
        w.write(RECORDS[1])
    result = cjson.load_cjsonl(path, schemas=SCHEMAS)
    assert result == RECORDS
    with open(path) as f:
        assert sum(1 for l in f if '"v"' in l) == 1


def test_open_writer_appends(tmp_path):
    """open_writer convenience function appends without duplicate header."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS[:1], path, schema=SCHEMA, seal=False)
    with cjson.open_writer(path, schema=SCHEMA) as w:
        w.write(RECORDS[1])
    assert cjson.load_cjsonl(path, schemas=SCHEMAS) == RECORDS


def test_append_one_by_one(tmp_path):
    """cjson.append() one record at a time builds a correct file."""
    path = str(tmp_path / "events.cjsonl")
    for r in RECORDS:
        cjson.append(path, r, schema=SCHEMA)
    assert cjson.load_cjsonl(path, schemas=SCHEMAS) == RECORDS


# ── writer lifecycle ──────────────────────────────────────────────────────────

def test_write_to_sealed_raises(tmp_path):
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False)
        w.write({"a": 1})
        w.seal()
        with pytest.raises(cjson.CjsonlError, match="sealed"):
            w.write({"a": 2})


def test_write_to_closed_raises(tmp_path):
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False)
        w.write({"a": 1})
        w.close()
        with pytest.raises(cjson.CjsonlError, match="closed"):
            w.write({"a": 2})


def test_close_idempotent(tmp_path):
    """close() called twice must not raise."""
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False)
        w.write({"a": 1})
        w.close()
        w.close()


def test_seal_then_close(tmp_path):
    """Explicit seal() before close() must not double-seal with seal_on_close=True."""
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False, seal_on_close=True)
        w.write({"a": 1})
        w.seal()
        w.close()
    with open(path) as f:
        assert sum(1 for l in f if '"$"' in l) == 1


# ── convert ───────────────────────────────────────────────────────────────────

def test_convert_jsonl_to_cjsonl_roundtrip(tmp_path):
    src = str(tmp_path / "in.jsonl")
    dst = str(tmp_path / "out.cjsonl")
    with open(src, "w") as f:
        for r in RECORDS:
            f.write(json.dumps(r) + "\n")
    cjson.convert_jsonl_to_cjsonl(src, dst)
    assert cjson.load_cjsonl(dst) == RECORDS


def test_convert_cjsonl_to_jsonl_roundtrip(tmp_path):
    src = str(tmp_path / "in.cjsonl")
    dst = str(tmp_path / "out.jsonl")
    cjson.dump_cjsonl(RECORDS, src, schema=SCHEMA)
    cjson.convert_cjsonl_to_jsonl(src, dst, schemas=SCHEMAS)
    with open(dst) as f:
        result = [json.loads(l) for l in f if l.strip()]
    assert result == RECORDS


def test_convert_jsonl_empty_source(tmp_path):
    """Empty JSONL source produces an empty (or missing) cjsonl without error."""
    src = str(tmp_path / "empty.jsonl")
    dst = str(tmp_path / "out.cjsonl")
    open(src, "w").close()
    cjson.convert_jsonl_to_cjsonl(src, dst, columns=["a", "b"])
    assert cjson.load_cjsonl(dst) == []


# ── compression ───────────────────────────────────────────────────────────────

def test_compress_decompress_roundtrip(tmp_path):
    src = str(tmp_path / "events.cjsonl")
    gz  = str(tmp_path / "events.cjsonl.gz")
    out = str(tmp_path / "events_out.cjsonl")
    cjson.dump_cjsonl(RECORDS, src, schema=SCHEMA)
    cjson.compress_cjsonl(src, gz)
    cjson.decompress_cjsonl(gz, out)
    assert cjson.load_cjsonl(out, schemas=SCHEMAS) == RECORDS


def test_iter_compressed_records(tmp_path):
    src = str(tmp_path / "events.cjsonl")
    gz  = str(tmp_path / "events.cjsonl.gz")
    cjson.dump_cjsonl(RECORDS, src, schema=SCHEMA)
    cjson.compress_cjsonl(src, gz)
    result = list(cjson.iter_cjsonl_compressed_records(gz, schemas=SCHEMAS))
    assert result == RECORDS


def test_iter_records_gzip_bytes(tmp_path):
    src = str(tmp_path / "events.cjsonl")
    gz  = str(tmp_path / "events.cjsonl.gz")
    cjson.dump_cjsonl(RECORDS, src, schema=SCHEMA)
    cjson.compress_cjsonl(src, gz)
    with open(gz, "rb") as f:
        data = f.read()
    result = list(cjson.iter_records_gzip_bytes(data, schemas=SCHEMAS))
    assert result == RECORDS


# ── row reader ────────────────────────────────────────────────────────────────

def test_row_reader_decoded(tmp_path):
    """RowReader without selection returns decoded lists in schema column order."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    rows = list(cjson.iter_cjsonl_rows(path, schemas=SCHEMAS))
    assert rows[0] == [1_000_000, True, "ok", 10, None]
    assert rows[1] == [1_000_060, False, "error", 20, "timeout"]


def test_row_reader_raw(tmp_path):
    """RowReader with raw=True returns encoded values without decode."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    rows = list(cjson.iter_cjsonl_rows(path, schemas=SCHEMAS, raw=True))
    # ts stored as delta: 1_000_000 - 1_000_000 = 0
    assert rows[0][0] == 0
    # ok stored as int: 1
    assert rows[0][1] == 1
    # status stored as alias: "ok" → 1
    assert rows[0][2] == 1
    # err stored as default marker: 0
    assert rows[0][4] == 0


def test_row_reader_raw_select(tmp_path):
    """raw=True + columns=[...] returns encoded values for selected columns only."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    rows = list(cjson.iter_cjsonl_rows(path, schemas=SCHEMAS, columns=["status", "ts"], raw=True))
    # status: alias "ok"→1, "error"→2; ts: delta 0 and 60
    assert rows[0] == [1, 0]
    assert rows[1] == [2, 60]


# ── Writer hooks ─────────────────────────────────────────────────────────────

def test_on_seal_hook_fires(tmp_path):
    """on_seal is called once after footer is written."""
    called = []
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False,
                         on_seal=lambda w: called.append(w.meta.count))
        w.write({"a": 1})
        w.write({"a": 2})
        w.seal()
    assert called == [2]


def test_on_seal_not_called_if_not_sealed(tmp_path):
    """on_seal must not fire if writer is closed without sealing."""
    called = []
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False,
                         on_seal=lambda w: called.append(True))
        w.write({"a": 1})
        w.close()
    assert called == []


def test_on_seal_fires_via_seal_on_close(tmp_path):
    """on_seal fires when triggered by seal_on_close=True."""
    called = []
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        with cjson.Writer(f, columns=["a"], append=False,
                          seal_on_close=True,
                          on_seal=lambda w: called.append(w.meta.count)) as w:
            w.write({"a": 1})
    assert called == [1]


def test_on_seal_not_called_twice(tmp_path):
    """on_seal fires only once even if seal() is called twice."""
    called = []
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False,
                         on_seal=lambda w: called.append(True))
        w.write({"a": 1})
        w.seal()
        w.seal()  # no-op
    assert called == [1]


def test_on_evolve_hook_fires(tmp_path):
    """on_evolve is called after each schema evolution."""
    seen_columns = []
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False,
                         on_evolve=lambda w: seen_columns.append(list(w.columns)))
        w.write({"a": 1})
        w.evolve(["a", "b"])
        w.write({"a": 2, "b": "x"})
        w.evolve(["a", "b", "c"])
        w.write({"a": 3, "b": "y", "c": 99})
    assert seen_columns == [["a", "b"], ["a", "b", "c"]]


def test_on_evolve_not_fired_for_noop(tmp_path):
    """on_evolve must not fire when evolve() is called with identical columns."""
    called = []
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a", "b"], append=False,
                         on_evolve=lambda w: called.append(True))
        w.write({"a": 1, "b": 2})
        w.evolve(["a", "b"])  # no-op
    assert called == []


def test_on_close_hook_fires(tmp_path):
    """on_close is called once when writer is closed."""
    called = []
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        with cjson.Writer(f, columns=["a"], append=False,
                          on_close=lambda w: called.append(True)) as w:
            w.write({"a": 1})
    assert called == [1]


def test_on_close_not_called_twice(tmp_path):
    """on_close fires only once even if close() is called twice."""
    called = []
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False,
                         on_close=lambda w: called.append(True))
        w.write({"a": 1})
        w.close()
        w.close()
    assert called == [1]


def test_open_writer_hooks(tmp_path):
    """open_writer passes hooks through to Writer."""
    sealed = []
    path = str(tmp_path / "events.cjsonl")
    with cjson.open_writer(path, columns=["a"],
                           on_seal=lambda w: sealed.append(w.meta.count)) as w:
        w.write({"a": 1})
        w.seal()
    assert sealed == [1]


def test_hook_exception_propagates(tmp_path):
    """Exception raised in a hook propagates to the caller."""
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False,
                         on_seal=lambda w: (_ for _ in ()).throw(RuntimeError("hook failed")))
        w.write({"a": 1})
        with pytest.raises(RuntimeError, match="hook failed"):
            w.seal()


# ── Writer.columns property ───────────────────────────────────────────────────

def test_writer_columns_property(tmp_path):
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, schema=SCHEMA, append=False)
        assert w.columns == list(SCHEMA.columns)


# ── Writer.evolve / multi-segment ─────────────────────────────────────────────

def test_evolve_adds_column(tmp_path):
    """evolve() writes a new header; data after it uses the new schema."""
    path = str(tmp_path / "events.cjsonl")
    base_cols = ["ts", "ok", "ms"]
    ext_cols  = ["ts", "ok", "ms", "err"]
    r1 = {"ts": 1_000_000, "ok": True,  "ms": 10}
    r2 = {"ts": 1_000_060, "ok": False, "ms": 20, "err": "timeout"}

    with open(path, "w") as f:
        w = cjson.Writer(f, columns=base_cols, append=False)
        w.write(r1)
        w.evolve(ext_cols)
        w.write(r2)

    result = cjson.load_cjsonl(path)
    assert result[0] == {**r1}
    assert result[1] == {**r2}


def test_evolve_produces_two_headers(tmp_path):
    """Each evolve() appends exactly one new header line."""
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False)
        w.write({"a": 1})
        w.evolve(["a", "b"])
        w.write({"a": 2, "b": "x"})

    with open(path) as f:
        header_lines = [l for l in f if '"v"' in l]
    assert len(header_lines) == 2


def test_evolve_noop_same_columns(tmp_path):
    """evolve() with identical column list does not write a new header."""
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a", "b"], append=False)
        w.write({"a": 1, "b": "x"})
        w.evolve(["a", "b"])  # same → no-op
        w.write({"a": 2, "b": "y"})

    with open(path) as f:
        header_lines = [l for l in f if '"v"' in l]
    assert len(header_lines) == 1


def test_evolve_on_sealed_raises(tmp_path):
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False)
        w.write({"a": 1})
        w.seal()
        with pytest.raises(cjson.CjsonlError, match="sealed"):
            w.evolve(["a", "b"])


def test_evolve_on_closed_raises(tmp_path):
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["a"], append=False)
        w.write({"a": 1})
        w.close()
        with pytest.raises(cjson.CjsonlError, match="closed"):
            w.evolve(["a", "b"])


def test_evolve_selective_decode_missing_column_is_none(tmp_path):
    """Selective decode across multi-segment: column absent in segment 1 yields None for those rows."""
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["ts", "ok"], append=False)
        w.write({"ts": 1, "ok": True})
        w.evolve(["ts", "ok", "err"])
        w.write({"ts": 2, "ok": False, "err": "timeout"})

    result = cjson.load_cjsonl(path, columns=["ts", "err"])
    assert result[0] == {"ts": 1, "err": None}        # segment 1: err absent → None
    assert result[1] == {"ts": 2, "err": "timeout"}   # segment 2: err present


def test_evolve_selective_rows_missing_column_is_none(tmp_path):
    """raw=True + columns= across multi-segment: absent column in segment 1 yields None."""
    path = str(tmp_path / "events.cjsonl")
    with open(path, "w") as f:
        w = cjson.Writer(f, columns=["ts", "ok"], append=False)
        w.write({"ts": 1, "ok": True})
        w.evolve(["ts", "ok", "err"])
        w.write({"ts": 2, "ok": False, "err": "timeout"})

    rows = list(cjson.iter_cjsonl_rows(path, columns=["err", "ts"], raw=True))
    assert rows[0] == [None, 1]    # segment 1: err=None, ts as-is
    assert rows[1] == ["timeout", 2]


def test_unknown_column_returns_none(tmp_path):
    """Requesting a column that never exists in any segment produces None, not an error."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl([{"a": 1}, {"a": 2}], path, columns=["a"])
    result = cjson.load_cjsonl(path, columns=["a", "ghost"])
    assert result[0] == {"a": 1, "ghost": None}
    assert result[1] == {"a": 2, "ghost": None}


def test_scan_sealed_fast_path(tmp_path):
    """scan() on sealed file uses read_footer() and skips data rows."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    meta = cjson.scan(path, schemas=SCHEMAS)
    assert meta.sealed
    assert meta.count == len(RECORDS)
    assert meta.columns == list(SCHEMA.columns)
    assert 0 in meta.minmax  # minmax from footer


def test_evolve_with_schema_adds_encoding(tmp_path):
    """evolve() carries schema encoding (bases, bool) into the extended column set."""
    # schema.columns defines the initial column set; Writer ignores columns= when schema is set.
    schema = cjson.Schema(
        id=None,
        columns=["ts", "ok", "ms"],
        bases={"ts": 1_000_000},
        bool_int={"ok"},
    )
    path = str(tmp_path / "events.cjsonl")
    r1 = {"ts": 1_000_000, "ok": True,  "ms": 10}
    r2 = {"ts": 1_000_060, "ok": False, "ms": 20, "err": "timeout"}

    with open(path, "w") as f:
        w = cjson.Writer(f, schema=schema, append=False)
        assert w.columns == ["ts", "ok", "ms"]
        w.write(r1)
        # Extend with "err" — not in schema, so no encoding for it
        w.evolve(["ts", "ok", "ms", "err"])
        w.write(r2)

    result = cjson.load_cjsonl(path)
    assert result[0] == {"ts": 1_000_000, "ok": True, "ms": 10}
    assert result[1] == {"ts": 1_000_060, "ok": False, "ms": 20, "err": "timeout"}


# ── read_footer ───────────────────────────────────────────────────────────────

def test_read_footer_sealed(tmp_path):
    """read_footer() on sealed file returns correct count and minmax."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    meta = cjson.read_footer(path)
    assert meta is not None
    assert meta.sealed
    assert meta.count == len(RECORDS)
    assert 0 in meta.minmax  # ts column


def test_read_footer_minmax_matches_scan(tmp_path):
    """read_footer() minmax must match what scan() reports."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA)
    footer_meta = cjson.read_footer(path)
    scan_meta   = cjson.scan(path, schemas=SCHEMAS)
    assert footer_meta.minmax == scan_meta.minmax
    assert footer_meta.count  == scan_meta.count


def test_read_footer_unsealed_returns_none(tmp_path):
    """read_footer() on an unsealed file must return None."""
    path = str(tmp_path / "events.cjsonl")
    cjson.dump_cjsonl(RECORDS, path, schema=SCHEMA, seal=False)
    assert cjson.read_footer(path) is None


def test_read_footer_empty_file_returns_none(tmp_path):
    path = str(tmp_path / "empty.cjsonl")
    open(path, "w").close()
    assert cjson.read_footer(path) is None


def test_read_footer_missing_file_returns_none(tmp_path):
    assert cjson.read_footer(str(tmp_path / "nonexistent.cjsonl")) is None
