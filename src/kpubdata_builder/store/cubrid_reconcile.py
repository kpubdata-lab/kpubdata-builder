"""Compare CUBRID's manifest rows with the files on disk, without writing (ADR 0022).

ADR 0016 made the CUBRID ``manifests`` row the canonical manifest and the file the
mirror: a write commits the row first and the file second, so a failed file write
leaves the newest manifest in the database alone. ADR 0022 returns Builder to files
only. Before a deployment leaves CUBRID, this report says which runs would lose their
manifest — the reconciliation step's dry run. It reads; it never writes either side.

Run it with the release that still has the CUBRID backend::

    KPUBDATA_BUILDER_CUBRID_URL=cubrid+pycubrid://... \\
        python -m kpubdata_builder.store.cubrid_reconcile --output-dir /data

Exit status: 0 when every row has a matching file, 1 when some run needs restoring,
2 when the database cannot be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..stages._path_safety import ensure_within

if TYPE_CHECKING:
    from sqlalchemy import Engine

_MANIFEST_FILENAME = "manifest.json"
_MANIFEST_TABLE = "manifests"
_CREDENTIAL_TABLE = "provider_credentials"


@dataclass(frozen=True)
class ManifestDiff:
    """What the database holds that the files do not, by run id."""

    #: Rows whose run has no ``manifest.json``: the manifest exists only in CUBRID.
    missing_on_disk: list[str] = field(default_factory=list)
    #: Rows whose file is there but cannot be read as a JSON object.
    unreadable_on_disk: list[str] = field(default_factory=list)
    #: Rows and files that both parse but hold different documents.
    differs: list[str] = field(default_factory=list)
    #: Rows that are not a JSON object. Builder already falls back to the file for them.
    unreadable_in_db: list[str] = field(default_factory=list)
    #: Row ids that would resolve outside the output directory. Never read.
    unsafe_run_ids: list[str] = field(default_factory=list)
    #: Rows whose file holds the same document.
    matching: int = 0
    #: Runs with a file and no row: never promoted, already file-only.
    disk_only: int = 0
    #: ``owner_id/provider`` pairs with a key saved in CUBRID. No secret is read.
    credentials: list[str] = field(default_factory=list)

    @property
    def needs_restore(self) -> bool:
        """Whether some run would lose its manifest if the database were dropped now."""
        return bool(self.missing_on_disk or self.unreadable_on_disk or self.differs)

    def as_dict(self) -> dict[str, object]:
        return {
            "missing_on_disk": self.missing_on_disk,
            "unreadable_on_disk": self.unreadable_on_disk,
            "differs": self.differs,
            "unreadable_in_db": self.unreadable_in_db,
            "unsafe_run_ids": self.unsafe_run_ids,
            "matching": self.matching,
            "disk_only": self.disk_only,
            "credentials": self.credentials,
            "needs_restore": self.needs_restore,
        }


def _read_object(text: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _disk_run_ids(output_root: Path) -> set[str]:
    if not output_root.is_dir():
        return set()
    return {
        entry.name
        for entry in output_root.iterdir()
        if entry.is_dir() and (entry / _MANIFEST_FILENAME).is_file()
    }


def compare_manifests(
    rows: Sequence[tuple[str, str]],
    output_root: Path,
    credentials: Sequence[tuple[str, str]] = (),
) -> ManifestDiff:
    """Compare ``(run_id, manifest_json)`` rows with ``output_root/<run_id>/manifest.json``.

    Documents are compared after parsing, because the row and the file are serialised
    differently (the file is indented).
    """
    missing: list[str] = []
    unreadable_on_disk: list[str] = []
    differs: list[str] = []
    unreadable_in_db: list[str] = []
    unsafe: list[str] = []
    matching = 0
    for run_id, text in sorted(rows):
        manifest_path = output_root / run_id / _MANIFEST_FILENAME
        try:
            ensure_within(output_root, manifest_path, label="manifest file")
        except ValueError:
            unsafe.append(run_id)
            continue
        stored = _read_object(text)
        if stored is None:
            unreadable_in_db.append(run_id)
            continue
        if not manifest_path.is_file():
            missing.append(run_id)
            continue
        try:
            on_disk = _read_object(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            on_disk = None
        if on_disk is None:
            unreadable_on_disk.append(run_id)
        elif on_disk != stored:
            differs.append(run_id)
        else:
            matching += 1
    return ManifestDiff(
        missing_on_disk=missing,
        unreadable_on_disk=unreadable_on_disk,
        differs=differs,
        unreadable_in_db=unreadable_in_db,
        unsafe_run_ids=unsafe,
        matching=matching,
        disk_only=len(_disk_run_ids(output_root) - {run_id for run_id, _ in rows}),
        credentials=sorted(f"{owner}/{provider}" for owner, provider in credentials),
    )


def reconcile(engine: Engine, output_root: Path) -> ManifestDiff:
    """Read CUBRID's manifest and credential rows and compare them with the files.

    Read-only: a missing table is an empty one, and nothing is created.
    """
    from sqlalchemy import column, inspect, select, table

    # Lightweight tables naming only the columns read: nothing is reflected or created.
    manifests = table(_MANIFEST_TABLE, column("run_id"), column("manifest"))
    saved_keys = table(_CREDENTIAL_TABLE, column("owner_id"), column("provider"))
    inspector = inspect(engine)
    rows: list[tuple[str, str]] = []
    credentials: list[tuple[str, str]] = []
    with engine.connect() as conn:
        if inspector.has_table(_MANIFEST_TABLE):
            for run_id, text in conn.execute(select(manifests.c.run_id, manifests.c.manifest)):
                rows.append((str(run_id), str(text)))
        if inspector.has_table(_CREDENTIAL_TABLE):
            for owner_id, provider in conn.execute(
                select(saved_keys.c.owner_id, saved_keys.c.provider)
            ):
                credentials.append((str(owner_id), str(provider)))
    return compare_manifests(rows, output_root, credentials)


def _print_report(diff: ManifestDiff) -> None:
    sections = (
        ("only in CUBRID (no manifest.json)", diff.missing_on_disk),
        ("manifest.json unreadable", diff.unreadable_on_disk),
        ("CUBRID and manifest.json differ", diff.differs),
        ("CUBRID row unreadable (file is used already)", diff.unreadable_in_db),
        ("run id outside the output directory (not read)", diff.unsafe_run_ids),
        ("provider keys saved in CUBRID (owner/provider)", diff.credentials),
    )
    for title, run_ids in sections:
        print(f"{title}: {len(run_ids)}")
        for run_id in run_ids:
            print(f"  {run_id}")
    print(f"matching: {diff.matching}")
    print(f"file only (never promoted): {diff.disk_only}")
    print("needs restore: " + ("yes" if diff.needs_restore else "no"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kpubdata_builder.store.cubrid_reconcile",
        description=(
            "Compare CUBRID's manifest rows with the manifest files (dry run; writes nothing)."
        ),
    )
    parser.add_argument("--output-dir", required=True, help="Builder's output directory")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    from .backend import dispose_engine, get_engine

    try:
        diff = reconcile(get_engine(), Path(args.output_dir))
    except Exception as exc:
        print(f"cannot read CUBRID: {exc}", file=sys.stderr)
        return 2
    finally:
        dispose_engine()
    if args.json:
        print(json.dumps(diff.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(diff)
    return 1 if diff.needs_restore else 0


if __name__ == "__main__":
    sys.exit(main())
