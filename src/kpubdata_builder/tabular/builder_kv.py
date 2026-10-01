"""The Parquet key-value metadata keys Builder writes (#869).

``TableHandle.write_parquet`` records each column's Builder dtype under :data:`KV_KEY`,
and the internal name of any column DuckDB cannot write under its own under
:data:`KV_NAMES_KEY`. Readers — ``duckdb_load.load_parquet`` and the query sandbox
(``query.sandbox``) — give the dtypes and names back.
"""

from __future__ import annotations

#: ``{column: Builder dtype}``.
KV_KEY = "kpubdata_builder.dtypes"
#: Columns written under an internal name because DuckDB cannot write their own (an
#: empty name, or names one letter case apart): ``{internal: real}``.
KV_NAMES_KEY = "kpubdata_builder.names"
#: Set to ``"true"`` on a table without columns. Parquet needs a column, so such a file
#: holds its rows under one placeholder column, :data:`NO_COLUMNS`, which no reader shows.
NO_COLUMNS_KEY = "kpubdata_builder.no_columns"
NO_COLUMNS = "__kpubdata_no_columns"

__all__ = ["KV_KEY", "KV_NAMES_KEY", "NO_COLUMNS", "NO_COLUMNS_KEY"]
