#!/usr/bin/env python3
"""
Benchmark modelling FilePerMonitorStateDriver usage patterns.

Scenarios:
  1. Buffer write   — one record at a time (save_result pattern)
  2. Blob read      — full decompressed scan (_populate_blob_cache pattern)
  3. Schema evolve  — new column mid-stream vs full file rewrite
  4. File sizes     — legacy cjson vs cjsonl vs JSONL

Usage:
    python bench/bench_monitoring.py [--monitors N] [--checks N] [--runs R]
"""

import argparse
import gzip
import io
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import cjson

# ── monitoring schema ─────────────────────────────────────────────────────────
# mirrors the columns the driver stores per check result

BASE_TS = 1_780_000_000

SCHEMA = cjson.Schema(
    id=1,
    columns=[
        "checkedTs", "ok", "status", "totalResponseTimeMs",
        "error", "pingEvent", "pingBody", "pingSignal", "pingRid",
        "durationMs", "entryId",
    ],
    bases={"checkedTs": BASE_TS},
    bool_int={"ok"},
    defaults={
        "error": None, "pingEvent": None, "pingBody": None,
        "pingSignal": None, "pingRid": None, "durationMs": None,
    },
    value_aliases={"status": {"ok": 1, "timeout": 2, "error": 3, "dns_error": 4, "ssl_error": 5}},
)
SCHEMAS = {1: SCHEMA}

# columns the driver extracts for the SQLite hour_index cache
CACHE_COLS = [
    "checkedTs", "ok", "error", "totalResponseTimeMs",
    "pingEvent", "pingSignal", "pingBody", "pingRid", "durationMs", "entryId",
]

# ── data generation ───────────────────────────────────────────────────────────

def make_check_results(n: int, seed: int = 42) -> list[dict]:
    """Simulate one hour of check results for one monitor."""
    rng = random.Random(seed)
    ts = BASE_TS
    statuses_ok  = ["ok"] * 90 + ["timeout"] * 8 + ["error"] * 2
    statuses_bad = ["timeout", "error", "dns_error", "ssl_error"]
    results = []
    for i in range(n):
        ts += rng.randint(1, 60)
        ok = rng.random() < 0.95
        if ok:
            results.append({
                "checkedTs": ts,
                "ok": True,
                "status": "ok",
                "totalResponseTimeMs": rng.randint(10, 800),
                "error": None,
                "pingEvent": None,
                "pingBody": None,
                "pingSignal": None,
                "pingRid": None,
                "durationMs": None,
                "entryId": f"e{i:08d}",
            })
        else:
            has_ping = rng.random() < 0.4
            results.append({
                "checkedTs": ts,
                "ok": False,
                "status": rng.choice(statuses_bad),
                "totalResponseTimeMs": rng.randint(800, 30000),
                "error": f"err_{i}",
                "pingEvent": f"ping_{i}" if has_ping else None,
                "pingBody": f"body_{i}" if has_ping else None,
                "pingSignal": f"sig_{i}" if has_ping else None,
                "pingRid": f"rid_{i}" if has_ping else None,
                "durationMs": rng.randint(100, 5000) if has_ping else None,
                "entryId": f"e{i:08d}",
            })
    return results


# ── write helpers (buffer format) ─────────────────────────────────────────────

def write_jsonl_one_by_one(path: str, records: list[dict]) -> None:
    """Simulate JSONL buffer: open-append-close per record (current driver pattern)."""
    for r in records:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_cjsonl_open_close(path: str, records: list[dict]) -> None:
    """cjsonl append via cjson.append() — open/close per record."""
    for r in records:
        cjson.append(path, r, schema=SCHEMA)


def write_cjsonl_writer_open(path: str, records: list[dict]) -> None:
    """cjsonl append with Writer held open — correct production pattern."""
    with cjson.open_writer(path, schema=SCHEMA) as w:
        for r in records:
            w.write(r)


def write_legacy_batch(path: str, records: list[dict]) -> bytes:
    """Legacy cjson batch format — what driver currently uses for blobs."""
    packed = cjson.dumps(records)  # {"$c":[...],"$r":[[...],...]}
    return gzip.compress(packed.encode("utf-8"), compresslevel=6)


# ── read helpers (blob format) ────────────────────────────────────────────────

def read_legacy_gz_to_tuples(data: bytes) -> list[tuple]:
    """Current driver: decompress → cjson.loads → dict.get per field → tuples."""
    records = cjson.loads(gzip.decompress(data).decode("utf-8"))
    return [(r.get(c) for c in CACHE_COLS) for r in records]


def read_cjsonl_gz_full_dict(path: str) -> list[dict]:
    return list(cjson.iter_cjsonl_compressed_records(path, schemas=SCHEMAS))


def read_cjsonl_gz_full_to_tuples(path: str) -> list[tuple]:
    return [(r.get(c) for c in CACHE_COLS)
            for r in cjson.iter_cjsonl_compressed_records(path, schemas=SCHEMAS)]


def read_cjsonl_gz_select_to_tuples(path: str) -> list[list]:
    """cjsonl selective decode — skips 'status' column not needed for cache."""
    return list(cjson.iter_cjsonl_compressed_rows(path, schemas=SCHEMAS, columns=CACHE_COLS))


def read_cjsonl_gz_select_raw(path: str) -> list[list]:
    """cjsonl selective raw — skip decode entirely, fastest path."""
    return list(cjson.iter_cjsonl_compressed_rows(path, schemas=SCHEMAS, columns=CACHE_COLS, raw=True))


# ── schema evolution ──────────────────────────────────────────────────────────

def write_with_rewrite_on_evolve(path: str, batches: list[list[dict]]) -> None:
    """
    Simulate current driver buffer approach:
    each time a new column appears, rewrite the entire file.
    batches[i] introduces one new column.
    """
    columns: list[str] = []
    all_rows: list[dict] = []
    for batch in batches:
        new_keys = [k for k in batch[0].keys() if k not in columns]
        if new_keys:
            columns = list(batch[0].keys())
            all_rows_with_new = all_rows + batch
            # rewrite from scratch
            with open(path, "w", encoding="utf-8") as f:
                w = cjson.Writer(f, columns=columns, append=False)
                for r in all_rows_with_new:
                    w.write(r)
        else:
            with open(path, "a", encoding="utf-8") as f:
                w = cjson.Writer(f, columns=columns, append=True)
                for r in batch:
                    w.write(r)
            all_rows += batch
            continue
        all_rows = all_rows_with_new


def write_with_evolve(path: str, batches: list[list[dict]]) -> None:
    """cjsonl evolve: new column → new inline header, no rewrite."""
    columns: list[str] = []
    writer: cjson.Writer | None = None
    fp = open(path, "w", encoding="utf-8")
    try:
        for batch in batches:
            new_keys = [k for k in batch[0].keys() if k not in columns]
            if new_keys:
                new_columns = columns + new_keys
                if writer is None:
                    writer = cjson.Writer(fp, columns=new_columns, append=False)
                else:
                    writer.evolve(new_columns)
                columns = new_columns
            for r in batch:
                writer.write(r)
    finally:
        if writer:
            writer.close()
        fp.close()


# ── timing ────────────────────────────────────────────────────────────────────

def median_elapsed(fn, runs: int) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    times.sort()
    return times[len(times) // 2]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--monitors", type=int, default=10,   metavar="N", help="concurrent monitors (default: 10)")
    parser.add_argument("--checks",   type=int, default=1000, metavar="N", help="checks per monitor per hour (default: 1000)")
    parser.add_argument("--runs",     type=int, default=5,    metavar="R", help="timing runs per benchmark (default: 5)")
    args = parser.parse_args()

    n_checks = args.checks
    n_monitors = args.monitors

    print(f"Generating {n_checks:,} check results × {n_monitors:,} monitors …", end=" ", flush=True)
    all_records = [make_check_results(n_checks, seed=i) for i in range(n_monitors)]
    print("done\n")

    with tempfile.TemporaryDirectory() as td:

        # ── 1. Buffer write performance ────────────────────────────────────────
        print("── 1. Buffer write  (one record at a time, single monitor) ─────────────")
        print(f"  {'scenario':<40}  {'time':>10}  vs JSONL-append")
        print(f"  {'-'*40}  {'-'*10}  ---------------")

        records = all_records[0]

        p_buf_jsonl  = os.path.join(td, "buf.jsonl")
        p_buf_cjsonl = os.path.join(td, "buf_open_close.cjsonl")
        p_buf_held   = os.path.join(td, "buf_held.cjsonl")

        write_benchmarks = [
            ("JSONL append (open-close per record)",   lambda: (os.remove(p_buf_jsonl)  if os.path.exists(p_buf_jsonl)  else None, write_jsonl_one_by_one(p_buf_jsonl, records))),
            ("cjsonl append (open-close per record)",  lambda: (os.remove(p_buf_cjsonl) if os.path.exists(p_buf_cjsonl) else None, write_cjsonl_open_close(p_buf_cjsonl, records))),
            ("cjsonl append (Writer held open)",       lambda: (os.remove(p_buf_held)   if os.path.exists(p_buf_held)   else None, write_cjsonl_writer_open(p_buf_held, records))),
        ]

        write_baseline: float | None = None
        for label, fn in write_benchmarks:
            fn()  # warmup write
            elapsed = median_elapsed(fn, args.runs)
            if write_baseline is None:
                write_baseline = elapsed
            ratio = elapsed / write_baseline
            print(f"  {label:<40}  {elapsed * 1000:7.1f} ms  {ratio:5.2f}×")

        # ── file sizes for the written buffer ──────────────────────────────────
        print()
        sz_jsonl  = os.path.getsize(p_buf_jsonl)
        sz_cjsonl = os.path.getsize(p_buf_held)
        print(f"  Buffer file sizes ({n_checks:,} records, single monitor):")
        print(f"    JSONL     {sz_jsonl  / 1024:8.1f} KB  (100%)")
        print(f"    cjsonl    {sz_cjsonl / 1024:8.1f} KB  ({100 * sz_cjsonl / sz_jsonl:3.0f}%)")

        # ── 2. Blob read performance (_populate_blob_cache) ────────────────────
        print()
        print("── 2. Blob read  (all monitors, _populate_blob_cache pattern) ──────────")
        print(f"  {'scenario':<48}  {'time':>10}  vs legacy-gz")
        print(f"  {'-'*48}  {'-'*10}  -----------")

        # Prepare blob files for all monitors.
        p_legacy_gz   = [os.path.join(td, f"blob_{i}.legacy.gz")     for i in range(n_monitors)]
        p_cjsonl_gz   = [os.path.join(td, f"blob_{i}.cjsonl.gz")     for i in range(n_monitors)]
        legacy_bytes  = []

        for i, recs in enumerate(all_records):
            # Legacy cjson gz (current driver format)
            b = write_legacy_batch("/dev/null", recs)
            with open(p_legacy_gz[i], "wb") as f:
                f.write(b)
            legacy_bytes.append(b)
            # cjsonl gz
            p_plain = os.path.join(td, f"blob_{i}.cjsonl")
            cjson.dump_cjsonl(recs, p_plain, schema=SCHEMA)
            cjson.compress_cjsonl(p_plain, p_cjsonl_gz[i])

        def bench_legacy_gz():
            for b in legacy_bytes:
                read_legacy_gz_to_tuples(b)

        def bench_cjsonl_gz_full():
            for p in p_cjsonl_gz:
                read_cjsonl_gz_full_to_tuples(p)

        def bench_cjsonl_gz_select():
            for p in p_cjsonl_gz:
                read_cjsonl_gz_select_to_tuples(p)

        def bench_cjsonl_gz_raw():
            for p in p_cjsonl_gz:
                read_cjsonl_gz_select_raw(p)

        read_benchmarks = [
            ("legacy cjson.gz → dict.get() → tuples",  bench_legacy_gz),
            ("cjsonl.gz → dict → dict.get() → tuples", bench_cjsonl_gz_full),
            (f"cjsonl.gz → rows [{len(CACHE_COLS)} cols]", bench_cjsonl_gz_select),
            (f"cjsonl.gz → rows [{len(CACHE_COLS)} cols, raw]", bench_cjsonl_gz_raw),
        ]

        read_baseline: float | None = None
        for label, fn in read_benchmarks:
            elapsed = median_elapsed(fn, args.runs)
            if read_baseline is None:
                read_baseline = elapsed
            ratio = elapsed / read_baseline
            print(f"  {label:<48}  {elapsed * 1000:7.1f} ms  {ratio:5.2f}×")

        # ── blob file sizes ────────────────────────────────────────────────────
        print()
        sz_leg  = sum(os.path.getsize(p) for p in p_legacy_gz)
        sz_cjgz = sum(os.path.getsize(p) for p in p_cjsonl_gz)
        print(f"  Blob file sizes ({n_monitors} monitors × {n_checks:,} checks):")
        print(f"    legacy cjson.gz  {sz_leg  / 1024:8.1f} KB  (100%)")
        print(f"    cjsonl.gz        {sz_cjgz / 1024:8.1f} KB  ({100 * sz_cjgz / sz_leg:3.0f}%)")

        # ── 3. Schema evolution ────────────────────────────────────────────────
        print()
        print("── 3. Schema evolution  (3 new columns appearing mid-stream) ───────────")

        recs = all_records[0]
        n3 = n_checks // 3

        # Simulate 3 batches, each introducing one new column (ping fields)
        base_cols = ["checkedTs", "ok", "status", "totalResponseTimeMs", "error", "entryId"]
        batches: list[list[dict]] = [
            [{k: r[k] for k in base_cols}                                        for r in recs[:n3]],
            [{**{k: r[k] for k in base_cols}, "pingEvent": r["pingEvent"]}       for r in recs[n3:2*n3]],
            [{**{k: r[k] for k in base_cols}, "pingEvent": r["pingEvent"],
              "durationMs": r["durationMs"]}                                      for r in recs[2*n3:]],
        ]

        p_rewrite = os.path.join(td, "evolve_rewrite.cjsonl")
        p_evolve  = os.path.join(td, "evolve_inline.cjsonl")

        t_rewrite = median_elapsed(lambda: write_with_rewrite_on_evolve(p_rewrite, batches), args.runs)
        t_evolve  = median_elapsed(lambda: write_with_evolve(p_evolve, batches), args.runs)

        print(f"  {'scenario':<40}  {'time':>10}  vs rewrite")
        print(f"  {'-'*40}  {'-'*10}  ----------")
        print(f"  {'rewrite entire buffer on new column':<40}  {t_rewrite * 1000:7.1f} ms   1.00×")
        print(f"  {'evolve() inline header, no rewrite':<40}  {t_evolve  * 1000:7.1f} ms  {t_evolve / t_rewrite:5.2f}×")

        sz_rewrite = os.path.getsize(p_rewrite)
        sz_evolve  = os.path.getsize(p_evolve)
        print(f"\n  File sizes after 3 schema evolutions ({n_checks:,} records):")
        print(f"    rewrite approach  {sz_rewrite / 1024:8.1f} KB  (100%)")
        print(f"    evolve approach   {sz_evolve  / 1024:8.1f} KB  ({100 * sz_evolve / sz_rewrite:3.0f}%)")

        print(f"\n  checks={n_checks:,}  monitors={n_monitors}  runs={args.runs}")


if __name__ == "__main__":
    main()
