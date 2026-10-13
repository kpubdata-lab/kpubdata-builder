"""persists Gold stage artifacts to the execution workspace (#47)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from ...spec import JsonValue
from .._path_safety import ensure_within, path_collision_key, validate_path_segment
from .models import GoldPackage

#: Longest split label kept as its own file name; longer ones get a generated name.
_MAX_KEPT_LABEL = 100
#: Windows device names: ``CON.parquet`` opens the console there, not a file.
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"{port}{n}" for port in ("com", "lpt") for n in range(10)}
)
#: A generated split file's stem; a label of this shape is never kept as it is.
_GENERATED_STEM = re.compile(r"key-[0-9a-f]{32}", re.IGNORECASE)


@dataclass(frozen=True)
class GoldPersistResult:
    """filesystem path recorded for Gold artifacts."""

    gold_dir: Path
    table_path: Path
    package_path: Path
    splits_paths: dict[str, Path] = field(default_factory=dict)


def _keeps_label(label: str) -> bool:
    """Whether ``label`` can be its own file name on every filesystem Builder runs on."""
    try:
        validate_path_segment(label, field_name="split_name")
    except ValueError:
        return False
    return (
        len(label) <= _MAX_KEPT_LABEL
        and label.split(".")[0].casefold() not in _WINDOWS_DEVICES
        and not _GENERATED_STEM.fullmatch(label)
    )


def split_file_name(label: str, labels: Collection[str]) -> str:
    """The file name, below ``splits/``, that the split ``label`` is written to (#1321).

    A key split's labels are its key values as text — Korean, negative numbers,
    datetimes, ``__null__``, ``..`` or ``/`` are all ordinary. A label that is a safe
    path segment, not a Windows device name, not too long, and not the same file as
    another of ``labels`` on a case-insensitive filesystem keeps its name
    (``train.parquet``, ``2024.parquet``), as before. Any other label is written to
    ``key-<digest>.parquet``, the digest taken over the label alone, so the name does
    not depend on the order or the other labels. A label shaped like a generated name
    is never kept, so a kept name never meets a generated one, and both pass the
    artifact download's segment check. ``package.json``'s ``split_files`` maps each
    label back to its file.
    """
    if _keeps_label(label):
        folded = path_collision_key(label)
        if not any(
            other != label and _keeps_label(other) and path_collision_key(other) == folded
            for other in labels
        ):
            return f"{label}.parquet"
    digest = hashlib.sha256(label.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return f"key-{digest}.parquet"


def _split_files(package: GoldPackage) -> dict[str, str]:
    """Each split label's file below ``splits/``."""
    labels = list(package.splits or {})
    return {label: split_file_name(label, labels) for label in labels}


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
        # Label -> file relative to the Gold directory (#1321). A package without it
        # predates #1321 and wrote each split to ``splits/<label>.parquet``.
        "split_files": (
            {label: f"splits/{name}" for label, name in _split_files(package).items()}
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

    split_files = _split_files(package)

    # Atomic write: write to temp dir then rename
    tmp_dir = Path(tempfile.mkdtemp(dir=gold_dir.parent, prefix=".gold_tmp_"))
    try:
        # DuckDB COPY, with the Builder dtypes in the file's metadata (#870).
        package.table.write_parquet(tmp_dir / "table.parquet")
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
                split_df.write_parquet(tmp_dir / "splits" / split_files[split_name])

        # Atomic swap: replaces existing directory without data loss (#180).
        atomic_replace_dir(tmp_dir, gold_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # construct splits_paths result
    splits_paths: dict[str, Path] = {}
    if package.splits is not None:
        for split_name in package.splits:
            splits_paths[split_name] = gold_dir / "splits" / split_files[split_name]

    return GoldPersistResult(
        gold_dir=gold_dir,
        table_path=table_path,
        package_path=package_path,
        splits_paths=splits_paths,
    )
