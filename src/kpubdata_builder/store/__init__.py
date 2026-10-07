"""Persistent Build store (#309, ADR 0003; backend split ADR 0010/0016).

Provide build index to reduce filesystem scan cost, improve scalability/consistency/concurrency.
``BuildIndex`` is Protocol; default implementation is ``SqliteBuildIndex`` (no external deps).
``make_build_index()`` factory switches based on ``KPUBDATA_BUILDER_STORAGE_BACKEND``
to sqlite/cubrid
implementation. ``CubridBuildIndex`` is lazy imported only on cubrid selection.
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
