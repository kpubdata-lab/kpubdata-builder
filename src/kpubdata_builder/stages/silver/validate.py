"""Silver schema validation (#46)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ...tabular.duckdb_load import TableHandle
from .models import ValidationResult


@dataclass(frozen=True)
class ValidationProblem:
    """individual validation violations (#261)."""

    code: str
    field: str | None
    message: str


#: Declared dtype names and the Builder dtype each one means (``cast_names.NAMED_TARGETS``).
_EXPECTED: Mapping[str, str] = {
    "bool": "Boolean",
    "boolean": "Boolean",
    "date": "Date",
    "datetime": "Datetime(time_unit='us', time_zone=None)",
    "float": "Float64",
    "float64": "Float64",
    "int": "Int64",
    "int64": "Int64",
    "str": "String",
    "string": "String",
    "utf8": "String",
}


def expected_dtype(spec: str) -> str:
    """The Builder dtype a declared dtype name stands for.

    Raises:
        ValueError: The name is not one Builder knows.
    """
    normalized = spec.strip().lower()
    if normalized not in _EXPECTED:
        supported = ", ".join(sorted(_EXPECTED))
        raise ValueError(f"Unsupported dtype: {spec!r}. Supported: {supported}")
    return _EXPECTED[normalized]


def validate_table(
    table: TableHandle,
    *,
    required_columns: Sequence[str] = (),
    column_dtypes: Mapping[str, str] | None = None,
) -> ValidationResult:
    """validates required column existence and declared dtype match."""
    problems: list[ValidationProblem] = []
    missing = [column for column in required_columns if column not in table.columns]
    if missing:
        for col in missing:
            problems.append(
                ValidationProblem(
                    code="missing_column",
                    field=col,
                    message=f"필수 컬럼 누락: {col}",
                )
            )
    for column, expected_spec in (column_dtypes or {}).items():
        if column not in table.columns:
            problems.append(
                ValidationProblem(
                    code="dtype_mismatch",
                    field=column,
                    message=f"컬럼 {column!r} 없음; dtype 검증 불가",
                )
            )
            continue
        expected = expected_dtype(expected_spec)
        actual = table.dtypes[table.columns.index(column)]
        if actual != expected:
            problems.append(
                ValidationProblem(
                    code="dtype_mismatch",
                    field=column,
                    message=f"컬럼 {column!r}: 예상 dtype {expected}, 실제 {actual}",
                )
            )
    return ValidationResult(ok=not problems, problems=tuple(problems))
