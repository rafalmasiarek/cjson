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

## Selective column reads

Pass `columns=[...]` to decode only a subset of columns. The result preserves
the caller's column order and raises `CjsonlError` for unknown names.

```python
for record in cjson.iter_cjsonl_records("events.cjsonl", schemas={1: schema}, columns=["ts", "status"]):
    ...  # {"ts": 1779000060, "status": "ok"}

for row in cjson.iter_cjsonl_rows("events.cjsonl", schemas={1: schema}, columns=["ts", "status"]):
    ...  # [1779000060, "ok"]
```

Pass `raw=True` on `iter_cjsonl_rows` to skip decoding entirely — values are
returned exactly as stored in the file (encoded ints, deltas, aliases).
Useful for fast filtering without restoring original values:

```python
for row in cjson.iter_cjsonl_rows("events.cjsonl", schemas={1: schema}, columns=["ts", "status"], raw=True):
    ...  # [60, 1]  — delta ts, alias int; no base restore or alias lookup
```

## Performance profile

`cjsonl` is optimized primarily for compact append-only storage and lower I/O volume.

Compared with classic JSONL (100K records, 6 columns):

| scenario | time | vs JSONL |
|---|---|---|
| JSONL → dict | 116 ms | 1.00× |
| cjsonl → dict (all columns) | 194 ms | 1.68× |
| cjsonl → dict [2 of 6 columns] | 143 ms | 1.23× |
| cjsonl → list (all columns) | 176 ms | 1.52× |
| cjsonl → list [2 of 6 columns] | 140 ms | 1.21× |
| cjsonl → list raw (all columns) | 92 ms | **0.80×** |
| cjsonl → list raw [2 of 6 cols] | 103 ms | **0.89×** |

File sizes for the same data:

| format | size | vs JSONL |
|---|---|---|
| JSONL | 8207 KB | 100% |
| JSONL.gz | 1277 KB | 16% |
| cjsonl | 3141 KB | 38% |
| cjsonl.gz | 1086 KB | 13% |

Full-record reads are slower than JSONL because each row requires a decode
pipeline in pure Python on top of `json.loads`. Selective column reads reduce
this to ~10–14% overhead. `raw=True` rows skip decoding entirely.

For analytical scans where maximum read throughput is the primary goal,
use Parquet/Arrow/DuckDB instead.

## Compatibility

Legacy functions remain available:

```python
s = cjson.dumps([{"a": 1}, {"a": 2}])
records = cjson.loads(s)
```
