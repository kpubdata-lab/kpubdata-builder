"""What a column means, kept apart from how it is stored and sent (#813, ADR 0019).

A column's metadata used to be three fields: `dtype`, `logical_type` and `wire_encoding`.
Those say how the values are stored and how they cross the wire. They do not say what
the values *are* — a code, a measure, a date, a period — how to label them, or which
unit they are counted in. A client that needs that has to guess, or load it into a field
that already means something else.

This module adds three separate, optional hints, each carrying where it came from:

    semantic  what kind of value: `code`, `measure`, `date`, `period`, or a kind a newer
              Engine adds. A client that does not know a kind shows the raw value.
    display   a label, a description and a display format.
    unit      a unit name and a scale (1000 for "thousand won").

When several sources describe the same column, each hint is taken from the highest
source that has one (`ORIGIN_PRIORITY`): a user's annotation, then the Core dataset
spec, then the catalog, then an Engine inference. The hints are metadata only. They
never change the storage type, the wire encoding, a cast, an aggregation or how a row is
identified, and none of them can overwrite the licence, a PII decision or the source's
provenance — those are not hints and are not decided here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from ..spec import JsonValue

Origin = Literal["user_annotation", "core_spec", "catalog", "engine_inferred"]

ORIGIN_PRIORITY: tuple[Origin, ...] = (
    "user_annotation",
    "core_spec",
    "catalog",
    "engine_inferred",
)
"""Highest first. A hint from an earlier origin wins over one from a later origin."""

KNOWN_SEMANTIC_KINDS: frozenset[str] = frozenset({"code", "measure", "date", "period"})
"""Kinds this Engine emits. The contract keeps the field an open string: a client that
meets a kind outside its own list shows the raw value instead of failing."""

# Core `FieldConstraints.format` values that name a date or a period. Anything else is
# carried to `display.format` verbatim and says nothing about the kind.
_DATE_FORMATS = frozenset({"date", "yyyymmdd", "yyyy-mm-dd", "yyyy.mm.dd"})
_PERIOD_FORMATS = frozenset({"yyyymm", "yyyy-mm", "yyyy.mm", "yyyy", "yyyyq", "yyyy-qq"})


@dataclass(frozen=True)
class SemanticHint:
    """The kind of value a column holds, and who said so."""

    kind: str
    origin: Origin


@dataclass(frozen=True)
class DisplayHint:
    """How to present a column: label, description and display format."""

    origin: Origin
    label: str | None = None
    description: str | None = None
    format: str | None = None


@dataclass(frozen=True)
class UnitHint:
    """The unit a measure is counted in. `scale` multiplies the stored value: 1000 means
    a stored 12 is 12,000 of `name`. The stored value itself is never rescaled."""

    name: str
    origin: Origin
    scale: int | float | None = None


@dataclass(frozen=True)
class ColumnSemantics:
    """The optional meaning of one column. Every part may be absent."""

    semantic: SemanticHint | None = None
    display: DisplayHint | None = None
    unit: UnitHint | None = None

    def is_empty(self) -> bool:
        return self.semantic is None and self.display is None and self.unit is None


def _rank(origin: Origin) -> int:
    return ORIGIN_PRIORITY.index(origin)


def resolve(layers: Iterable[ColumnSemantics]) -> ColumnSemantics:
    """Combine what several sources say about one column.

    Each part is chosen on its own: the display label can come from a user's annotation
    while the unit comes from the Core spec. Within a part the highest origin wins; two
    hints from the same origin keep the first one given, so the result does not depend
    on anything but the order the caller chose.
    """
    semantic: SemanticHint | None = None
    display: DisplayHint | None = None
    unit: UnitHint | None = None
    for layer in layers:
        if layer.semantic is not None and (
            semantic is None or _rank(layer.semantic.origin) < _rank(semantic.origin)
        ):
            semantic = layer.semantic
        if layer.display is not None and (
            display is None or _rank(layer.display.origin) < _rank(display.origin)
        ):
            display = layer.display
        if layer.unit is not None and (
            unit is None or _rank(layer.unit.origin) < _rank(unit.origin)
        ):
            unit = layer.unit
    return ColumnSemantics(semantic=semantic, display=display, unit=unit)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def from_field_descriptor(descriptor: object) -> ColumnSemantics:
    """Map a Core `FieldDescriptor` onto column semantics (origin `core_spec`).

    `title` becomes the display label, `description` the display description and
    `constraints.format` the display format. The format also names the kind when it is a
    date or a period format; any other format says nothing about the kind. Core has no
    unit field, so no unit comes from here. `type` and `nullable` are ignored: they
    describe the source, and the storage type is decided by the data Engine holds.

    Read by attribute so that this module does not depend on one kpubdata release.
    """
    label = _text(getattr(descriptor, "title", None))
    description = _text(getattr(descriptor, "description", None))
    constraints = getattr(descriptor, "constraints", None)
    fmt = _text(getattr(constraints, "format", None)) if constraints is not None else None

    display = (
        DisplayHint(origin="core_spec", label=label, description=description, format=fmt)
        if label or description or fmt
        else None
    )
    semantic: SemanticHint | None = None
    if fmt is not None:
        key = fmt.strip().lower()
        if key in _DATE_FORMATS:
            semantic = SemanticHint(kind="date", origin="core_spec")
        elif key in _PERIOD_FORMATS:
            semantic = SemanticHint(kind="period", origin="core_spec")
    return ColumnSemantics(semantic=semantic, display=display)


def semantics_json(semantics: ColumnSemantics) -> dict[str, JsonValue]:
    """The wire form: only the parts that are present, and within them only set fields."""
    out: dict[str, JsonValue] = {}
    if semantics.semantic is not None:
        out["semantic"] = {"kind": semantics.semantic.kind, "origin": semantics.semantic.origin}
    if semantics.display is not None:
        display: dict[str, JsonValue] = {"origin": semantics.display.origin}
        for key in ("label", "description", "format"):
            value = getattr(semantics.display, key)
            if value is not None:
                display[key] = value
        out["display"] = display
    if semantics.unit is not None:
        unit: dict[str, JsonValue] = {
            "name": semantics.unit.name,
            "origin": semantics.unit.origin,
        }
        if semantics.unit.scale is not None:
            unit["scale"] = semantics.unit.scale
        out["unit"] = unit
    return out


def with_semantics(
    meta: Iterable[Mapping[str, JsonValue]],
    semantics: Mapping[str, ColumnSemantics] | None,
) -> list[dict[str, JsonValue]]:
    """Add each column's semantics to its metadata entry, without touching the rest.

    `name`, `logical_type`, `wire_encoding` and every other key already present are
    copied as they are: a hint cannot change how a column is stored or sent. A column
    with no semantics, or empty ones, gets no new keys at all, so a response for a table
    nobody described is byte-for-byte what it was before.
    """
    out: list[dict[str, JsonValue]] = []
    for entry in meta:
        item = dict(entry)
        name = item.get("name")
        sem = semantics.get(name) if semantics and isinstance(name, str) else None
        if sem is not None and not sem.is_empty():
            for key, value in semantics_json(sem).items():
                item.setdefault(key, value)
        out.append(item)
    return out


__all__ = [
    "KNOWN_SEMANTIC_KINDS",
    "ORIGIN_PRIORITY",
    "ColumnSemantics",
    "DisplayHint",
    "Origin",
    "SemanticHint",
    "UnitHint",
    "from_field_descriptor",
    "resolve",
    "semantics_json",
    "with_semantics",
]
