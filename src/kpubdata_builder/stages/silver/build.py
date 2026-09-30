"""Silver stage orchestration (#46).

Take BronzeArtifact and process through normalize → validate → summarize → preview
sequence, then assemble into SilverDataset.

Main functions:
    - build_silver_dataset: BronzeArtifact → SilverDataset
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import duckdb

from ...spec import ColumnNullTokens, DerivedColumn
from ...tabular import DEFAULT_PREVIEW_LIMIT
from ..bronze.models import BronzeArtifact
from .models import SilverDataset
from .normalize import normalize_table
from .preview import build_preview
from .summarize import build_schema, build_statistics
from .validate import validate_table


def build_silver_dataset(
    bronze: BronzeArtifact,
    *,
    required_columns: Sequence[str] = (),
    casts: Mapping[str, str] | None = None,
    rename: Mapping[str, str] | None = None,
    derived: Sequence[DerivedColumn] = (),
    read_as: Mapping[str, str] | None = None,
    null_tokens: Sequence[str] = (),
    column_null_tokens: Mapping[str, ColumnNullTokens] | None = None,
    coalesce: Mapping[str, Sequence[str]] | None = None,
    zfill: Mapping[str, int] | None = None,
    column_dtypes: Mapping[str, str] | None = None,
    preview_limit: int = DEFAULT_PREVIEW_LIMIT,
    connection: duckdb.DuckDBPyConnection | None = None,
    workdir: Path | None = None,
) -> SilverDataset:
    """Transform Bronze artifacts into Silver datasets.

    Args:
        bronze: source Bronze output.
        required_columns: required columns list for validation.
        casts: per-column dtype casting rules applied during normalization.
        rename: original field name -> canonical column name mapping (#611).
        derived: rule creating new column from existing (#611).
        read_as: type declaration for reading source columns.
        null_tokens: source notations representing missing values.
        column_null_tokens: missing notation recognized only in specific column (#623).
        coalesce: rule collecting per-generation alias columns into one (#620).
        zfill: rule padding canonical identifiers to declared width (#620).
        column_dtypes: per-column expected dtype rules for validation. Keys are column names,
            values are dtype names as a BuildSpec declares them.
        preview_limit: maximum rows to include in preview.
        connection: the source's DuckDB connection (#869); a private one if omitted.
            The caller keeps it open while the dataset is used and closes it.
        workdir: where the table's spill files go; the Bronze staging directory by
            default.

    Returns:
        SilverDataset: refined table and schema/statistics/preview/validation info.

    Raises:
        ValueError if preview_limit negative (#190).
    """
    if preview_limit < 0:
        raise ValueError(f"preview_limit must be >= 0, got {preview_limit}")
    table = normalize_table(
        bronze,
        casts=casts,
        rename=rename,
        derived=derived,
        read_as=read_as,
        null_tokens=null_tokens,
        column_null_tokens=column_null_tokens,
        coalesce=coalesce,
        zfill=zfill,
        connection=connection,
        workdir=workdir,
    )
    validation = validate_table(
        table, required_columns=required_columns, column_dtypes=column_dtypes
    )
    schema = build_schema(table)
    statistics = build_statistics(table)
    preview = build_preview(table, limit=preview_limit)

    return SilverDataset(
        table=table,
        schema=schema,
        statistics=statistics,
        preview=preview,
        validation=validation,
        source_bronze=bronze.source_key,
    )
