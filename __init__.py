from __future__ import annotations

"""cjson: compact JSON batch + append-only cjsonl storage."""

from .legacy import *  # noqa: F401,F403
from .schema import CjsonlError, Schema, SchemaLike, SchemasLike, StringParts, Transform
from .codecs import CjsonlMeta, V1Codec, get_codec, register_codec
from .cjsonl import (
    Reader,
    RowReader,
    Writer,
    append,
    compress_cjsonl,
    convert_cjsonl_to_jsonl,
    convert_jsonl_to_cjsonl,
    decompress_cjsonl,
    dump_cjsonl,
    iter_cjsonl_compressed_records,
    iter_cjsonl_compressed_rows,
    iter_cjsonl_gzip_records,
    iter_cjsonl_gzip_rows,
    iter_cjsonl_records,
    iter_cjsonl_rows,
    iter_records_gzip_bytes,
    load_cjsonl,
    load_cjsonl_gzip,
    open_writer,
    scan,
    scan_compressed,
    scan_gzip,
    seal,
)
from .compression import (
    Compressor,
    ExternalCompressor,
    GzipCompressor,
    NoopCompressor,
    PigzCompressor,
    compress_file,
    decompress_file,
    gunzip_file,
    gzip_file,
)

# Ergonomic/backward cjsonl aliases.
cjsonl_append = append
cjsonl_dump = dump_cjsonl
cjsonl_load = load_cjsonl
cjsonl_iter_records = iter_cjsonl_records
cjsonl_iter_rows = iter_cjsonl_rows
cjsonl_iter_gzip_records = iter_cjsonl_gzip_records
cjsonl_scan = scan
cjsonl_seal = seal
cjsonl_gzip_file = gzip_file

__all__ = [
    # schema / cjsonl errors
    "CjsonlError", "Schema", "SchemaLike", "SchemasLike", "Transform", "StringParts",
    # codecs
    "CjsonlMeta", "V1Codec", "register_codec", "get_codec",
    # cjsonl IO
    "Writer", "Reader", "RowReader", "open_writer", "append", "dump_cjsonl", "load_cjsonl",
    "iter_cjsonl_records", "iter_cjsonl_rows", "seal", "scan",
    "compress_cjsonl", "decompress_cjsonl",
    "iter_cjsonl_compressed_records", "iter_cjsonl_compressed_rows",
    "iter_cjsonl_gzip_records", "iter_cjsonl_gzip_rows", "load_cjsonl_gzip",
    "iter_records_gzip_bytes", "scan_compressed", "scan_gzip",
    "convert_jsonl_to_cjsonl", "convert_cjsonl_to_jsonl",
    # compression
    "Compressor", "GzipCompressor", "PigzCompressor", "ExternalCompressor", "NoopCompressor",
    "compress_file", "decompress_file", "gzip_file", "gunzip_file",
    # cjsonl aliases
    "cjsonl_append", "cjsonl_dump", "cjsonl_load", "cjsonl_iter_records", "cjsonl_iter_rows",
    "cjsonl_iter_gzip_records", "cjsonl_scan", "cjsonl_seal", "cjsonl_gzip_file",
]
