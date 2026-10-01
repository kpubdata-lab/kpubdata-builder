"""The Parquet key-value metadata keys Builder writes (#869), without importing Polars.

``TableHandle.write_parquet`` records each column's Builder dtype under :data:`KV_KEY`,
and the internal name of any column DuckDB cannot write under its own under
:data:`KV_NAMES_KEY`. Readers — Polars (``builder_parquet``) and the DuckDB query
sandbox (``query.sandbox``) — give the dtypes and names back.
"""

from __future__ import annotations

#: ``{column: Builder dtype}``.
KV_KEY = "kpubdata_builder.dtypes"
#: Columns written under an internal name because DuckDB cannot write their own (an
#: empty name, or names one letter case apart): ``{internal: real}``.
KV_NAMES_KEY = "kpubdata_builder.names"

__all__ = ["KV_KEY", "KV_NAMES_KEY"]
