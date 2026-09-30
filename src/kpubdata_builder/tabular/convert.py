"""dict ↔ Polars conversion utilities (#49).

Handle bidirectional conversion between raw JSON-like records and Polars DataFrame.
Use plain dict on records side to avoid forcing Polars types on public API.

Key functions:
    - records_to_dataframe: record sequence → pl.DataFrame
    - dataframe_to_records: pl.DataFrame → plain dict list
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import polars as pl

from ..errors import TabularError
from ..spec import JsonValue

# Upper limit of integer absolute value exactly representable by IEEE-754 double (2^53).
# When integers beyond this range mix with floats in same column, Polars
# upcasts to f64 and silently rounds.
_SAFE_INTEGER = 2**53

# Sentinel for type shape unification.
_NULL = object()  # Unknown/absent (null) — compatible with any type.
_CONFLICT = object()  # Incompatible heterogeneous types.


def _shape(value: object) -> object:
    """Create type shape of value (including nesting).

    Group int/float as "num" to allow normal numeric mixing like [1, 2.5], but recurse
    into list/struct internals to distinguish nested heterogeneous types like
    list[int] vs list[str], struct{x:int} vs struct{x:str} (#199).
    """
    if value is None:
        return _NULL
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return ("map", {str(k): _shape(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        elem: object = _NULL
        for item in value:
            elem = _unify(elem, _shape(item))
        return ("list", elem)
    return ("scalar", type(value).__name__)


def _collect_numeric_kinds(value: object, path: str, acc: dict[str, set[str]]) -> None:
    """Collect numeric kinds by structural path for precision risk detection (#198).

    List elements merge into same dtype, so collect under same path (``path[]``).
    Struct fields become separate columns, so split by key path (``path.key``).
    Recurse into nested list/struct internals, not just top-level values, so catch
    f64 upcast rounding even in nested places like ``[{"v": [big_int]}, {"v": [2.5]}]``.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        acc.setdefault(path, set()).add("float")
    elif isinstance(value, int):
        if abs(value) > _SAFE_INTEGER:
            acc.setdefault(path, set()).add("unsafe_int")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _collect_numeric_kinds(item, f"{path}.{key}", acc)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_numeric_kinds(item, f"{path}[]", acc)


def _unify(a: object, b: object) -> object:
    """Unify two type shapes. null compatible with anything, return _CONFLICT on conflict."""
    if a is _CONFLICT or b is _CONFLICT:
        return _CONFLICT
    if a is _NULL:
        return b
    if b is _NULL:
        return a
    if isinstance(a, str) and isinstance(b, str):
        return a if a == b else _CONFLICT
    if isinstance(a, tuple) and isinstance(b, tuple) and a[0] == b[0]:
        kind = a[0]
        if kind == "list":
            unified = _unify(a[1], b[1])
            return _CONFLICT if unified is _CONFLICT else ("list", unified)
        if kind == "map":
            merged: dict[str, object] = dict(cast(dict[str, object], a[1]))
            for key, shape in cast(dict[str, object], b[1]).items():
                # Keys appearing in only one side treated as optional field,
                # unified with null (allowed).
                merged[key] = _unify(merged.get(key, _NULL), shape)
                if merged[key] is _CONFLICT:
                    return _CONFLICT
            return ("map", merged)
        if kind == "scalar":
            return a if a[1] == b[1] else _CONFLICT
    return _CONFLICT


def _apply_read_as(
    record: dict[str, JsonValue], declared: Mapping[str, str]
) -> dict[str, JsonValue]:
    """Create copy with declared column values cast to their types.

    Keep null as null — converting missing to "None" string breaks missing rate
    measurement entirely.
    """
    converted = dict(record)
    for column, dtype in declared.items():
        if column not in converted:
            continue
        value = converted[column]
        if value is None:
            continue
        if dtype == "str":
            converted[column] = str(value)
        else:
            raise TabularError(f"unsupported read_as type {dtype!r} for column {column!r}")
    return converted


class RecordTypeScan:
    """The raw-JSON type checks of :func:`records_to_dataframe`, one record at a time.

    An engine's own inference sees only the table it settled on; these checks see every
    value first (#187, #198, #199), nested lists and maps included. Fed record by record,
    they hold one shape per column rather than the records, so a Bronze file of any size
    can be checked as it is read (#868) — with the same errors, in the same order.
    """

    def __init__(self, *, read_as: Mapping[str, str] | None = None) -> None:
        self._declared = dict(read_as or {})
        self._shapes: dict[str, object] = {}
        self._conflicts: list[str] = []
        self._numeric_kinds: dict[str, set[str]] = {}

    @property
    def columns(self) -> tuple[str, ...]:
        """Every column seen, in first-seen order."""
        return tuple(self._shapes)

    def add(self, record: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Check one record; returns it with ``read_as`` applied."""
        if self._declared:
            record = _apply_read_as(record, self._declared)
        for key, value in record.items():
            _collect_numeric_kinds(value, key, self._numeric_kinds)
            shape = _unify(self._shapes.get(key, _NULL), _shape(value))
            self._shapes[key] = shape
            if shape is _CONFLICT and key not in self._conflicts:
                self._conflicts.append(key)
        return record

    def check(self) -> None:
        """Raise for what the records seen so far would silently coerce.

        Raises:
            TabularError: A column mixes incompatible types, or floats with integers
                beyond ±2^53.
        """
        if self._conflicts:
            raise TabularError(
                "heterogeneous column types detected (refusing to silently coerce): "
                f"{sorted(self._conflicts)}"
            )
        precision_risk = sorted(
            path
            for path, kinds in self._numeric_kinds.items()
            if "float" in kinds and "unsafe_int" in kinds
        )
        if precision_risk:
            raise TabularError(
                "integer precision loss risk: columns mix floats with integers beyond the "
                f"IEEE-754 safe range (±2^53) and would round on f64 upcast: {precision_risk}. "
                "Use an explicit cast to keep these columns as strings or integers."
            )


def check_case_fold_collisions(columns: Sequence[str]) -> None:
    """Refuse columns whose names differ only in letter case (#868, R7).

    SQL engines, DuckDB included, match column names case-insensitively, quoted or not:
    ``Name`` and ``name`` would be one column, or one would be renamed to ``name_1``
    behind the user's back. Neither is acceptable, so the table is refused and the user
    renames or coalesces the columns in the source's schema.

    Raises:
        TabularError: Two or more columns fold to the same name.
    """
    groups: dict[str, list[str]] = {}
    for column in columns:
        groups.setdefault(column.casefold(), []).append(column)
    clashes = sorted(sorted(names) for names in groups.values() if len(names) > 1)
    if clashes:
        raise TabularError(
            "columns differ only in letter case, which SQL reads as one column: "
            f"{clashes}. Rename or coalesce them in the source's schema."
        )


def apply_read_as(
    record: dict[str, JsonValue], declared: Mapping[str, str]
) -> dict[str, JsonValue]:
    """``record`` with ``read_as`` applied — declared columns' values as text."""
    return _apply_read_as(record, dict(declared))


def records_to_dataframe(
    records: Sequence[dict[str, JsonValue]],
    *,
    read_as: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """Convert raw record mapping to Polars DataFrame.

    Relying only on Polars auto-inference risks silent forced conversion of mixed-type
    columns before validation, changing raw values. Detect first and fail fast with
    clear error:

    - Incompatible types mixed in column (and nested list/struct) (#187, #199).
    - Large integers mixed with float in same column, f64 upcast loses precision (#198).

    Args:
        records: JSON-compatible record sequence.
        read_as: Type declaration for source columns (``{column: "str"}``). Public data
            sometimes gives same column different types per record (e.g., lot number mostly
            string but some records integer). Read declared columns as their type,
            reject undeclared mixed types as-is — preserves #187 contract against silent
            forced conversion.

    Returns:
        pl.DataFrame: DataFrame reflecting column structure of input records.

    Raises:
        TabularError: If heterogeneous types mixed or integer precision loss possible.
    """
    scan = RecordTypeScan(read_as=read_as)
    records = [scan.add(record) for record in records]
    scan.check()

    # infer_schema_length=None: scan all records to infer dtype. With default inference window
    # (first few rows) only, first float appearing outside window silently truncates to int in
    # columns inferred as Int64 (#216).
    return pl.DataFrame(list(records), infer_schema_length=None)


def dataframe_to_records(df: pl.DataFrame) -> list[dict[str, JsonValue]]:
    """Convert Polars DataFrame to list of plain dict records.

    Args:
        df: DataFrame to convert.

    Returns:
        list[dict[str, JsonValue]]: Row-by-row dict representation.
    """
    return cast(list[dict[str, JsonValue]], df.to_dicts())


__all__ = [
    "RecordTypeScan",
    "apply_read_as",
    "check_case_fold_collisions",
    "dataframe_to_records",
    "records_to_dataframe",
]
