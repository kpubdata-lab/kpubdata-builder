"""persists Gold stage artifacts to the execution workspace (#47)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from ...spec import JsonValue
from ...tabular.polars_bridge import write_table_parquet
from .._path_safety import ensure_within, validate_path_segment
from .models import GoldPackage


@dataclass(frozen=True)
class GoldPersistResult:
    """filesystem path recorded for Gold artifacts."""

    gold_dir: Path
    table_path: Path
    package_path: Path
    splits_paths: dict[str, Path] = field(default_factory=dict)


def _package_metadata(package: GoldPackage) -> dict[str, JsonValue]:
    """constructs JSON-serializable metadata for GoldPackage."""
    return {
        "dataset_name": package.dataset_name,
        "source_silver": package.source_silver,
        "source_refs": (
            cast(list[JsonValue], list(package.source_refs))
            if package.source_refs is not None
            else None
        ),
        "row_count": package.table.height,
        "columns": cast(list[JsonValue], list(package.table.columns)),
        "metadata": dict(package.metadata),
        "export_plan": {
            "targets": [
                {
                    "kind": target.kind,
                    "output_path": target.output_path,
                    "options": target.options,
                }
                for target in package.export_plan.targets
            ],
        },
        "splits": (
            {name: frame.height for name, frame in package.splits.items()}
            if package.splits is not None
            else None
        ),
    }


def persist_gold_package(
    package: GoldPackage,
    *,
    output_root: Path,
    run_id: str,
) -> GoldPersistResult:
    """records Gold artifacts under output_root/{run_id}/gold/{dataset_name}/."""
    validate_path_segment(run_id, field_name="run_id")
    validate_path_segment(package.dataset_name, field_name="dataset_name")

    gold_dir = output_root / run_id / "gold" / package.dataset_name
    ensure_within(output_root, gold_dir, label="gold directory")

    import shutil
    import tempfile

    from .._atomic import atomic_replace_dir

    gold_dir.parent.mkdir(parents=True, exist_ok=True)

    table_path = gold_dir / "table.parquet"
    package_path = gold_dir / "package.json"

    # Atomic write: write to temp dir then rename
    tmp_dir = Path(tempfile.mkdtemp(dir=gold_dir.parent, prefix=".gold_tmp_"))
    try:
        # DuckDB COPY, with the Builder dtypes in the file's metadata (#870).
        write_table_parquet(package.table, tmp_dir / "table.parquet")
        # allow_nan=False: NaN/Infinity are non-standard JSON tokens, so fail with
        # ValueError (#217).
        (tmp_dir / "package.json").write_text(
            json.dumps(
                _package_metadata(package),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )

        # splits handling: if package.splits exists, write each split as parquet in splits/
        if package.splits is not None:
            (tmp_dir / "splits").mkdir(exist_ok=True)
            for split_name, split_df in package.splits.items():
                validate_path_segment(split_name, field_name="split_name")
                split_df.write_parquet(tmp_dir / "splits" / f"{split_name}.parquet")

        # Atomic swap: replaces existing directory without data loss (#180).
        atomic_replace_dir(tmp_dir, gold_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # construct splits_paths result
    splits_paths: dict[str, Path] = {}
    if package.splits is not None:
        for split_name in package.splits:
            splits_paths[split_name] = gold_dir / "splits" / f"{split_name}.parquet"

    return GoldPersistResult(
        gold_dir=gold_dir,
        table_path=table_path,
        package_path=package_path,
        splits_paths=splits_paths,
    )
