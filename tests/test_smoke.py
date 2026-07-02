import gzip
import os
import tempfile

import cjson


def test_legacy_roundtrip():
    records = [{"a": 1}, {"a": 2, "b": None}]
    assert cjson.loads(cjson.dumps(records)) == records


def test_cjsonl_schema_alias_stringparts_gzip():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "events.cjsonl")
        gz = path + ".gz"
        schema = cjson.Schema(
            id=1,
            columns=["ts", "ok", "status", "pan", "ms", "err"],
            bases={"ts": 1000},
            bool_int={"ok"},
            defaults={"err": None},
            value_aliases={"status": {"ok": 1, "timeout": 2}},
            string_parts={"pan": cjson.StringParts(prefix="00000", store_in_header=False)},
        )
        records = [
            {"ts": 1000, "ok": True, "status": "ok", "pan": "000001", "ms": 10, "err": None},
            {"ts": 1060, "ok": False, "status": "timeout", "pan": "000002", "ms": 20, "err": "timeout"},
        ]
        cjson.dump_cjsonl(records, path, schema=schema)
        assert cjson.load_cjsonl(path, schemas={1: schema}) == records
        cjson.compress_file(path, gz)
        assert cjson.load_cjsonl_gzip(gz, schemas={1: schema}) == records
        meta = cjson.scan_gzip(gz, schemas={1: schema})
        assert meta.count == 2
        assert meta.sealed
