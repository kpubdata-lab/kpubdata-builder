"""Parquet exporter implementation."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

import duckdb

from ..artifact import ArtifactDataset
from ..errors import ExportError, TabularError
from ..spec import ExportTarget
from ._rows import copy_atomically, write_records_parquet
from .base import BaseExporter, ExportResult, ensure_output_dir


class ParquetExporter(BaseExporter):
    """exporter that writes records to Parquet.

    Example:
        >>> ParquetExporter().name
        'parquet'
    """

    @property
    def name(self) -> str:
        """returns exporter name."""
        return "parquet"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """exports the rows to a Parquet file.

        A data source with a Parquet file (a Gold table's ``table.parquet``) is copied:
        the rows are not read into Python, and the file keeps the table's dtypes (#873).
        Rows in memory are written by DuckDB, their dtypes inferred.
        """
        destination = ensure_output_dir(output_dir, target.output_path)
        source = artifact.data_source.parquet_path
        try:
            if source is not None:
                copy_atomically(source, destination)
            else:
                fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
                os.close(fd)
                try:
                    write_records_parquet(artifact, Path(tmp_name))
                    os.replace(tmp_name, destination)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_name)
                    raise
        except (OSError, ValueError, TabularError, duckdb.Error) as exc:
            raise ExportError(f"Failed to export Parquet artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
