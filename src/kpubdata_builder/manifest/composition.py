"""Composition (join) provenance model for build manifest (#506).

Holds provenance tracking info for results of combining two sources via
CompositionSpec/JoinSpec. Duplicate key explosion risk uses original row count/distinct
key count as-is — so auditor can recompute/verify themselves
(same as this repo's existing provenance principle: don't hide evidence with arbitrary summaries).

Key components:
    - CompositionProvenance: Provenance snapshot of composition result
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompositionProvenance:
    """Detailed provenance info for composition (join) execution result.

    Attributes:
        name: Combined Gold dataset name (CompositionSpec.name).
        left: Left source output key (alias).
        right: Right source output key (alias).
        join_type: "inner" | "left".
        left_key: Left join key column name.
        right_key: Right join key column name.
        left_row_count: Left Silver table row count.
        left_distinct_key_count: Left join key column distinct value count (null excluded).
        right_row_count: Right Silver table row count.
        right_distinct_key_count: Right join key column distinct value count (null excluded).
        output_row_count: Join result row count.
        duplicate_key_warning: Both join keys have duplicate values (many-to-many), row
            explosion risk detected. If detected, actual handling (warn-only or fail
            build) decided by JoinSpec.on_duplicate_key — this field only records
            whether detected.
    """

    name: str
    left: str
    right: str
    join_type: str
    left_key: str
    right_key: str
    left_row_count: int
    left_distinct_key_count: int
    right_row_count: int
    right_distinct_key_count: int
    output_row_count: int
    duplicate_key_warning: bool


__all__ = ["CompositionProvenance"]
