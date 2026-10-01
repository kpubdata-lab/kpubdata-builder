"""Shared helpers for exporters reading an artifact's data source (#873)."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from ..artifact import ArtifactDataset

#: Rows read per batch from a data source.
BATCH_SIZE = 1000


def resolve_columns(artifact: ArtifactDataset) -> list[str]:
    """Column order: the schema's, then any other key the rows carry, in order of
    first appearance.

    A source that declares its columns (a Gold table: every row has exactly them) is
    not scanned for more; otherwise the rows are read once for keys — the source is
    replayable, so the export reads them again for values.
    """
    columns: dict[str, None] = dict.fromkeys(artifact.schema)
    declared = getattr(artifact.data_source, "columns", None)
    if declared is not None:
        columns.update(dict.fromkeys(declared))
        return list(columns)
    for record in artifact.data_source.iter_records(batch_size=BATCH_SIZE):
        for key in record:
            columns.setdefault(key, None)
    return list(columns)


def write_text_atomically(destination: Path, write: Callable[[TextIO], None]) -> None:
    """Write ``destination`` through a temporary file in its directory, replaced into
    place only once ``write`` has finished — a failed export leaves no partial file.

    Raises:
        OSError: The file could not be written or moved into place.
    """
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            write(handle)
        os.replace(tmp_name, destination)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def copy_atomically(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` through a temporary file, as above."""
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    os.close(fd)
    try:
        shutil.copyfile(source, tmp_name)
        os.replace(tmp_name, destination)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


__all__ = ["BATCH_SIZE", "copy_atomically", "resolve_columns", "write_text_atomically"]


#: A declared schema's type names (the spec's and Builder's) → DuckDB, for a source
#: with a schema but no rows; anything else is text.
_EMPTY_TYPES = {
    "str": "VARCHAR",
    "String": "VARCHAR",
    "Utf8": "VARCHAR",
    "int": "BIGINT",
    "Int64": "BIGINT",
    "Int32": "INTEGER",
    "float": "DOUBLE",
    "Float64": "DOUBLE",
    "Float32": "FLOAT",
    "bool": "BOOLEAN",
    "Boolean": "BOOLEAN",
}


def write_records_parquet(artifact: ArtifactDataset, destination: Path) -> None:
    """The rows of a data source without a Parquet file, written as Parquet by DuckDB.

    Their dtypes are inferred as the loader infers them (``duckdb_load.load_records``).
    A source with no rows is written with its declared schema's columns, typed.
    """
    import duckdb

    from ..tabular.duckdb_load import TableHandle, load_records
    from ..tabular.sql import quote_identifier, quote_literal

    with (
        tempfile.TemporaryDirectory(prefix="kpubdata-export-") as workdir,
        duckdb.connect(":memory:") as connection,
    ):
        connection.execute("SET TimeZone = 'UTC'")
        loaded = load_records(
            connection,
            lambda: artifact.data_source.iter_records(batch_size=BATCH_SIZE),
            table="export_rows",
            workdir=Path(workdir),
        )
        if loaded.row_count == 0 and artifact.schema:
            columns = ", ".join(
                f"CAST(NULL AS {_EMPTY_TYPES.get(dtype, 'VARCHAR')}) AS {quote_identifier(name)}"
                for name, dtype in artifact.schema.items()
            )
            connection.execute(
                f"COPY (SELECT {columns} WHERE false) TO {quote_literal(str(destination))} "
                "(FORMAT PARQUET)"
            )
            return
        TableHandle(connection, loaded, Path(workdir)).write_parquet(destination)
