"""Snapshot directory layout and manifest (#699).

Existing stage output is written by replacing the destination directory
(``stages/_atomic.py``). That leaves a window between the two renames in which
the final path **does not exist** — a reader landing there gets "no such table"
rather than "the previous version".

A warehouse cannot work that way. A committed snapshot is immutable, and a
refresh means **writing a new snapshot and moving a pointer**. The previous
snapshot's directory is never touched.

Layout::

    <root>/tables/<table_id>/
      _staging/<snapshot_id>/     being written; gone once committed
      snapshots/<snapshot_id>/    immutable once it exists
        _snapshot.json            manifest

``_staging`` and ``snapshots`` must live on the same filesystem, because
``promote()`` moves between them with a single ``rename``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from ..stages._path_safety import validate_path_segment
from .errors import ImmutableSnapshot

_TABLES_DIRNAME = "tables"
_STAGING_DIRNAME = "_staging"
_SNAPSHOTS_DIRNAME = "snapshots"

MANIFEST_FILENAME = "_snapshot.json"

# Strip write permission from a committed snapshot. This guards against an
# accidental overwrite and is **not a security boundary** — the owner can chmod it
# back. What actually enforces the contract is promote() refusing an existing path.
_READ_ONLY_FILE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_READ_ONLY_DIR = _READ_ONLY_FILE | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


@dataclass(frozen=True)
class SnapshotManifest:
    """Manifest written alongside a snapshot.

    It lets the contents of the warehouse be identified from disk alone if the
    catalog database is lost. The catalog stays canonical; this file is the copy
    the catalog can be rebuilt from.

    Attributes:
        snapshot_id: Snapshot identifier.
        table_id: Owning table.
        logical_name: Human-readable table name.
        run_id: The build run that produced this snapshot.
        schema_version: Table schema version.
        coverage_hash: Hash of the collected coverage; equal coverage hashes
            mean the same range was fetched.
        artifact_digest: Digest of the artifact contents.
        row_count: Record count, or None when unknown.
        created_at: Creation time (UTC ISO 8601).
    """

    snapshot_id: str
    table_id: str
    logical_name: str
    run_id: str
    schema_version: int
    coverage_hash: str
    artifact_digest: str
    row_count: int | None
    created_at: str

    def to_json(self) -> str:
        """Serialise the manifest reproducibly."""
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> SnapshotManifest:
        """Read a manifest back from JSON."""
        data = json.loads(text)
        return cls(**{field: data[field] for field in cls.__dataclass_fields__})


class SnapshotLayout:
    """Computes the snapshot paths for one table.

    Paths only. State and the current pointer belong to ``TableCatalog``.
    """

    def __init__(self, root: Path, table_id: str) -> None:
        """Set up the layout.

        Args:
            root: Warehouse root.
            table_id: Table identifier. Used as a path segment, so it is validated.
        """
        validate_path_segment(table_id, field_name="table_id")
        self._table_dir = root / _TABLES_DIRNAME / table_id

    @property
    def table_dir(self) -> Path:
        """Root directory for this table."""
        return self._table_dir

    @property
    def staging_root(self) -> Path:
        """Directory holding snapshots that are still being written."""
        return self._table_dir / _STAGING_DIRNAME

    @property
    def snapshots_root(self) -> Path:
        """Directory holding committed snapshots."""
        return self._table_dir / _SNAPSHOTS_DIRNAME

    def staging_dir(self, snapshot_id: str) -> Path:
        """Path where ``snapshot_id`` is being written."""
        validate_path_segment(snapshot_id, field_name="snapshot_id")
        return self.staging_root / snapshot_id

    def snapshot_dir(self, snapshot_id: str) -> Path:
        """Final, immutable path of ``snapshot_id``."""
        validate_path_segment(snapshot_id, field_name="snapshot_id")
        return self.snapshots_root / snapshot_id

    def begin(self, snapshot_id: str) -> Path:
        """Create and return the staging directory.

        An existing directory is reused, which is what resuming an interrupted
        attempt looks like. Staging is not immutable.
        """
        path = self.staging_dir(snapshot_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_manifest(self, snapshot_id: str, manifest: SnapshotManifest) -> Path:
        """Write the manifest into the staging directory.

        Call this **before** committing: after promote the directory is immutable.
        """
        path = self.staging_dir(snapshot_id) / MANIFEST_FILENAME
        path.write_text(manifest.to_json(), encoding="utf-8")
        return path

    def read_manifest(self, snapshot_id: str) -> SnapshotManifest:
        """Read a committed snapshot's manifest."""
        path = self.snapshot_dir(snapshot_id) / MANIFEST_FILENAME
        return SnapshotManifest.from_json(path.read_text(encoding="utf-8"))

    def promote(self, snapshot_id: str) -> Path:
        """Move the staging directory to its final path.

        A single ``rename``, which is atomic on one filesystem. **The previous
        current snapshot is not touched** — that is the whole reason this module
        exists.

        Raises:
            ImmutableSnapshot: The final path already exists. A committed
                snapshot is never overwritten.
            FileNotFoundError: There is no staging directory to promote.
        """
        source = self.staging_dir(snapshot_id)
        target = self.snapshot_dir(snapshot_id)
        if target.exists():
            raise ImmutableSnapshot(f"snapshot {snapshot_id!r} is already committed: {target}")
        if not source.is_dir():
            raise FileNotFoundError(f"no staging directory to promote: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        _freeze(target)
        return target

    def discard_staging(self, snapshot_id: str) -> None:
        """Remove the staging directory; do nothing when it is absent."""
        shutil.rmtree(self.staging_dir(snapshot_id), ignore_errors=True)

    def orphan_staging_ids(self, known: frozenset[str]) -> list[str]:
        """Names of staging directories the catalog does not know about.

        Those are what a crash leaves behind. Anything the catalog knows may
        still be in progress, so it is excluded.
        """
        if not self.staging_root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.staging_root.iterdir()
            if entry.is_dir() and entry.name not in known
        )


def content_digest(directory: Path) -> str:
    """A digest over every file in a snapshot directory, excluding the manifest.

    The manifest is left out because it carries the digest: including it would make the
    value depend on itself. Paths are relative and sorted, so the same bytes in the same
    layout give the same digest on any machine.

    Raises:
        FileNotFoundError: The directory does not exist.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"no such snapshot directory: {directory}")
    digest = hashlib.sha256()
    files = sorted(
        path for path in directory.rglob("*") if path.is_file() and path.name != MANIFEST_FILENAME
    )
    for path in files:
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def is_empty(directory: Path) -> bool:
    """Whether a snapshot directory holds no file other than its manifest.

    An empty snapshot is a build that produced nothing and said it succeeded. Committing
    one replaces a table with emptiness, which is worse than failing.
    """
    if not directory.is_dir():
        return True
    return not any(
        path.is_file() and path.name != MANIFEST_FILENAME for path in directory.rglob("*")
    )


def _freeze(directory: Path) -> None:
    """Strip write permission from a whole directory tree.

    Walks bottom-up: making a directory read-only first would prevent changing
    what is inside it.

    A permission failure is not raised. This only guards against mistakes, and
    what enforces the contract is ``promote()`` refusing an existing path. A
    filesystem that does not carry permissions (some mounts) must not fail a
    commit.
    """
    paths: list[Path] = []
    for current, dirs, files in os.walk(directory):
        base = Path(current)
        paths.extend(base / name for name in files)
        paths.extend(base / name for name in dirs)
    paths.append(directory)
    for path in reversed(paths):
        try:
            path.chmod(_READ_ONLY_DIR if path.is_dir() else _READ_ONLY_FILE)
        except OSError:
            return


def thaw(directory: Path) -> None:
    """Undo ``_freeze``, which garbage collection needs before deleting.

    Entries inside a read-only directory cannot be removed, so write permission
    has to come back first.
    """
    for current, dirs, files in os.walk(directory):
        base = Path(current)
        for name in (*dirs, *files):
            with contextlib.suppress(OSError):
                (base / name).chmod(stat.S_IRWXU)
    with contextlib.suppress(OSError):
        directory.chmod(stat.S_IRWXU)


__all__ = [
    "MANIFEST_FILENAME",
    "content_digest",
    "is_empty",
    "SnapshotLayout",
    "SnapshotManifest",
    "thaw",
]
