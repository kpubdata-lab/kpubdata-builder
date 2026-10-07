"""persists Silver stage artifacts to the execution workspace (#46).

Persist SilverDataset under output_root/{run_id}/silver/{source_key}/. Table saved as
parquet; schema/statistics/preview/validation info recorded as deterministic JSON.

Main components:
    - SilverPersistResult: persist path result object
    - persist_silver_dataset: Silver output file recording function
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from ...tabular.wire import encode_rows
from .._path_safety import ensure_within, validate_path_segment
from .models import SilverDataset


@dataclass(frozen=True)
class SilverPersistResult:
    """filesystem path recorded for Silver artifacts.

    Attributes:
        silver_dir: Silver directory where artifacts were saved.
        table_path: refined table parquet file path.
        schema_path: schema summary JSON path.
        stats_path: statistics summary JSON path.
        preview_path: preview JSON path.
        validation_path: validation result JSON path.
    """

    silver_dir: Path
    table_path: Path
    schema_path: Path
    stats_path: Path
    preview_path: Path
    validation_path: Path


def _json_default(value: object) -> str:
    """default JSON serializer. converts date/datetime to ISO strings."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _write_json(path: Path, payload: object) -> None:
    """writes payload as deterministic JSON (date/datetime as ISO strings)."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
        + "\n",
        encoding="utf-8",
    )


def _write_table(dataset: SilverDataset, path: Path) -> None:
    """The table as Parquet, written by DuckDB with the Builder dtypes in the file's
    metadata (a table without columns keeps a placeholder, ``builder_kv``)."""
    dataset.table.write_parquet(path)


def persist_silver_dataset(
    dataset: SilverDataset,
    *,
    output_root: Path,
    run_id: str,
) -> SilverPersistResult:
    """records Silver artifacts under output_root/{run_id}/silver/{source_key}/."""
    validate_path_segment(run_id, field_name="run_id")

    source_key_segment = dataset.source_bronze.replace("/", "_")
    validate_path_segment(source_key_segment, field_name="source_key")

    silver_dir = output_root / run_id / "silver" / source_key_segment
    ensure_within(output_root, silver_dir, label="silver directory")

    import shutil
    import tempfile

    from .._atomic import atomic_replace_dir

    silver_dir.parent.mkdir(parents=True, exist_ok=True)

    table_path = silver_dir / "table.parquet"
    schema_path = silver_dir / "schema.json"
    stats_path = silver_dir / "stats.json"
    preview_path = silver_dir / "preview.json"
    validation_path = silver_dir / "validation.json"

    # Atomic write: write to temp dir then rename
    tmp_dir = Path(tempfile.mkdtemp(dir=silver_dir.parent, prefix=".silver_tmp_"))
    try:
        _write_table(dataset, tmp_dir / "table.parquet")
        _write_json(tmp_dir / "schema.json", asdict(dataset.schema))
        _write_json(tmp_dir / "stats.json", asdict(dataset.statistics))
        # The sample is served as-is by stage detail, so it is written wire-encoded (#735):
        # a Decimal or an out-of-range integer is stored as its exact decimal text.
        preview = asdict(dataset.preview)
        preview["rows"] = list(encode_rows(dataset.preview.rows, dataset.schema.columns))
        _write_json(tmp_dir / "preview.json", preview)
        _write_json(tmp_dir / "validation.json", asdict(dataset.validation))

        # Atomic swap: replaces existing directory without data loss (#180).
        atomic_replace_dir(tmp_dir, silver_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return SilverPersistResult(
        silver_dir=silver_dir,
        table_path=table_path,
        schema_path=schema_path,
        stats_path=stats_path,
        preview_path=preview_path,
        validation_path=validation_path,
    )
