from __future__ import annotations

"""cjsonl codecs. Official v1 is dense-only: every record is a JSON array."""

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from .schema import CjsonlError, Schema, SchemaLike, SchemasLike, StringParts

# Official cjsonl v1 keys are intentionally short.
V = "v"       # cjsonl version
F = "f"       # optional custom codec id
S = "s"       # optional schema id
C = "c"       # columns when no schema id is used
B = "b"       # base values by column index, for delta encoding
D = "d"       # defaults by column index
E = "e"       # encodings metadata for self-describing no-schema files
A = "a"       # string-part aliases stored in header
FOOTER = "$"  # footer/seal marker
N = "n"       # row count
X = "x"       # min/max by column index in encoded space


@dataclass(slots=True)
class CjsonlMeta:
    version: int = 1
    codec: str | None = None
    schema_id: int | str | None = None
    columns: list[str] = field(default_factory=list)
    bases: dict[int, Any] = field(default_factory=dict)
    defaults: dict[int, Any] = field(default_factory=dict)
    default_markers: dict[int, Any] = field(default_factory=dict)
    bool_positions: set[int] = field(default_factory=set)
    alias_encode: dict[int, dict[Any, Any]] = field(default_factory=dict)
    alias_decode: dict[int, dict[Any, Any]] = field(default_factory=dict)
    transforms: dict[int, Any] = field(default_factory=dict)
    string_parts: dict[int, StringParts] = field(default_factory=dict)
    sealed: bool = False
    count: int = 0
    minmax: dict[int, list[Any]] = field(default_factory=dict)
    raw_headers: list[dict[str, Any]] = field(default_factory=list)
    raw_footer: dict[str, Any] | None = None


def json_dumps_line(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def positions(columns: list[str]) -> dict[str, int]:
    return {c: i for i, c in enumerate(columns)}


def normalize_index_dict(obj: Mapping[Any, Any] | None) -> dict[int, Any]:
    if not obj:
        return {}
    out: dict[int, Any] = {}
    for k, v in obj.items():
        out[int(k)] = v
    return out


def schema_registry_get(schemas: SchemasLike | None, schema_id: int | str | None) -> Schema | None:
    if schema_id is None or schemas is None:
        return None
    if schema_id in schemas:
        return Schema.from_obj(schemas[schema_id])
    alt = str(schema_id)
    if alt in schemas:  # type: ignore[operator]
        return Schema.from_obj(schemas[alt])  # type: ignore[index]
    raise CjsonlError(f"unknown cjsonl schema id: {schema_id!r}")


def _build_meta_from_schema(schema: Schema | None, header: Mapping[str, Any]) -> CjsonlMeta:
    schema_id = header.get(S) if S in header else (schema.id if schema else None)
    columns = list(schema.columns) if schema else [str(c) for c in header.get(C, [])]
    pos = positions(columns)

    meta = CjsonlMeta(
        version=int(header.get(V, 1)),
        codec=header.get(F),
        schema_id=schema_id,
        columns=columns,
        raw_headers=[dict(header)] if header else [],
    )

    if schema:
        for name, base in schema.bases.items():
            if name in pos:
                meta.bases[pos[name]] = base
        for name, default in schema.defaults.items():
            if name in pos:
                p = pos[name]
                meta.defaults[p] = default
                meta.default_markers[p] = schema.default_markers.get(name, 0)
        for name in schema.bool_int:
            if name in pos:
                meta.bool_positions.add(pos[name])
        for name, aliases in schema.value_aliases.items():
            if name in pos:
                p = pos[name]
                meta.alias_encode[p] = dict(aliases)
                meta.alias_decode[p] = {v: k for k, v in aliases.items()}
        for name, transform in schema.transforms.items():
            if name in pos:
                meta.transforms[pos[name]] = transform
        for name, parts in schema.string_parts.items():
            if name in pos:
                meta.string_parts[pos[name]] = parts

    # Header can override/add compact metadata for no-schema files and segment-specific bases.
    for p, base in normalize_index_dict(header.get(B)).items():
        meta.bases[p] = base
    for p, default in normalize_index_dict(header.get(D)).items():
        meta.defaults[p] = default
        meta.default_markers.setdefault(p, 0)

    enc = header.get(E, {}) or {}
    for p in [int(x) for x in enc.get("bool", [])]:
        meta.bool_positions.add(p)
    for p, marker in normalize_index_dict(enc.get("m", {})).items():
        meta.default_markers[p] = marker

    # Header-stored string parts. Useful when file should be self-describing.
    for p, spec in normalize_index_dict(header.get(A)).items():
        if isinstance(spec, Mapping):
            meta.string_parts[p] = StringParts(
                prefix=str(spec.get("p", "")),
                suffix=str(spec.get("suf", spec.get("u", ""))),
                strict=bool(spec.get("strict", True)),
                store_in_header=True,
            )
    return meta


def make_header(schema: Schema | None, columns: list[str], *, codec: str | None = None) -> dict[str, Any]:
    header: dict[str, Any] = {V: 1}
    if codec:
        header[F] = codec
    if schema and schema.id is not None:
        header[S] = schema.id
    else:
        header[C] = columns

    pos = positions(columns)
    bases: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    markers: dict[str, Any] = {}
    bools: list[int] = []
    string_parts_header: dict[str, Any] = {}

    if schema:
        for name, base in schema.bases.items():
            if name in pos:
                bases[str(pos[name])] = base

        # If no schema id, write metadata so file remains self-describing.
        if schema.id is None:
            for name, default in schema.defaults.items():
                if name in pos:
                    p = pos[name]
                    defaults[str(p)] = default
                    marker = schema.default_markers.get(name, 0)
                    if marker != 0:
                        markers[str(p)] = marker
            bools = [pos[name] for name in schema.bool_int if name in pos]

        # string_parts may be explicitly written even with schema id.
        for name, parts in schema.string_parts.items():
            if name in pos and parts.store_in_header:
                spec: dict[str, Any] = {}
                if parts.prefix:
                    spec["p"] = parts.prefix
                if parts.suffix:
                    spec["u"] = parts.suffix
                if not parts.strict:
                    spec["strict"] = False
                if spec:
                    string_parts_header[str(pos[name])] = spec

    if bases:
        header[B] = bases
    if defaults:
        header[D] = defaults
    enc: dict[str, Any] = {}
    if bools:
        enc["bool"] = sorted(bools)
    if markers:
        enc["m"] = markers
    if enc:
        header[E] = enc
    if string_parts_header:
        header[A] = string_parts_header
    return header


def encode_cell(value: Any, pos: int, meta: CjsonlMeta, *, missing: bool = False) -> Any:
    if missing:
        value = meta.defaults.get(pos)

    transform = meta.transforms.get(pos)
    if transform is not None:
        value = transform.encode(value)

    parts = meta.string_parts.get(pos)
    if parts is not None:
        value = parts.encode_value(value)

    alias = meta.alias_encode.get(pos)
    if alias is not None:
        value = alias.get(value, value)

    if pos in meta.bool_positions and value is not None:
        value = 1 if bool(value) else 0

    if pos in meta.defaults and value == meta.defaults[pos]:
        return meta.default_markers.get(pos, 0)

    if pos in meta.bases and value is not None:
        try:
            return value - meta.bases[pos]
        except TypeError:
            return value
    return value


def decode_cell(value: Any, pos: int, meta: CjsonlMeta) -> Any:
    if pos in meta.defaults and value == meta.default_markers.get(pos, 0):
        return meta.defaults[pos]

    if pos in meta.bases and value is not None:
        try:
            value = meta.bases[pos] + value
        except TypeError:
            pass

    if pos in meta.bool_positions and value is not None:
        value = bool(value)

    alias = meta.alias_decode.get(pos)
    if alias is not None:
        value = alias.get(value, value)

    parts = meta.string_parts.get(pos)
    if parts is not None:
        value = parts.decode_value(value)

    transform = meta.transforms.get(pos)
    if transform is not None and transform.decode is not None:
        value = transform.decode(value)
    return value


def encode_record(record: Mapping[str, Any], meta: CjsonlMeta) -> list[Any]:
    return [
        encode_cell(record[column], pos, meta, missing=False)
        if column in record else encode_cell(None, pos, meta, missing=True)
        for pos, column in enumerate(meta.columns)
    ]


def decode_row(row: list[Any], meta: CjsonlMeta) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for pos, column in enumerate(meta.columns):
        if pos < len(row):
            record[column] = decode_cell(row[pos], pos, meta)
        elif pos in meta.defaults:
            record[column] = meta.defaults[pos]
        else:
            record[column] = None
    return record


def track_minmax(meta: CjsonlMeta, row: list[Any]) -> None:
    for pos, value in enumerate(row):
        if value is None:
            continue
        if pos in meta.defaults and value == meta.default_markers.get(pos, 0):
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        bounds = meta.minmax.get(pos)
        if bounds is None:
            meta.minmax[pos] = [value, value]
        else:
            if value < bounds[0]:
                bounds[0] = value
            if value > bounds[1]:
                bounds[1] = value


class V1Codec:
    """Official dense-only cjsonl v1 codec."""

    id: str | None = None

    def make_header(self, schema: Schema | None, columns: list[str]) -> dict[str, Any]:
        return make_header(schema, columns, codec=self.id)

    def context_from_header(self, header: Mapping[str, Any], schemas: SchemasLike | None = None) -> CjsonlMeta:
        schema = schema_registry_get(schemas, header.get(S))
        return _build_meta_from_schema(schema, header)

    def encode_record(self, record: Mapping[str, Any], meta: CjsonlMeta) -> list[Any]:
        return encode_record(record, meta)

    def decode_row(self, row: list[Any], meta: CjsonlMeta) -> dict[str, Any]:
        return decode_row(row, meta)

    def make_footer(self, meta: CjsonlMeta) -> dict[str, Any]:
        footer: dict[str, Any] = {FOOTER: 1, N: meta.count}
        if meta.minmax:
            footer[X] = {str(k): v for k, v in sorted(meta.minmax.items())}
        return footer


class CodecRegistry:
    def __init__(self) -> None:
        self._codecs: dict[str | None, V1Codec] = {None: V1Codec()}

    def register(self, codec_id: str, codec: V1Codec) -> None:
        codec.id = codec_id
        self._codecs[codec_id] = codec

    def get(self, codec_id: str | None) -> V1Codec:
        if codec_id in self._codecs:
            return self._codecs[codec_id]
        raise CjsonlError(f"unknown cjsonl codec: {codec_id!r}")


_CODEC_REGISTRY = CodecRegistry()


def register_codec(codec_id: str, codec: V1Codec) -> None:
    """Register a custom cjsonl codec. Official v1 is used when no codec id is present."""
    _CODEC_REGISTRY.register(codec_id, codec)


def get_codec(codec_id: str | None = None) -> V1Codec:
    return _CODEC_REGISTRY.get(codec_id)


def context_from_schema(schema: Schema | None, columns: list[str], *, codec_id: str | None = None) -> CjsonlMeta:
    """Build an in-memory write context directly from a Schema object."""
    header: dict[str, Any] = {V: 1}
    if codec_id:
        header[F] = codec_id
    if schema is not None and schema.id is not None:
        header[S] = schema.id
    else:
        header[C] = columns
    meta = _build_meta_from_schema(schema, header)
    meta.columns = columns
    return meta
