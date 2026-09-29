"""Composition (join) provenance model for build manifest (#506).

Holds provenance tracking info for results of combining two sources via
CompositionSpec/JoinSpec. Duplicate key explosion risk uses original row count/distinct
key count as-is — so auditor can recompute/verify themselves
(same as this repo's existing provenance principle: don't hide evidence with arbitrary summaries).

Key components:
    - JoinKeyProvenance: One join key column pair (#698)
    - CompositionProvenance: Provenance snapshot of composition result
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JoinKeyProvenance:
    """One join key column pair (#698).

    Attributes:
        left: Key column name in the left table.
        right: Key column name in the right table.
    """

    left: str
    right: str


@dataclass(frozen=True)
class CompositionProvenance:
    """Detailed provenance info for composition (join) execution result.

    Attributes:
        name: Combined Gold dataset name (CompositionSpec.name).
        left: Left source output key (alias).
        right: Right source output key (alias).
        join_type: "inner" | "left".
        left_key: Left join key column name (first pair of ``keys``).
        right_key: Right join key column name (first pair of ``keys``).
        left_row_count: Left Silver table row count.
        left_distinct_key_count: Distinct left key values (tuples for a composite key),
            null excluded.
        right_row_count: Right Silver table row count.
        right_distinct_key_count: Distinct right key values, null excluded.
        output_row_count: Join result row count.
        duplicate_key_warning: Some key present on both sides repeats on both sides,
            so its rows multiply many-to-many (#698: judged on intersecting keys
            only). If detected, actual handling (warn-only or fail build) decided by
            JoinSpec.on_duplicate_key — this field only records whether detected.
        keys: Every join key column pair (#698). Empty only when constructed by a
            caller that predates #698.
        cardinality: Declared JoinSpec.cardinality, or None when not declared.
        observed_cardinality: Cardinality observed over the intersecting keys.
        left_unmatched_ratio: Left rows whose key has no match on the right (null-key
            rows included) over left rows; 0.0 for an empty left side.
        right_unmatched_ratio: Same for the right side.
        expansion_ratio: Output rows over left rows; None when the left is empty.
        left_null_key_rows: Left rows with a null in any key column.
        right_null_key_rows: Right rows with a null in any key column.

    The #698 fields default to None/empty so the model stays additive.
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
    keys: tuple[JoinKeyProvenance, ...] = ()
    cardinality: str | None = None
    observed_cardinality: str | None = None
    left_unmatched_ratio: float | None = None
    right_unmatched_ratio: float | None = None
    expansion_ratio: float | None = None
    left_null_key_rows: int | None = None
    right_null_key_rows: int | None = None


__all__ = ["CompositionProvenance", "JoinKeyProvenance"]
