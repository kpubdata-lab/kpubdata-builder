"""Standard assembled artifact model used by export tools.

This file defines the minimal data structure that exporters can commonly
consume after passing through internal stages like Bronze/Silver/Gold.

Key classes:
    - ArtifactDataset: Value object holding records, schema, metadata, provenance
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .spec import JsonValue


@dataclass(frozen=True)
class ArtifactDataset:
    """Assembled dataset representation used before concrete export.

    Keeps record body, schema, provenance, and statistics summary together
    so that various exporters work with the same contract.

    Attributes:
        records: Collection of normalized records to export.
        schema: Optional schema description holding column names and type names.
        metadata: Dataset-level metadata.
        provenance: List of identifiers showing which source combination generated this.
        statistics: Simple aggregate info like record count.

    Example:
        >>> artifact = ArtifactDataset(records=({"id": "1"},))
        >>> len(artifact.records)
        1
    """

    records: tuple[dict[str, JsonValue], ...] = ()
    schema: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()
    statistics: dict[str, int] = field(default_factory=dict)
