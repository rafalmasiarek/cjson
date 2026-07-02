# cjson

Stdlib-only compact JSON helpers with two compatible formats:

1. **Legacy batch cjson**: `list[dict] -> {"$c": [...], "$r": [[...]]}`.
2. **cjsonl v1**: append-only, JSONL-like, dense column arrays with optional schema-based compression.

The new `cjsonl` format is designed to replace classic JSONL buffers:

```text
write directly to .cjsonl -> seal footer -> stream-compress to .cjsonl.gz
```

No `JSONL -> list[dict] -> cjson.dumps(...) -> gzip` repacking is needed.

## Example

```python
import cjson

schema = cjson.Schema(
    id=1,
    name="events.v1",
    columns=["ts", "ok", "status", "pan_like", "ms", "err"],
    bases={"ts": 1779000000},
    bool_int={"ok"},
    defaults={"err": None},
    value_aliases={"status": {"ok": 1, "timeout": 2}},
    string_parts={"pan_like": cjson.StringParts(prefix="00000", store_in_header=False)},
)

records = [
    {"ts": 1779000000, "ok": True, "status": "ok", "pan_like": "000001", "ms": 128, "err": None},
    {"ts": 1779000060, "ok": False, "status": "timeout", "pan_like": "000002", "ms": 3201, "err": "timeout"},
]

cjson.dump_cjsonl(records, "events.cjsonl", schema=schema)
cjson.compress_file("events.cjsonl", "events.cjsonl.gz")

loaded = cjson.load_cjsonl_gzip("events.cjsonl.gz", schemas={1: schema})
assert loaded == records
```

Generated `.cjsonl` lines are compact arrays:

```text
{"v":1,"s":1,"b":{"0":1779000000}}
[0,1,1,"1",128,0]
[60,0,2,"2",3201,"timeout"]
{"$":1,"n":2,"x":{"0":[0,60],"1":[0,1],"2":[1,2],"4":[128,3201]}}
```

## Compression adapters

`gzip` is the stdlib default, but compression is an adapter layer:

```python
cjson.compress_file("events.cjsonl", "events.cjsonl.gz")
cjson.compress_file("events.cjsonl", "events.cjsonl.gz", compressor=cjson.PigzCompressor(level=6, processes=8))
```

`PigzCompressor` uses an external `pigz` binary but produces gzip-compatible output.

## Compatibility

Legacy functions remain available:

```python
s = cjson.dumps([{"a": 1}, {"a": 2}])
records = cjson.loads(s)
```
