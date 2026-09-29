"""Back up a warehouse and restore it only when the pair is whole (#705).

A catalog without its snapshot files, or snapshot files without their catalog, is not
a warehouse. It is a table whose pointer names nothing. So a backup carries both, and
a restore checks them against each other before anything becomes readable:

- every table a snapshot names exists;
- every table's current pointer names a committed snapshot;
- every committed or quarantined snapshot has its directory, the directory is not
  empty, and its bytes still hash to the digest the catalog recorded.

Any failure refuses the whole restore and names every problem it found. A partial
restore would produce the one outcome worse than no restore: a table that answers
queries with nothing, or with bytes that are not the ones that were committed.

What a backup holds::

    <backup>/
      backup.json                       format, time, each snapshot and its digest
      _warehouse.sqlite                 the catalog, copied through SQLite
      tables/<table>/snapshots/<id>/    committed and quarantined snapshots

What it leaves out, on purpose:

- **leases** — they describe queries running in the process that took the backup;
- **staging, validated, abandoned and retiring snapshots** — nothing can read them,
  and a ``retiring`` one is already being deleted. Backing up what is on its way out
  would restore it.

Holds are kept: a retention period or an audit is exactly what a backup is for.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import CATALOG_FILENAME, TableCatalog, _now
from .errors import BackupInvalid, SnapshotNotFound, SnapshotStateError
from .layout import SnapshotLayout, content_digest, is_empty, thaw

BACKUP_MANIFEST = "backup.json"
BACKUP_FORMAT = 1

# The states a reader can reach. Everything else stays behind (see module docstring).
_READABLE = ("committed", "quarantined")


@dataclass(frozen=True)
class BackupReport:
    """What a backup captured.

    Attributes:
        path: The backup directory.
        tables: Tables in the catalog copy.
        snapshots: Snapshot ids whose files were copied.
        left_out: Snapshot ids dropped from the copy because nothing can read them.
    """

    path: Path
    tables: int
    snapshots: tuple[str, ...]
    left_out: tuple[str, ...] = field(default=())


def _sibling(path: Path, label: str) -> Path:
    """A fresh directory beside ``path``, so the final step is one rename."""
    return path.parent / f".{path.name}.{label}-{uuid.uuid4().hex[:8]}"


def _discard(path: Path) -> None:
    """Remove a partial directory, including read-only snapshot copies."""
    if path.exists():
        thaw(path)
        shutil.rmtree(path, ignore_errors=True)


def _refuse_existing(path: Path, what: str) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise BackupInvalid(
            [f"{what} {path} already exists and is not empty; nothing is overwritten"]
        )


@contextmanager
def _pinned(catalog: TableCatalog, snapshot_ids: list[str]) -> Iterator[None]:
    """Lease every snapshot being copied, so collection cannot delete one mid-copy."""
    leases: list[str] = []
    try:
        for snapshot_id in snapshot_ids:
            try:
                leases.append(catalog.pin(snapshot_id).lease_id)
            except (SnapshotNotFound, SnapshotStateError) as exc:
                raise BackupInvalid(
                    [f"snapshot {snapshot_id} disappeared while the backup was taken: {exc}"]
                ) from exc
        yield
    finally:
        for lease_id in leases:
            catalog.release(lease_id)


def backup(catalog: TableCatalog, destination: Path) -> BackupReport:
    """Copy the catalog and every readable snapshot into ``destination``.

    The catalog is copied with SQLite's online backup, which is consistent even while
    other connections write. Snapshots are then leased while their files are copied,
    and every copy is checked against its recorded digest — a backup of damaged bytes
    is caught here rather than on the day it is needed.

    Everything is written beside ``destination`` and renamed into place at the end,
    so a failed backup leaves nothing that looks like a finished one.

    Raises:
        BackupInvalid: ``destination`` is not empty, a snapshot vanished while being
            copied, or a copied snapshot does not match its digest.
    """
    _refuse_existing(destination, "backup destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = _sibling(destination, "partial")
    work.mkdir()
    try:
        copy_path = work / CATALOG_FILENAME
        with (
            closing(sqlite3.connect(str(catalog.path))) as source,
            closing(sqlite3.connect(str(copy_path))) as target,
        ):
            source.backup(target)
            # The live catalog runs in WAL mode and the page copy carries that over. A
            # backup is read far from the process that wrote it, often read-only, and a
            # WAL database cannot be opened read-only without its shared-memory file.
            target.execute("PRAGMA journal_mode=DELETE")
            placeholders = ",".join("?" for _ in _READABLE)
            left_out = [
                row[0]
                for row in target.execute(
                    f"SELECT id FROM table_snapshots WHERE state NOT IN ({placeholders})",
                    _READABLE,
                )
            ]
            target.execute("DELETE FROM snapshot_leases")
            for snapshot_id in left_out:
                target.execute("DELETE FROM snapshot_holds WHERE snapshot_id = ?", (snapshot_id,))
                target.execute("DELETE FROM table_snapshots WHERE id = ?", (snapshot_id,))
            target.commit()
            snapshots = [
                (row[0], row[1], row[2])
                for row in target.execute(
                    "SELECT id, table_id, artifact_digest FROM table_snapshots ORDER BY id"
                )
            ]
            tables = int(target.execute("SELECT COUNT(*) FROM tables").fetchone()[0])

        problems: list[str] = []
        with _pinned(catalog, [snapshot_id for snapshot_id, _, _ in snapshots]):
            for snapshot_id, table_id, digest in snapshots:
                source_dir = SnapshotLayout(catalog.root, table_id).snapshot_dir(snapshot_id)
                target_dir = SnapshotLayout(work, table_id).snapshot_dir(snapshot_id)
                if not source_dir.is_dir():
                    problems.append(f"snapshot {snapshot_id} has no directory at {source_dir}")
                    continue
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_dir, target_dir)
                if content_digest(target_dir) != digest:
                    problems.append(
                        f"snapshot {snapshot_id} does not match its recorded digest; "
                        "the live copy is damaged and was not backed up as if it were whole"
                    )
        if problems:
            raise BackupInvalid(problems)

        (work / BACKUP_MANIFEST).write_text(
            json.dumps(
                {
                    "format": BACKUP_FORMAT,
                    "created_at": _now(),
                    "snapshots": [
                        {"table_id": table_id, "snapshot_id": snapshot_id, "digest": digest}
                        for snapshot_id, table_id, digest in snapshots
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            destination.rmdir()
        work.rename(destination)
    except BaseException:
        _discard(work)
        raise
    return BackupReport(
        path=destination,
        tables=tables,
        snapshots=tuple(snapshot_id for snapshot_id, _, _ in snapshots),
        left_out=tuple(left_out),
    )


def verify_backup(source: Path) -> list[str]:
    """Every reason ``source`` would not restore to a whole warehouse; empty if none.

    Reads the catalog copy directly rather than opening it as a ``TableCatalog``,
    which would migrate it in place — verification must not change what it checks.
    """
    problems: list[str] = []
    manifest_path = source / BACKUP_MANIFEST
    catalog_path = source / CATALOG_FILENAME
    if not manifest_path.is_file():
        return [f"{source} has no {BACKUP_MANIFEST}; it is not a warehouse backup"]
    if not catalog_path.is_file():
        return [f"{source} has no {CATALOG_FILENAME}; files without a catalog are not a backup"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{BACKUP_MANIFEST} cannot be read: {exc}"]
    if manifest.get("format") != BACKUP_FORMAT:
        return [f"backup format {manifest.get('format')!r} is not {BACKUP_FORMAT}"]

    with closing(sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)) as conn:
        tables = {
            row[0]: row[1] for row in conn.execute("SELECT id, current_snapshot_id FROM tables")
        }
        snapshots = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT id, table_id, state, artifact_digest FROM table_snapshots"
            )
        }

    for snapshot_id, (table_id, _, _) in sorted(snapshots.items()):
        if table_id not in tables:
            problems.append(f"snapshot {snapshot_id} names table {table_id}, which does not exist")
    for table_id, current in sorted(tables.items()):
        if current is None:
            continue
        if current not in snapshots:
            problems.append(f"table {table_id} points at {current}, which the backup does not hold")
        elif snapshots[current][1] != "committed":
            problems.append(
                f"table {table_id} points at {current}, which is {snapshots[current][1]}, "
                "not committed"
            )
    recorded = {entry["snapshot_id"]: entry["digest"] for entry in manifest.get("snapshots", [])}
    for snapshot_id, (table_id, state, digest) in sorted(snapshots.items()):
        if state not in _READABLE:
            problems.append(f"snapshot {snapshot_id} is {state}; a backup holds only readable ones")
            continue
        if recorded.get(snapshot_id) != digest:
            problems.append(
                f"snapshot {snapshot_id}'s digest differs between the catalog and {BACKUP_MANIFEST}"
            )
        directory = SnapshotLayout(source, table_id).snapshot_dir(snapshot_id)
        if not directory.is_dir():
            problems.append(f"snapshot {snapshot_id} is missing its files ({directory})")
        elif is_empty(directory):
            problems.append(
                f"snapshot {snapshot_id} is empty; restoring it would replace a table with nothing"
            )
        elif content_digest(directory) != digest:
            problems.append(
                f"snapshot {snapshot_id} does not match its recorded digest; the backup is damaged"
            )
    return problems


def restore(source: Path, root: Path) -> TableCatalog:
    """Restore the backup at ``source`` into an empty ``root``, or refuse entirely.

    The backup is verified, copied beside ``root``, verified again as copied, and only
    then renamed into place. A restore that fails leaves ``root`` as it was: nothing
    half-restored can be opened and queried as if it were the warehouse.

    Raises:
        BackupInvalid: ``root`` is not empty, or the backup — as found or as copied —
            fails verification. Every problem is listed.
    """
    _refuse_existing(root, "restore target")
    problems = verify_backup(source)
    if problems:
        raise BackupInvalid(problems)
    root.parent.mkdir(parents=True, exist_ok=True)
    work = _sibling(root, "restoring")
    try:
        shutil.copytree(source, work, ignore=shutil.ignore_patterns(BACKUP_MANIFEST))
        # Keep the manifest for the second check, then drop it: it describes the
        # backup, not the warehouse.
        shutil.copy2(source / BACKUP_MANIFEST, work / BACKUP_MANIFEST)
        problems = verify_backup(work)
        if problems:
            raise BackupInvalid(problems)
        (work / BACKUP_MANIFEST).unlink()
        if root.exists():
            root.rmdir()
        work.rename(root)
    except BaseException:
        _discard(work)
        raise
    return TableCatalog(root)


__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_MANIFEST",
    "BackupReport",
    "backup",
    "restore",
    "verify_backup",
]
