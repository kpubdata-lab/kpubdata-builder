"""Silver preview (#46)."""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from ...spec import JsonValue
from ...tabular import DEFAULT_PREVIEW_LIMIT, PreviewSlice, generate_preview
from ...tabular.convert import dataframe_to_records


def build_preview(table: pl.DataFrame, *, limit: int = DEFAULT_PREVIEW_LIMIT) -> PreviewSlice:
    """generates preview slice of top N rows."""
    return generate_preview(table, limit=limit)


def select_preview_rows(
    table: pl.DataFrame, indices: Sequence[int]
) -> tuple[dict[str, JsonValue], ...]:
    """extracts records at specified row indices (#497)."""
    if not indices:
        return ()
    return tuple(dataframe_to_records(table[list(indices)]))
