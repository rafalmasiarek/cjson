from __future__ import annotations

"""Append-only compact JSON Lines API."""

import gzip
import io
import json
import os
from typing import Any, Iterable, Iterator, Mapping, TextIO

from .codecs import (
    C,
    F,
    FOOTER,
    N,
    S,
    V,
    X,
    CjsonlMeta,
    V1Codec,
    context_from_schema,
    decode_cell,
    get_codec,
    json_dumps_line,
    track_minmax,
)
from .compression import Compressor, GzipCompressor, compress_file as _compress_file, decompress_file as _decompress_file
from .schema import CjsonlError, Schema, SchemaLike, SchemasLike


class Writer:
    """
    Append-only cjsonl writer.

    The writer writes exactly one header when the target is empty, then writes
    each record as a compact JSON array. Use seal() before rotating/gzipping.
    """

    def __init__(
        self,
        fp: TextIO,
        *,
        schema: SchemaLike | None = None,
        columns: Iterable[str] | None = None,
        codec: V1Codec | None = None,
        append: bool = True,
        close_file: bool = False,
    ) -> None:
        self.fp = fp
        self._close_file = close_file
        self.codec = codec or get_codec(None)
        self.schema = Schema.from_obj(schema) if schema is not None else None
        if self.schema is not None:
            cols = list(self.schema.columns)
        elif columns is not None:
            cols = [str(c) for c in columns]
        else:
            cols = []
        self.meta = context_from_schema(self.schema, cols, codec_id=self.codec.id)
        self._sealed = False
        self._header_written = False
        if append:
            self._header_written = self._looks_nonempty(fp)
        if not self._header_written:
            if not self.meta.columns:
                raise CjsonlError("columns or schema are required when creating a new cjsonl stream")
            self.write_header()

    @staticmethod
    def _looks_nonempty(fp: TextIO) -> bool:
        try:
            pos = fp.tell()
            fp.seek(0, os.SEEK_END)
            end = fp.tell()
            fp.seek(pos, os.SEEK_SET)
            return end > 0
        except Exception:
            return False

    def write_header(self) -> None:
        header = self.codec.make_header(self.schema, self.meta.columns)
        self.fp.write(json_dumps_line(header))
        self._header_written = True

    def write(self, record: Mapping[str, Any]) -> None:
        if self._sealed:
            raise CjsonlError("cannot write to sealed cjsonl writer")
        row = self.codec.encode_record(record, self.meta)
        self.fp.write(json_dumps_line(row))
        self.meta.count += 1
        track_minmax(self.meta, row)

    def seal(self) -> None:
        if self._sealed:
            return
        footer = self.codec.make_footer(self.meta)
        self.fp.write(json_dumps_line(footer))
        self.fp.flush()
        self._sealed = True

    def close(self) -> None:
        self.fp.flush()
        if self._close_file:
            self.fp.close()

    def __enter__(self) -> "Writer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class Reader:
    """Streaming cjsonl reader. Iterates normal dict records."""

    def __init__(self, fp: TextIO, *, schemas: SchemasLike | None = None) -> None:
        self.fp = fp
        self.schemas = schemas
        self.meta: CjsonlMeta | None = None
        self.codec: V1Codec = get_codec(None)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for raw_line in self.fp:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                # JSONL/cjsonl recovery behavior: ignore a torn final line.
                continue

            if isinstance(item, dict):
                if item.get(FOOTER) == 1:
                    if self.meta is not None:
                        self.meta.sealed = True
                        self.meta.raw_footer = item
                        self.meta.count = int(item.get(N, self.meta.count))
                        self.meta.minmax = {int(k): v for k, v in item.get(X, {}).items()}
                    continue
                if V in item or C in item or S in item:
                    self.codec = get_codec(item.get(F))
                    self.meta = self.codec.context_from_header(item, self.schemas)
                    continue
                # Unknown object line is metadata for a custom/user layer. Skip.
                continue

            if isinstance(item, list):
                if self.meta is None:
                    raise CjsonlError("cjsonl data row encountered before header")
                yield self.codec.decode_row(item, self.meta)


class RowReader:
    """
    Streaming cjsonl row reader. Iterates decoded row arrays and exposes columns.

    Use this for fast scans where building dicts for every row is unnecessary.
    """

    def __init__(self, fp: TextIO, *, schemas: SchemasLike | None = None) -> None:
        self.fp = fp
        self.schemas = schemas
        self.meta: CjsonlMeta | None = None
        self.codec: V1Codec = get_codec(None)

    def __iter__(self) -> Iterator[list[Any]]:
        for raw_line in self.fp:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                if item.get(FOOTER) == 1:
                    continue
                if V in item or C in item or S in item:
                    self.codec = get_codec(item.get(F))
                    self.meta = self.codec.context_from_header(item, self.schemas)
                continue
            if isinstance(item, list):
                if self.meta is None:
                    raise CjsonlError("cjsonl data row encountered before header")
                yield [decode_cell(v, i, self.meta) for i, v in enumerate(item)]


def open_writer(
    path: str,
    *,
    schema: SchemaLike | None = None,
    columns: Iterable[str] | None = None,
    encoding: str = "utf-8",
) -> Writer:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fp = open(path, "a+", encoding=encoding)
    return Writer(fp, schema=schema, columns=columns, append=True, close_file=True)


def append(path: str, record: Mapping[str, Any], *, schema: SchemaLike | None = None, columns: Iterable[str] | None = None) -> None:
    """Append one record to a cjsonl file."""
    with open_writer(path, schema=schema, columns=columns) as w:
        w.write(record)


def iter_cjsonl_records(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> Iterator[dict[str, Any]]:
    with open(path, "r", encoding=encoding) as fp:
        yield from Reader(fp, schemas=schemas)


def iter_cjsonl_rows(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> Iterator[list[Any]]:
    with open(path, "r", encoding=encoding) as fp:
        yield from RowReader(fp, schemas=schemas)


def load_cjsonl(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> list[dict[str, Any]]:
    return list(iter_cjsonl_records(path, schemas=schemas, encoding=encoding))


def dump_cjsonl(
    records: Iterable[Mapping[str, Any]],
    path: str,
    *,
    schema: SchemaLike | None = None,
    columns: Iterable[str] | None = None,
    seal: bool = True,
    encoding: str = "utf-8",
) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding=encoding) as fp:
        writer = Writer(fp, schema=schema, columns=columns, append=False)
        for record in records:
            writer.write(record)
        if seal:
            writer.seal()
        else:
            writer.close()


def scan(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> CjsonlMeta:
    """Scan cjsonl metadata/count/minmax without returning records."""
    meta: CjsonlMeta | None = None
    codec: V1Codec = get_codec(None)
    with open(path, "r", encoding=encoding) as fp:
        for raw_line in fp:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                if item.get(FOOTER) == 1:
                    if meta is None:
                        meta = CjsonlMeta()
                    meta.sealed = True
                    meta.raw_footer = item
                    meta.count = int(item.get(N, meta.count))
                    meta.minmax = {int(k): v for k, v in item.get(X, {}).items()}
                    continue
                if V in item or C in item or S in item:
                    codec = get_codec(item.get(F))
                    meta = codec.context_from_header(item, schemas)
                    continue
                continue
            if isinstance(item, list):
                if meta is None:
                    raise CjsonlError("cjsonl data row encountered before header")
                meta.count += 1
                track_minmax(meta, item)
    return meta or CjsonlMeta()


def seal(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> CjsonlMeta:
    """
    Append a footer/seal line to an existing cjsonl file.

    This scans the file to compute count/minmax, then appends one compact footer.
    It does not rewrite the header or data rows.
    """
    meta = scan(path, schemas=schemas, encoding=encoding)
    if meta.sealed:
        return meta
    footer = {FOOTER: 1, N: meta.count}
    if meta.minmax:
        footer[X] = {str(k): v for k, v in sorted(meta.minmax.items())}
    with open(path, "a", encoding=encoding) as fp:
        fp.write(json_dumps_line(footer))
    meta.sealed = True
    meta.raw_footer = footer
    return meta


def compress_cjsonl(
    src_path: str,
    dst_path: str,
    *,
    compressor: Compressor | None = None,
    remove_src: bool = False,
) -> None:
    _compress_file(src_path, dst_path, compressor=compressor or GzipCompressor(), remove_src=remove_src)


def decompress_cjsonl(
    src_path: str,
    dst_path: str,
    *,
    compressor: Compressor | None = None,
    remove_src: bool = False,
) -> None:
    _decompress_file(src_path, dst_path, compressor=compressor or GzipCompressor(), remove_src=remove_src)


def iter_cjsonl_compressed_records(
    path: str,
    *,
    schemas: SchemasLike | None = None,
    compressor: Compressor | None = None,
    encoding: str = "utf-8",
) -> Iterator[dict[str, Any]]:
    comp = compressor or GzipCompressor()
    with comp.open_text(path, encoding=encoding) as fp:
        yield from Reader(fp, schemas=schemas)


def iter_cjsonl_compressed_rows(
    path: str,
    *,
    schemas: SchemasLike | None = None,
    compressor: Compressor | None = None,
    encoding: str = "utf-8",
) -> Iterator[list[Any]]:
    comp = compressor or GzipCompressor()
    with comp.open_text(path, encoding=encoding) as fp:
        yield from RowReader(fp, schemas=schemas)


# Backward-compatible gzip-specific helpers.
def iter_cjsonl_gzip_records(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> Iterator[dict[str, Any]]:
    yield from iter_cjsonl_compressed_records(path, schemas=schemas, compressor=GzipCompressor(), encoding=encoding)


def iter_cjsonl_gzip_rows(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> Iterator[list[Any]]:
    yield from iter_cjsonl_compressed_rows(path, schemas=schemas, compressor=GzipCompressor(), encoding=encoding)


def load_cjsonl_gzip(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> list[dict[str, Any]]:
    return list(iter_cjsonl_gzip_records(path, schemas=schemas, encoding=encoding))


def iter_records_gzip_bytes(data: bytes, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> Iterator[dict[str, Any]]:
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gz:
        with io.TextIOWrapper(gz, encoding=encoding) as fp:
            yield from Reader(fp, schemas=schemas)


def scan_compressed(
    path: str,
    *,
    schemas: SchemasLike | None = None,
    compressor: Compressor | None = None,
    encoding: str = "utf-8",
) -> CjsonlMeta:
    comp = compressor or GzipCompressor()
    meta: CjsonlMeta | None = None
    codec: V1Codec = get_codec(None)
    with comp.open_text(path, encoding=encoding) as fp:
        for raw_line in fp:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                if item.get(FOOTER) == 1:
                    if meta is None:
                        meta = CjsonlMeta()
                    meta.sealed = True
                    meta.raw_footer = item
                    meta.count = int(item.get(N, meta.count))
                    meta.minmax = {int(k): v for k, v in item.get(X, {}).items()}
                    continue
                if V in item or C in item or S in item:
                    codec = get_codec(item.get(F))
                    meta = codec.context_from_header(item, schemas)
                    continue
                continue
            if isinstance(item, list):
                if meta is None:
                    raise CjsonlError("cjsonl data row encountered before header")
                meta.count += 1
                track_minmax(meta, item)
    return meta or CjsonlMeta()


def scan_gzip(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> CjsonlMeta:
    return scan_compressed(path, schemas=schemas, compressor=GzipCompressor(), encoding=encoding)


def convert_jsonl_to_cjsonl(
    src_path: str,
    dst_path: str,
    *,
    schema: SchemaLike | None = None,
    columns: Iterable[str] | None = None,
    seal_output: bool = True,
    encoding: str = "utf-8",
) -> None:
    """Convert classic JSONL dict records into cjsonl."""
    inferred_columns: list[str] | None = None
    if schema is None and columns is None:
        with open(src_path, "r", encoding=encoding) as src:
            for line in src:
                if not line.strip():
                    continue
                first = json.loads(line)
                if not isinstance(first, dict):
                    raise CjsonlError("classic JSONL input must contain object records")
                inferred_columns = [str(k) for k in first.keys()]
                break
    cols = columns if columns is not None else inferred_columns
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
    with open(src_path, "r", encoding=encoding) as src, open(dst_path, "w", encoding=encoding) as dst:
        writer = Writer(dst, schema=schema, columns=cols, append=False)
        for line in src:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            writer.write(record)
        if seal_output:
            writer.seal()
        else:
            writer.close()


def convert_cjsonl_to_jsonl(
    src_path: str,
    dst_path: str,
    *,
    schemas: SchemasLike | None = None,
    encoding: str = "utf-8",
) -> None:
    """Convert cjsonl back to classic JSONL."""
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
    with open(dst_path, "w", encoding=encoding) as dst:
        for record in iter_cjsonl_records(src_path, schemas=schemas, encoding=encoding):
            dst.write(json_dumps_line(record))
