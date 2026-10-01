"""Standard assembled artifact model used by export tools.

This file defines the minimal data structure that exporters can commonly
consume after passing through internal stages like Bronze/Silver/Gold.

Key classes:
    - ArtifactDataSource: the rows, as a source an exporter can read as often as it needs
    - RecordsSource: a data source over records already in memory
    - ArtifactDataset: value object holding the data source, schema, metadata, provenance

The rows are not a tuple of records any more (#873, a 0.x breaking change): a Gold table
can be larger than memory, and turning it into Python objects before every export
undid DuckDB's out-of-core execution. An exporter reads ``data_source.iter_records()``,
which starts over on every call and holds a batch at a time, and may copy
``data_source.parquet_path`` when it writes Parquet.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .spec import JsonValue


@runtime_checkable
class ArtifactDataSource(Protocol):
    """The rows of an artifact, readable again and again (#873).

    Not a one-shot iterator: every :meth:`iter_records` call starts from the first row,
    so an exporter may read the rows twice (columns first, then values) and two
    exporters may read the same source. Nothing requires holding every row at once.
    """

    @property
    def row_count(self) -> int:
        """How many rows :meth:`iter_records` yields."""
        ...

    @property
    def parquet_path(self) -> Path | None:
        """A Parquet file holding exactly these rows and their dtypes, when one exists —
        an exporter writing Parquet may copy it instead of reading rows."""
        ...

    def iter_records(self, *, batch_size: int = 1000) -> Iterator[dict[str, JsonValue]]:
        """Every row, in order, from the first; ``batch_size`` bounds what is read at once."""
        ...


@dataclass(frozen=True)
class RecordsSource:
    """A data source over records already in memory — for small artifacts, tests and
    plugins that build their own. Replayable like any other source."""

    records: Sequence[dict[str, JsonValue]] = ()

    @property
    def row_count(self) -> int:
        return len(self.records)

    @property
    def parquet_path(self) -> Path | None:
        return None

    def iter_records(self, *, batch_size: int = 1000) -> Iterator[dict[str, JsonValue]]:
        del batch_size
        return iter(self.records)


@dataclass(frozen=True)
class ArtifactDataset:
    """Assembled dataset representation used before concrete export.

    Keeps the data source, schema, provenance, and statistics summary together
    so that various exporters work with the same contract.

    Attributes:
        data_source: The rows (:class:`ArtifactDataSource`).
        schema: Optional schema description holding column names and type names.
        metadata: Dataset-level metadata.
        provenance: List of identifiers showing which source combination generated this.
        statistics: Simple aggregate info like record count.

    Example:
        >>> artifact = ArtifactDataset.from_records(({"id": "1"},))
        >>> artifact.data_source.row_count
        1
    """

    data_source: ArtifactDataSource = field(default_factory=RecordsSource)
    schema: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()
    statistics: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_records(
        cls,
        records: Sequence[dict[str, JsonValue]] = (),
        *,
        schema: dict[str, str] | None = None,
        metadata: dict[str, str] | None = None,
        provenance: tuple[str, ...] = (),
        statistics: dict[str, int] | None = None,
    ) -> ArtifactDataset:
        """An artifact over records in memory (:class:`RecordsSource`)."""
        return cls(
            data_source=RecordsSource(tuple(records)),
            schema=dict(schema or {}),
            metadata=dict(metadata or {}),
            provenance=provenance,
            statistics=dict(statistics or {}),
        )


__all__ = ["ArtifactDataSource", "ArtifactDataset", "RecordsSource"]
