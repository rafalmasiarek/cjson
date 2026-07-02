from __future__ import annotations

"""Append-only compact JSON Lines API."""

import gzip
import io
import json
import os
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence, TextIO

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


def _resolve_columns(select: list[str], meta: CjsonlMeta) -> list[tuple[str, int | None]]:
    """Resolve selected column names to (name, position) pairs, preserving user order.

    Columns absent from the current segment get position None and decode as None.
    This makes columns= work correctly across multi-segment (evolved) files.
    """
    pos_map = {col: i for i, col in enumerate(meta.columns)}
    return [(col, pos_map.get(col)) for col in select]


class Writer:
    """
    Append-only cjsonl writer.

    The writer writes exactly one header when the target is empty, then writes
    each record as a compact JSON array. Use seal() before rotating/gzipping.

    seal_on_close=True is useful for one-shot batch writes (dump_cjsonl).
    Leave it False for append workflows where the file stays active across
    multiple writers.
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
        seal_on_close: bool = False,
        on_seal: Callable[[Writer], None] | None = None,
        on_evolve: Callable[[Writer], None] | None = None,
        on_close: Callable[[Writer], None] | None = None,
    ) -> None:
        self.fp = fp
        self._close_file = close_file
        self._seal_on_close = seal_on_close
        self._on_seal = on_seal
        self._on_evolve = on_evolve
        self._on_close = on_close
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
        self._closed = False
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
            return os.fstat(fp.fileno()).st_size > 0
        except Exception:
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
        if self._closed:
            raise CjsonlError("cannot write to closed cjsonl writer")
        if self._sealed:
            raise CjsonlError("cannot write to sealed cjsonl writer")
        row = self.codec.encode_record(record, self.meta)
        self.fp.write(json_dumps_line(row))
        self.meta.count += 1
        track_minmax(self.meta, row)

    @property
    def columns(self) -> list[str]:
        return self.meta.columns

    def evolve(self, new_columns: Iterable[str]) -> None:
        """Write a new segment header with an updated column list.

        Existing records are not rewritten. The Reader handles multiple headers
        transparently — each segment is decoded with its own meta.
        Use when the record schema gains new fields mid-stream.
        """
        if self._closed:
            raise CjsonlError("cannot evolve a closed cjsonl writer")
        if self._sealed:
            raise CjsonlError("cannot evolve a sealed cjsonl writer")
        cols = [str(c) for c in new_columns]
        if cols == self.meta.columns:
            return
        self.meta = context_from_schema(self.schema, cols, codec_id=self.codec.id)
        self.fp.write(json_dumps_line(self.codec.make_header(self.schema, cols)))
        if self._on_evolve is not None:
            self._on_evolve(self)

    def seal(self) -> None:
        if self._sealed:
            return
        footer = self.codec.make_footer(self.meta)
        self.fp.write(json_dumps_line(footer))
        self.fp.flush()
        self._sealed = True
        if self._on_seal is not None:
            self._on_seal(self)

    def close(self) -> None:
        if self._closed:
            return
        if self._seal_on_close and not self._sealed:
            self.seal()
        else:
            self.fp.flush()
        if self._close_file:
            self.fp.close()
        self._closed = True
        if self._on_close is not None:
            self._on_close(self)

    def __enter__(self) -> "Writer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class Reader:
    """Streaming cjsonl reader. Iterates normal dict records.

    Pass columns=[...] to decode only a subset of columns. The result dict
    preserves the order of the columns argument, not the file order.
    Unknown column names raise CjsonlError immediately when the header is read.
    """

    def __init__(
        self,
        fp: TextIO,
        *,
        schemas: SchemasLike | None = None,
        columns: Sequence[str] | None = None,
    ) -> None:
        self.fp = fp
        self.schemas = schemas
        self.meta: CjsonlMeta | None = None
        self.codec: V1Codec = get_codec(None)
        self._select: list[str] | None = list(columns) if columns is not None else None
        self._selected: list[tuple[str, int]] | None = None

    def _decode_selected(self, row: list[Any]) -> dict[str, Any]:
        assert self.meta is not None and self._selected is not None
        out: dict[str, Any] = {}
        for col, pos in self._selected:
            if pos is None:
                out[col] = None
            elif pos < len(row):
                out[col] = decode_cell(row[pos], pos, self.meta)
            elif pos in self.meta.defaults:
                out[col] = self.meta.defaults[pos]
            else:
                out[col] = None
        return out

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
                    if self._select is not None:
                        self._selected = _resolve_columns(self._select, self.meta)
                    continue
                # Unknown object line is metadata for a custom/user layer. Skip.
                continue

            if isinstance(item, list):
                if self.meta is None:
                    raise CjsonlError("cjsonl data row encountered before header")
                if self._selected is not None:
                    yield self._decode_selected(item)
                else:
                    yield self.codec.decode_row(item, self.meta)


class RowReader:
    """
    Streaming cjsonl row reader. Iterates row arrays.

    Pass columns=[...] to return only selected values (in caller's order).
    Pass raw=True to skip decoding entirely — values are returned as stored
    in the file (encoded). Fastest read path; useful for filtering on encoded
    values (e.g. alias ints, delta timestamps) without restoring originals.
    raw=True + columns=[...] returns encoded values for selected columns only.
    """

    def __init__(
        self,
        fp: TextIO,
        *,
        schemas: SchemasLike | None = None,
        columns: Sequence[str] | None = None,
        raw: bool = False,
    ) -> None:
        self.fp = fp
        self.schemas = schemas
        self.meta: CjsonlMeta | None = None
        self.codec: V1Codec = get_codec(None)
        self._select: list[str] | None = list(columns) if columns is not None else None
        self._selected: list[tuple[str, int]] | None = None
        self._raw = raw

    def _decode_selected_row(self, row: list[Any]) -> list[Any]:
        assert self.meta is not None and self._selected is not None
        out: list[Any] = []
        for _col, pos in self._selected:
            if pos is None:
                out.append(None)
            elif pos < len(row):
                out.append(decode_cell(row[pos], pos, self.meta))
            elif pos in self.meta.defaults:
                out.append(self.meta.defaults[pos])
            else:
                out.append(None)
        return out

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
                    if self._select is not None:
                        self._selected = _resolve_columns(self._select, self.meta)
                continue
            if isinstance(item, list):
                if self.meta is None:
                    raise CjsonlError("cjsonl data row encountered before header")
                if self._raw:
                    if self._selected is not None:
                        row_len = len(item)
                        yield [item[pos] if (pos is not None and pos < row_len) else None for _col, pos in self._selected]
                    else:
                        yield item  # no copy — caller gets the parsed list directly
                elif self._selected is not None:
                    yield self._decode_selected_row(item)
                else:
                    decoders = self.meta.decoders
                    if decoders:
                        row_out: list[Any] = []
                        for i, v in enumerate(item):
                            d = decoders[i]
                            row_out.append(d(v) if d is not None else v)
                        yield row_out
                    else:
                        yield [decode_cell(v, i, self.meta) for i, v in enumerate(item)]


def open_writer(
    path: str,
    *,
    schema: SchemaLike | None = None,
    columns: Iterable[str] | None = None,
    encoding: str = "utf-8",
    on_seal: Callable[[Writer], None] | None = None,
    on_evolve: Callable[[Writer], None] | None = None,
    on_close: Callable[[Writer], None] | None = None,
) -> Writer:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fp = open(path, "a+", encoding=encoding, buffering=1)
    return Writer(
        fp, schema=schema, columns=columns, append=True, close_file=True,
        on_seal=on_seal, on_evolve=on_evolve, on_close=on_close,
    )


def append(path: str, record: Mapping[str, Any], *, schema: SchemaLike | None = None, columns: Iterable[str] | None = None) -> None:
    """Append one record to a cjsonl file."""
    with open_writer(path, schema=schema, columns=columns) as w:
        w.write(record)


def iter_cjsonl_records(
    path: str,
    *,
    schemas: SchemasLike | None = None,
    columns: Sequence[str] | None = None,
    encoding: str = "utf-8",
) -> Iterator[dict[str, Any]]:
    with open(path, "r", encoding=encoding) as fp:
        yield from Reader(fp, schemas=schemas, columns=columns)


def iter_cjsonl_rows(
    path: str,
    *,
    schemas: SchemasLike | None = None,
    columns: Sequence[str] | None = None,
    raw: bool = False,
    encoding: str = "utf-8",
) -> Iterator[list[Any]]:
    with open(path, "r", encoding=encoding) as fp:
        yield from RowReader(fp, schemas=schemas, columns=columns, raw=raw)


def load_cjsonl(
    path: str,
    *,
    schemas: SchemasLike | None = None,
    columns: Sequence[str] | None = None,
    encoding: str = "utf-8",
) -> list[dict[str, Any]]:
    return list(iter_cjsonl_records(path, schemas=schemas, columns=columns, encoding=encoding))


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
        with Writer(fp, schema=schema, columns=columns, append=False, seal_on_close=seal) as writer:
            for record in records:
                writer.write(record)


def _scan_headers(fp: TextIO, *, schemas: SchemasLike | None = None) -> CjsonlMeta:
    """Parse only header dict lines, skip data rows. Used by scan() for sealed files."""
    meta: CjsonlMeta | None = None
    codec: V1Codec = get_codec(None)
    for raw_line in fp:
        line = raw_line.strip()
        if not line or line[0] != '{':  # data rows start with '[', skip cheaply
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get(FOOTER) == 1:
            continue
        if V in item or C in item or S in item:
            codec = get_codec(item.get(F))
            meta = codec.context_from_header(item, schemas)
    return meta or CjsonlMeta()


def _scan_fp(fp: TextIO, *, schemas: SchemasLike | None = None) -> CjsonlMeta:
    meta: CjsonlMeta | None = None
    codec: V1Codec = get_codec(None)
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


def scan(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> CjsonlMeta:
    """Scan cjsonl metadata/count/minmax. O(headers) for sealed files, O(rows) for unsealed."""
    footer = read_footer(path, encoding=encoding)
    if footer is not None:
        with open(path, "r", encoding=encoding) as fp:
            meta = _scan_headers(fp, schemas=schemas)
        meta.sealed = True
        meta.raw_footer = footer.raw_footer
        meta.count = footer.count
        meta.minmax = footer.minmax
        return meta
    with open(path, "r", encoding=encoding) as fp:
        return _scan_fp(fp, schemas=schemas)


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
    columns: Sequence[str] | None = None,
    encoding: str = "utf-8",
) -> Iterator[dict[str, Any]]:
    comp = compressor or GzipCompressor()
    with comp.open_text(path, encoding=encoding) as fp:
        yield from Reader(fp, schemas=schemas, columns=columns)


def iter_cjsonl_compressed_rows(
    path: str,
    *,
    schemas: SchemasLike | None = None,
    compressor: Compressor | None = None,
    columns: Sequence[str] | None = None,
    raw: bool = False,
    encoding: str = "utf-8",
) -> Iterator[list[Any]]:
    comp = compressor or GzipCompressor()
    with comp.open_text(path, encoding=encoding) as fp:
        yield from RowReader(fp, schemas=schemas, columns=columns, raw=raw)


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
    with comp.open_text(path, encoding=encoding) as fp:
        return _scan_fp(fp, schemas=schemas)


def scan_gzip(path: str, *, schemas: SchemasLike | None = None, encoding: str = "utf-8") -> CjsonlMeta:
    return scan_compressed(path, schemas=schemas, compressor=GzipCompressor(), encoding=encoding)


def read_footer(path: str, *, encoding: str = "utf-8") -> CjsonlMeta | None:
    """O(1) footer read for sealed plain .cjsonl files — seeks from EOF.

    Returns a CjsonlMeta with sealed=True, count, and minmax populated.
    Returns None if the file is not sealed or cannot be read.
    Does not work for compressed files; use scan_compressed() for those.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            chunk_size = min(4096, size)
            f.seek(-chunk_size, os.SEEK_END)
            chunk = f.read()
    except OSError:
        return None

    lines = chunk.split(b"\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None

    try:
        item = json.loads(lines[-1].decode(encoding))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(item, dict) or item.get(FOOTER) != 1:
        return None

    meta = CjsonlMeta()
    meta.sealed = True
    meta.raw_footer = item
    meta.count = int(item.get(N, 0))
    meta.minmax = {int(k): v for k, v in item.get(X, {}).items()}
    return meta


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
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
    with open(src_path, "r", encoding=encoding) as src, open(dst_path, "w", encoding=encoding) as dst:
        writer: Writer | None = None
        for line in src:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise CjsonlError("classic JSONL input must contain object records")
            if writer is None:
                cols = columns if columns is not None else (
                    [str(k) for k in record.keys()] if schema is None else None
                )
                writer = Writer(dst, schema=schema, columns=cols, append=False)
            writer.write(record)
        if writer is None:
            return
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
