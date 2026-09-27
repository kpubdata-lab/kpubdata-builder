"""Schema summary model for build manifest (#11).

This module holds immutable value objects for column-level schema info in manifest and
builders that create summary from raw (name, type, nullable) sequence.
Input as primitive tuples to avoid depending on tabular engine.

Key components:
    - FieldSummary: Single column summary (name/type/nullable)
    - SchemaSummary: Summary bundle preserving column order
    - build_schema_summary: (name, type, nullable) sequence → SchemaSummary
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSummary:
    """Schema summary for single column.

    Attributes:
        name: Column name.
        type: String representation of column type.
        nullable: Whether null values can exist in column.
    """

    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class SchemaSummary:
    """Dataset schema summary.

    Attributes:
        fields: FieldSummary tuple preserving column order.
        total_fields: Column count (same as fields length).
    """

    fields: tuple[FieldSummary, ...] = ()
    total_fields: int = 0


def build_schema_summary(fields: Iterable[tuple[str, str, bool]]) -> SchemaSummary:
    """Create SchemaSummary from (name, type, nullable) sequence.

    Args:
        fields: Tuple sequence of (name, type, nullable) preserving column order.

    Returns:
        SchemaSummary: Summary where total_fields matches fields length.
    """
    summaries = tuple(
        FieldSummary(name=name, type=type_name, nullable=nullable)
        for name, type_name, nullable in fields
    )
    return SchemaSummary(fields=summaries, total_fields=len(summaries))


__all__ = ["FieldSummary", "SchemaSummary", "build_schema_summary"]
