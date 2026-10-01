"""Parquet exporter implementation."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

import polars as pl

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ..tabular.convert import records_to_dataframe
from ._rows import copy_atomically
from .base import BaseExporter, ExportResult, ensure_output_dir


def _build_frame(artifact: ArtifactDataset) -> pl.DataFrame:
    """Rows in memory as a frame, for a data source without a Parquet file."""
    records = list(artifact.data_source.iter_records())
    if records:
        return records_to_dataframe(records)
    if artifact.schema:
        _TYPE_MAP: dict[str, type[pl.DataType]] = {
            "str": pl.Utf8,
            "String": pl.Utf8,
            "Utf8": pl.Utf8,
            "int": pl.Int64,
            "Int64": pl.Int64,
            "Int32": pl.Int32,
            "float": pl.Float64,
            "Float64": pl.Float64,
            "Float32": pl.Float32,
            "bool": pl.Boolean,
            "Boolean": pl.Boolean,
        }
        schema = {name: _TYPE_MAP.get(dtype, pl.Utf8) for name, dtype in artifact.schema.items()}
        return pl.DataFrame(schema=schema)
    return pl.DataFrame()


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
        Rows in memory are written through a frame, their dtypes inferred.
        """
        destination = ensure_output_dir(output_dir, target.output_path)
        source = artifact.data_source.parquet_path
        try:
            if source is not None:
                copy_atomically(source, destination)
            else:
                frame = _build_frame(artifact)
                fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
                os.close(fd)
                try:
                    frame.write_parquet(Path(tmp_name))
                    os.replace(tmp_name, destination)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_name)
                    raise
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise ExportError(f"Failed to export Parquet artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
