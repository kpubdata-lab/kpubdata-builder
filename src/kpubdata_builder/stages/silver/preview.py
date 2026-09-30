"""Silver preview (#46), on DuckDB (#869)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from ...spec import JsonValue
from ...tabular import DEFAULT_PREVIEW_LIMIT, PreviewSlice
from ...tabular.duckdb_load import TableHandle
from ...tabular.duckdb_summary import preview_of


def build_preview(table: TableHandle, *, limit: int = DEFAULT_PREVIEW_LIMIT) -> PreviewSlice:
    """generates preview slice of top N rows."""
    return preview_of(table.connection, table.table, limit=limit)


def select_preview_rows(
    table: TableHandle, indices: Sequence[int]
) -> tuple[dict[str, JsonValue], ...]:
    """extracts records at specified row indices (#497)."""
    if not indices:
        return ()
    return cast(tuple[dict[str, JsonValue], ...], table.rows_at(indices))
