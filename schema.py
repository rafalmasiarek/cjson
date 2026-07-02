from __future__ import annotations

"""Schema and value-transform primitives for cjsonl."""

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class CjsonlError(ValueError):
    """Base error for cjsonl parsing/writing failures."""


@dataclass(slots=True)
class Transform:
    """
    User-defined per-column value transform.

    encode is applied before a value is written.
    decode is optional; if omitted, Reader returns the stored transformed value.

    This is intentionally generic. cjson does not ship hard-coded masking rules;
    callers decide how to shorten, normalize, tokenize, hash, or redact values.
    """

    encode: Callable[[Any], Any]
    decode: Callable[[Any], Any] | None = None


@dataclass(slots=True)
class StringParts:
    """
    Prefix/suffix string compression for repeated string parts.

    Example: prefix="00000" stores "000001" as "1" and decodes it back.

    strict=True means values must match the prefix/suffix; this keeps the row
    format compact because no fallback marker is needed.

    store_in_header=True writes prefix/suffix into cjsonl header metadata. Keep
    it False when those parts are sensitive or when a schema registry is used.
    """

    prefix: str = ""
    suffix: str = ""
    strict: bool = True
    store_in_header: bool = False

    def encode_value(self, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            if self.strict:
                raise CjsonlError(f"StringParts expected str, got {type(value).__name__}")
            return value
        out = value
        if self.prefix:
            if out.startswith(self.prefix):
                out = out[len(self.prefix):]
            elif self.strict:
                raise CjsonlError(f"value does not start with configured prefix {self.prefix!r}: {value!r}")
        if self.suffix:
            if out.endswith(self.suffix):
                out = out[: -len(self.suffix)]
            elif self.strict:
                raise CjsonlError(f"value does not end with configured suffix {self.suffix!r}: {value!r}")
        return out

    def decode_value(self, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            if self.strict:
                raise CjsonlError(f"StringParts expected stored str, got {type(value).__name__}")
            return value
        return f"{self.prefix}{value}{self.suffix}"


@dataclass(slots=True)
class Schema:
    """
    Optional schema/profile for compact cjsonl files.

    id:
        Short int/str written as "s". Prefer small ints for storage.
    columns:
        Stable column order. Rows are JSON arrays in exactly this order.
    bool_int:
        Columns encoded as 1/0 and decoded back to bool.
    bases:
        Base values by column name. Encoded value = raw value - base.
        Useful for timestamps, counters, monotonic ids, offsets, etc.
    defaults/default_markers:
        Default values by column name. If a record is missing the column or has
        the same value, the marker is stored instead. Marker default is 0.
    value_aliases:
        Reversible static aliases for repeated values. Backend writes normal
        values; cjsonl stores short aliases and Reader restores originals.
    transforms:
        User-defined value transforms. They may be lossy if decode is omitted.
    string_parts:
        Prefix/suffix string compression for columns with repeated string parts.
    name:
        Human-readable registry name; not written to compact files.
    """

    id: int | str | None
    columns: list[str]
    bool_int: set[str] = field(default_factory=set)
    bases: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    default_markers: dict[str, Any] = field(default_factory=dict)
    value_aliases: dict[str, dict[Any, Any]] = field(default_factory=dict)
    transforms: dict[str, Transform] = field(default_factory=dict)
    string_parts: dict[str, StringParts] = field(default_factory=dict)
    name: str | None = None

    def __post_init__(self) -> None:
        self.columns = [str(c) for c in self.columns]
        self.bool_int = {str(c) for c in self.bool_int}
        self.bases = {str(k): v for k, v in self.bases.items()}
        self.defaults = {str(k): v for k, v in self.defaults.items()}
        self.default_markers = {str(k): v for k, v in self.default_markers.items()}
        self.value_aliases = {str(k): dict(v) for k, v in self.value_aliases.items()}
        self.transforms = {str(k): v for k, v in self.transforms.items()}
        self.string_parts = {str(k): v for k, v in self.string_parts.items()}

    @classmethod
    def from_obj(cls, obj: "SchemaLike") -> "Schema":
        if isinstance(obj, Schema):
            return obj
        if isinstance(obj, (list, tuple)):
            return cls(id=None, columns=[str(c) for c in obj])
        if isinstance(obj, Mapping):
            cols = obj.get("columns") or obj.get("c")
            if cols is None:
                raise CjsonlError("schema mapping must contain 'columns' or 'c'")
            return cls(
                id=obj.get("id", obj.get("s")),
                name=obj.get("name"),
                columns=[str(c) for c in cols],
                bool_int=set(obj.get("bool_int", obj.get("bool", []))),
                bases=dict(obj.get("bases", obj.get("b", {}))),
                defaults=dict(obj.get("defaults", obj.get("d", {}))),
                default_markers=dict(obj.get("default_markers", obj.get("markers", {}))),
                value_aliases=dict(obj.get("value_aliases", obj.get("aliases", {}))),
                transforms=dict(obj.get("transforms", {})),
                string_parts=dict(obj.get("string_parts", {})),
            )
        raise TypeError(f"unsupported schema type: {type(obj)!r}")


SchemaLike = Schema | Mapping[str, Any] | list[str] | tuple[str, ...]
SchemasLike = Mapping[int | str, SchemaLike]
