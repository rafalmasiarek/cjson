#!/usr/bin/env python3
"""
Read performance benchmark: JSONL vs cjsonl.

Usage:
    python bench/bench_read.py [--rows N] [--runs R]

Measures file size (plain + gzip) and read throughput for:
  - JSONL → dict               (baseline)
  - JSONL.gz → dict
  - cjsonl → dict, all columns
  - cjsonl → dict, selected columns
  - cjsonl → list, all columns (RowReader)
  - cjsonl → list, selected columns (RowReader)
  - cjsonl.gz → dict, all columns
"""

import argparse
import gzip
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import cjson

# ── schema ────────────────────────────────────────────────────────────────────

SCHEMA = cjson.Schema(
    id=1,
    columns=["ts", "ok", "status", "code", "ms", "err"],
    bases={"ts": 1_700_000_000},
    bool_int={"ok"},
    defaults={"err": None},
    value_aliases={"status": {"ok": 1, "timeout": 2, "error": 3}},
    string_parts={"code": cjson.StringParts(prefix="TX", store_in_header=False)},
)
SCHEMAS = {1: SCHEMA}
SELECT = ["ts", "status"]


# ── data generation ───────────────────────────────────────────────────────────

def make_records(n: int, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    ts = 1_700_000_000
    statuses = ["ok"] * 80 + ["timeout"] * 15 + ["error"] * 5
    out = []
    for i in range(n):
        ts += rng.randint(1, 60)
        ok = rng.random() < 0.8
        status = rng.choice(statuses)
        out.append({
            "ts": ts,
            "ok": ok,
            "status": status,
            "code": f"TX{rng.randint(0, 999_999):06d}",
            "ms": rng.randint(1, 5000),
            "err": None if ok else f"err_{status}_{i}",
        })
    return out


# ── write helpers ─────────────────────────────────────────────────────────────

def write_jsonl(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_cjsonl(path: str, records: list[dict]) -> None:
    cjson.dump_cjsonl(records, path, schema=SCHEMA)


def gzip_compress(src: str, dst: str) -> None:
    import shutil
    with open(src, "rb") as s, gzip.open(dst, "wb", compresslevel=6) as d:
        shutil.copyfileobj(s, d)


# ── read functions (all consume the full stream) ──────────────────────────────

def read_jsonl(path: str) -> int:
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            json.loads(line)
            n += 1
    return n


def read_jsonl_gz(path: str) -> int:
    n = 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            json.loads(line)
            n += 1
    return n


def read_cjsonl_full(path: str) -> int:
    return sum(1 for _ in cjson.iter_cjsonl_records(path, schemas=SCHEMAS))


def read_cjsonl_select(path: str) -> int:
    return sum(1 for _ in cjson.iter_cjsonl_records(path, schemas=SCHEMAS, columns=SELECT))


def read_cjsonl_rows_full(path: str) -> int:
    return sum(1 for _ in cjson.iter_cjsonl_rows(path, schemas=SCHEMAS))


def read_cjsonl_rows_select(path: str) -> int:
    return sum(1 for _ in cjson.iter_cjsonl_rows(path, schemas=SCHEMAS, columns=SELECT))


def read_cjsonl_gz_full(path: str) -> int:
    return sum(1 for _ in cjson.iter_cjsonl_compressed_records(path, schemas=SCHEMAS))


def read_cjsonl_gz_select(path: str) -> int:
    return sum(1 for _ in cjson.iter_cjsonl_compressed_records(path, schemas=SCHEMAS, columns=SELECT))


def read_cjsonl_rows_raw(path: str) -> int:
    return sum(1 for _ in cjson.iter_cjsonl_rows(path, schemas=SCHEMAS, raw=True))


def read_cjsonl_rows_raw_select(path: str) -> int:
    return sum(1 for _ in cjson.iter_cjsonl_rows(path, schemas=SCHEMAS, columns=SELECT, raw=True))


# ── timing ────────────────────────────────────────────────────────────────────

def median_elapsed(fn, runs: int) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    times.sort()
    return times[len(times) // 2]


# ── output ────────────────────────────────────────────────────────────────────

def fmt_size(b: int, ref: int) -> str:
    kb = b / 1024
    pct = 100 * b / ref
    return f"{kb:8.1f} KB  ({pct:4.0f}%)"


def fmt_ms(s: float, ref: float) -> str:
    ratio = s / ref
    return f"{s * 1000:7.1f} ms  {ratio:5.2f}×"


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=100_000, metavar="N", help="number of records (default: 100000)")
    parser.add_argument("--runs", type=int, default=5, metavar="R", help="timing runs per benchmark (default: 5)")
    args = parser.parse_args()

    print(f"Generating {args.rows:,} records … ", end="", flush=True)
    records = make_records(args.rows)
    print("done")

    with tempfile.TemporaryDirectory() as td:
        p_jsonl     = os.path.join(td, "events.jsonl")
        p_jsonl_gz  = os.path.join(td, "events.jsonl.gz")
        p_cjsonl    = os.path.join(td, "events.cjsonl")
        p_cjsonl_gz = os.path.join(td, "events.cjsonl.gz")

        print("Writing files … ", end="", flush=True)
        write_jsonl(p_jsonl, records)
        write_cjsonl(p_cjsonl, records)
        gzip_compress(p_jsonl, p_jsonl_gz)
        gzip_compress(p_cjsonl, p_cjsonl_gz)
        print("done\n")

        # ── sizes ──────────────────────────────────────────────────────────
        ref_size = os.path.getsize(p_jsonl)
        print("── File sizes ──────────────────────────────────────────────────────")
        print(f"  {'format':<20}  {'size':>12}  ratio")
        print(f"  {'-'*20}  {'-'*12}  -----")
        for label, path in [
            ("JSONL",       p_jsonl),
            ("JSONL.gz",    p_jsonl_gz),
            ("cjsonl",      p_cjsonl),
            ("cjsonl.gz",   p_cjsonl_gz),
        ]:
            b = os.path.getsize(path)
            kb = b / 1024
            pct = 100 * b / ref_size
            ref_marker = "  ← baseline" if label == "JSONL" else ""
            print(f"  {label:<20}  {kb:8.1f} KB  ({pct:4.0f}%){ref_marker}")

        # ── read benchmarks ────────────────────────────────────────────────
        benchmarks = [
            ("JSONL → dict",                         lambda: read_jsonl(p_jsonl)),
            ("JSONL.gz → dict",                      lambda: read_jsonl_gz(p_jsonl_gz)),
            ("cjsonl → dict  (all columns)",         lambda: read_cjsonl_full(p_cjsonl)),
            (f"cjsonl → dict  {SELECT}",             lambda: read_cjsonl_select(p_cjsonl)),
            ("cjsonl → list  (all columns)",         lambda: read_cjsonl_rows_full(p_cjsonl)),
            (f"cjsonl → list  {SELECT}",             lambda: read_cjsonl_rows_select(p_cjsonl)),
            ("cjsonl → list  (all, raw)",            lambda: read_cjsonl_rows_raw(p_cjsonl)),
            (f"cjsonl → list  {SELECT}  raw",        lambda: read_cjsonl_rows_raw_select(p_cjsonl)),
            ("cjsonl.gz → dict  (all columns)",      lambda: read_cjsonl_gz_full(p_cjsonl_gz)),
            (f"cjsonl.gz → dict  {SELECT}",          lambda: read_cjsonl_gz_select(p_cjsonl_gz)),
        ]

        print(f"\n── Read benchmarks  ({args.runs} runs, median) ─────────────────────────")
        print(f"  {'scenario':<44}  {'time':>10}  vs JSONL")
        print(f"  {'-'*44}  {'-'*10}  --------")

        baseline: float | None = None
        for label, fn in benchmarks:
            elapsed = median_elapsed(fn, args.runs)
            if baseline is None:
                baseline = elapsed
            ratio = elapsed / baseline
            print(f"  {label:<44}  {elapsed * 1000:7.1f} ms  {ratio:5.2f}×")

        print(f"\n  rows={args.rows:,}  runs={args.runs}  select={SELECT}")


if __name__ == "__main__":
    main()
