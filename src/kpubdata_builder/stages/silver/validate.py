"""Silver schema validation (#46)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import polars as pl

from ...tabular.polars_helpers import DtypeSpec, _resolve_dtype
from .models import ValidationResult


@dataclass(frozen=True)
class ValidationProblem:
    """individual validation violations (#261)."""

    code: str
    field: str | None
    message: str


def validate_table(
    table: pl.DataFrame,
    *,
    required_columns: Sequence[str] = (),
    column_dtypes: Mapping[str, DtypeSpec] | None = None,
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
        expected = _resolve_dtype(expected_spec)
        actual = table.schema[column]
        if actual != expected:
            problems.append(
                ValidationProblem(
                    code="dtype_mismatch",
                    field=column,
                    message=f"컬럼 {column!r}: 예상 dtype {expected}, 실제 {actual}",
                )
            )
    return ValidationResult(ok=not problems, problems=tuple(problems))
