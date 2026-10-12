"""Persistent Build store (#309, ADR 0003; abstraction ADR 0010, SQLite only ADR 0022).

Provide build index to reduce filesystem scan cost, improve scalability/consistency/concurrency.
``BuildIndex`` is Protocol; its implementation is ``SqliteBuildIndex`` (no external deps).
"""

from __future__ import annotations

from .build_index import (
    SCHEMA_VERSION,
    BuildEntry,
    BuildIndex,
    SqliteBuildIndex,
    bring_index_up_to_date,
    make_build_index,
    rebuild_index,
)

__all__ = [
    "BuildEntry",
    "BuildIndex",
    "SCHEMA_VERSION",
    "SqliteBuildIndex",
    "bring_index_up_to_date",
    "make_build_index",
    "rebuild_index",
]
