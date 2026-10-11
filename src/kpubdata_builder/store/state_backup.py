"""Back up every piece of Builder's state into one archive, and restore it whole (#1095).

``warehouse-backup`` (#705) copies the table catalog and its snapshots. Everything else
— the run events, the uploads, the sign-up ledger, the saved analyses, the publish
receipts, the encrypted provider keys, the runs themselves — lives in other files and
was in no backup at all. This module backs up all of it, and only all of it.

**What is in it.** The state inventory (``store/inventory.py``): every SQLite store, and
every other path under the output directory (``OUTPUT_PATHS``). An output directory
holding something on neither list is refused, by name — a backup that silently leaves
out what nobody listed is the incomplete backup this exists to prevent. The warehouse
goes in by the catalog's own rules (``warehouse/backup.py``) when it is a directory of
the output directory, as the compose file has it (``/data/warehouse``).

**One point in time, and how far that reaches.** Copying each store with SQLite's online
backup is consistent per store, not across them: a build that ends between two copies
is finished in one and running in the other. So a backup runs only while nothing
writes: it holds the exclusive state lock (``store/state_lock.py``), which every writer
— the server for as long as it runs, a build, the maintenance commands — holds shared.
While one does, the backup refuses rather than waits; stopping the server drains it as
any shutdown does. What the archive holds is then the state of the output directory at
one moment. It does not reach the identity provider's database (Keycloak keeps its
own), the environment (the credential master key above all) or anything outside the
output directory.

**Secrets stay as they are stored.** The provider-key store is copied as SQLite has it:
each key encrypted under ``KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY``. The master key is
not in the archive, is not read here, and is never written to any output; the manifest
names the variable so that a restore knows to bring it. Without that key the restored
provider keys cannot be read, which is the point of keeping it apart.

**The archive.** One uncompressed tar file — the runs and snapshots are Parquet and
compress no further::

    output/<path>      every store (an online copy) and every file, by its path
    warehouse/...      a warehouse backup (``warehouse/backup.py``), when there is one
    backup.json        format, release, time, and for every member its SHA-256, size
                       and, for a store, its schema version

It is written beside its destination and renamed into place at the end: a backup that
failed leaves nothing that looks like a finished one. Its working copies go beside the
destination too, never into the directory being backed up, so a backup cannot copy
itself.

**A restore is verified, staged, then swapped.** Every member is checked against its
checksum, every store against SQLite's integrity check and its recorded version — one a
newer release wrote is refused, as opening it would be (#1096) — and the warehouse and
the uploads against what refers to them. Only then is anything moved. The archive is
unpacked into ``.restoring/`` inside the output directory (the same filesystem, since
``/data`` is a mount point and cannot itself be renamed), and the swap is a series of
renames recorded in a journal: the old contents move into ``.restoring/previous/``, the
new ones move in, and the old contents end up in ``.before-restore-<time>/`` for the
operator to delete once satisfied. Nothing is overwritten. A restore killed midway
leaves the journal, and the server refuses to start on that directory until
``restore --recover`` (or another restore) puts the earlier state back. The build index
is rebuilt from the restored manifests at the end, as ``rebuild-index`` does.

**Not decided here.** How often to back up, how long to keep a backup, where to keep it
off this machine and encrypted, and the recovery point and time objectives are the
maintainers' to decide (#1095); this module takes a backup when it is asked to and
leaves its file where it is told.

Only the SQLite backend is backed up. ADR 0022 retires CUBRID; a CUBRID deployment's
index and keys are in the database, which this cannot copy, so both commands refuse it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import IO, Literal

from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS

from .inventory import OUTPUT_PATHS, SQLITE_SIDECARS, STORES, OutputPath
from .schema_version import open_read_only, stored_version
from .state_lock import LOCK_FILENAME, RESTORE_DIRNAME, RESTORE_JOURNAL, state_lock

#: The archive's layout version. A restore refuses any other.
FORMAT = 1
KIND = "kpubdata-builder-state"
MANIFEST_NAME = "backup.json"
_OUTPUT = "output"
_WAREHOUSE = "warehouse"
_CHUNK = 1 << 20
#: A manifest larger than this is not one this module wrote.
_MANIFEST_LIMIT = 256 << 20
#: Named in the manifest, never read: the archive carries ciphertext only.
MASTER_KEY_VARIABLE = "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY"

EntryKind = Literal["store", "file", "warehouse"]


class StateBackupRefused(RuntimeError):
    """A backup, a verification or a restore found reasons to stop; nothing was changed."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class BackupResult:
    """What a backup wrote."""

    path: Path
    stores: int
    files: int
    #: Snapshots of the warehouse in the archive; None when it holds no warehouse.
    snapshots: int | None
    #: Paths not copied, each for the reason the inventory gives.
    left_out: tuple[str, ...]


@dataclass(frozen=True)
class RestoreResult:
    """What a restore did."""

    output_root: Path
    #: Where the state the restore replaced was moved; None when there was none.
    previous: Path | None
    #: Runs in the rebuilt build index.
    indexed: int
    #: What an earlier, interrupted restore needed first: "" when nothing.
    recovered: str


def refuse_cubrid() -> None:
    """Refuse a deployment whose state is in CUBRID, which this does not copy (ADR 0022)."""
    from .backend import storage_backend

    if storage_backend() == "cubrid":
        raise StateBackupRefused(
            [
                "KPUBDATA_BUILDER_STORAGE_BACKEND is cubrid: the build index and the "
                "provider keys are in the CUBRID database, which this backup does not "
                "copy. ADR 0022 retires CUBRID; back that database up with CUBRID's own "
                "tools, or move the deployment to SQLite first."
            ]
        )


def supported_versions() -> dict[str, int]:
    """The schema version this release reads, for every store, by its path."""
    from ..credentials.store import SCHEMA as credentials
    from ..events.store import SCHEMA_VERSION as events
    from ..service.analyses_api import SCHEMA as analyses
    from ..service.provider_tests import SCHEMA as provider_tests
    from ..service.publish import RECEIPTS_SCHEMA as receipts
    from ..service.revisions import SCHEMA as revisions
    from ..service.user_ledger import SCHEMA as ledger
    from ..uploads.store import SCHEMA as uploads
    from ..warehouse.catalog import SCHEMA_VERSION as catalog
    from .build_index import SCHEMA_VERSION as build_index

    return {
        "_builds.sqlite": build_index,
        "_build_events.sqlite": events,
        "_publish_receipts.sqlite": receipts.version,
        ".service/provider-credentials.sqlite3": credentials.version,
        ".service/uploads.sqlite3": uploads.version,
        ".service/provider_tests.sqlite3": provider_tests.version,
        ".service/revisions.sqlite3": revisions.version,
        ".service/users.sqlite3": ledger.version,
        ".service/analyses.sqlite3": analyses.version,
        "_warehouse.sqlite": catalog,
    }


# -- the scan ----------------------------------------------------------------------------


@dataclass
class _Scan:
    stores: list[str]
    files: list[str]
    left_out: list[str]
    problems: list[str]


def _listed(rel: str, is_dir: bool) -> OutputPath | None:
    for entry in OUTPUT_PATHS:
        if entry.is_dir == is_dir and fnmatch.fnmatchcase(rel, entry.pattern):
            return entry
    return None


def scan_output(output_root: Path, warehouse_name: str | None = None) -> _Scan:
    """Sort everything under ``output_root`` by the inventory.

    ``warehouse_name`` is the warehouse's directory, skipped here because the catalog
    decides what of it is backed up. Anything on no list is a problem, by name.
    """
    stores = {store.path for store in STORES if store.root == "output"}
    sidecars = {path + suffix for path in stores for suffix in SQLITE_SIDECARS}
    scan = _Scan(stores=[], files=[], left_out=[], problems=[])

    def visit(entry: Path, rel: str) -> None:
        if entry.is_symlink():
            scan.problems.append(
                f"{rel} is a symbolic link; a backup copies what is in the output "
                "directory, not what a link points at"
            )
        elif rel == RESTORE_DIRNAME:
            scan.problems.append(
                f"{rel} is left from a restore that did not finish; run `kpubdata-builder "
                "restore --recover` first"
            )
        elif rel in stores and entry.is_file():
            scan.stores.append(rel)
        elif rel in sidecars:
            scan.left_out.append(rel)
        elif rel == ".service" and entry.is_dir():
            for child in sorted(entry.iterdir()):
                visit(child, f".service/{child.name}")
        else:
            listed = _listed(rel, entry.is_dir())
            if entry.is_dir() and "/" not in rel and (entry / "_warehouse.sqlite").is_file():
                scan.problems.append(
                    f"{rel} holds a table catalog, but the backup was not told it is the "
                    "warehouse; give it as --warehouse (or KPUBDATA_BUILDER_WAREHOUSE) so "
                    "the catalog and its snapshots are copied by the catalog's own rules"
                )
            elif listed is None:
                scan.problems.append(
                    f"{rel} is not in the state inventory (store/inventory.py), so "
                    "nobody has said whether it is state; a backup without it may be "
                    "incomplete. Add it to the inventory, or move it out of the output "
                    "directory"
                )
            elif listed.kind == "left_out":
                scan.left_out.append(rel)
            else:
                scan.files.extend(_files_under(entry, rel, scan.problems))

    if output_root.is_dir():
        for entry in sorted(output_root.iterdir()):
            if entry.name != warehouse_name:
                visit(entry, entry.name)
    return scan


def _files_under(directory: Path, rel: str, problems: list[str]) -> list[str]:
    found: list[str] = []
    for current, dirs, files in os.walk(directory):
        base = Path(current)
        for name in sorted(dirs):
            if (base / name).is_symlink():
                problems.append(f"{_rel(base / name, directory, rel)} is a symbolic link")
        dirs[:] = sorted(name for name in dirs if not (base / name).is_symlink())
        for name in sorted(files):
            path = base / name
            if path.is_symlink() or not path.is_file():
                problems.append(f"{_rel(path, directory, rel)} is not a regular file")
            else:
                found.append(_rel(path, directory, rel))
    return found


def _rel(path: Path, directory: Path, rel: str) -> str:
    return (PurePosixPath(rel) / path.relative_to(directory).as_posix()).as_posix()


# -- backup ------------------------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    resolved, base = path.resolve(), root.resolve()
    return resolved == base or base in resolved.parents


def _discard(path: Path) -> None:
    """Remove a working directory, read-only snapshot copies included."""
    from ..warehouse.layout import thaw

    if path.exists():
        thaw(path)
        shutil.rmtree(path, ignore_errors=True)


def _integrity(path: Path) -> str | None:
    """SQLite's own verdict on ``path``, or None when it is sound."""
    try:
        with closing(open_read_only(path)) as conn:
            rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.Error as exc:
        return str(exc)
    return None if rows == ["ok"] else "; ".join(rows[:5])


def _copy_store(source: Path, target: Path) -> None:
    """Copy one store through SQLite, so what its journal holds comes too."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(str(source), timeout=BUSY_TIMEOUT_SECONDS)) as live,
        closing(sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_SECONDS)) as copy,
    ):
        live.backup(copy)
        # A copy is read far from the process that wrote it, often read-only, and a WAL
        # database cannot be opened read-only without its shared-memory file.
        copy.execute("PRAGMA journal_mode=DELETE")


class _Hashing(io.RawIOBase):
    """Reads ``source`` and keeps the SHA-256 and size of what was read."""

    def __init__(self, source: IO[bytes]) -> None:
        self._source = source
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        data = self._source.read(-1 if size is None else size)
        self.digest.update(data)
        self.size += len(data)
        return data


def _add(
    tar: tarfile.TarFile, path: Path, name: str, kind: EntryKind, version: int | None = None
) -> dict[str, object]:
    status = path.stat()
    info = tarfile.TarInfo(name)
    info.size = status.st_size
    info.mtime = int(status.st_mtime)
    info.mode = status.st_mode & 0o777
    with path.open("rb") as handle:
        reader = _Hashing(handle)
        tar.addfile(info, reader)
    if reader.size != status.st_size:
        raise StateBackupRefused([f"{path} changed while it was copied"])
    entry: dict[str, object] = {
        "path": name,
        "kind": kind,
        "sha256": reader.digest.hexdigest(),
        "size": reader.size,
    }
    if version is not None:
        entry["schema_version"] = version
    return entry


def _warehouse_name(output_root: Path, warehouse: Path | None) -> str | None:
    if warehouse is None:
        return None
    if warehouse.resolve().parent != output_root.resolve():
        raise StateBackupRefused(
            [
                f"the warehouse {warehouse} is not a directory of the output directory "
                f"{output_root}. One archive is restored with one swap, which two "
                "directories apart cannot share. Keep the warehouse inside the output "
                "directory, as the compose file does (/data/warehouse), or back it up on "
                "its own with `warehouse-backup`"
            ]
        )
    return warehouse.name


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup(
    output_root: Path,
    destination: Path,
    *,
    warehouse: Path | None = None,
    work_dir: Path | None = None,
) -> BackupResult:
    """Write all of ``output_root``'s state to the archive ``destination``.

    Raises:
        StateBackupRefused: The deployment is on CUBRID, ``destination`` exists or lies
            inside what is backed up, the output directory holds something the inventory
            does not list, a store fails its integrity check, or the warehouse backup
            refused. Every reason found is listed; nothing is written.
        StateInUse: Something is writing the output directory or the warehouse.
    """
    from ..warehouse import backup as warehouse_backup
    from ..warehouse.catalog import CATALOG_FILENAME, TableCatalog
    from ..warehouse.errors import BackupInvalid

    refuse_cubrid()
    if not output_root.is_dir():
        raise StateBackupRefused([f"{output_root} is not a directory; there is nothing to back up"])
    if destination.exists():
        raise StateBackupRefused([f"{destination} already exists; nothing is overwritten"])
    work_parent = work_dir if work_dir is not None else destination.parent
    for place, what in ((destination, "the archive"), (work_parent, "the working directory")):
        for root in (output_root, warehouse):
            if root is not None and _inside(place, root):
                raise StateBackupRefused(
                    [f"{what} ({place}) is inside {root}, which is being backed up"]
                )
    warehouse_name = _warehouse_name(output_root, warehouse)
    roots = [output_root] + ([warehouse] if warehouse is not None else [])

    with state_lock(roots, exclusive=True, purpose="the backup"):
        scan = scan_output(output_root, warehouse_name)
        if scan.problems:
            raise StateBackupRefused(scan.problems)
        token = uuid.uuid4().hex[:8]
        work = work_parent / f".{destination.name}.work-{token}"
        partial = destination.parent / f".{destination.name}.partial-{token}"
        work.mkdir(parents=True)
        try:
            copies: list[tuple[str, Path, int | None]] = []
            problems: list[str] = []
            for rel in scan.stores:
                copy = work / _OUTPUT / rel
                _copy_store(output_root / rel, copy)
                damage = _integrity(copy)
                if damage is not None:
                    problems.append(f"{rel} fails SQLite's integrity check: {damage}")
                copies.append((rel, copy, stored_version(copy)))
            snapshots: int | None = None
            left_out = list(scan.left_out)
            if (
                warehouse_name is not None
                and (output_root / warehouse_name / CATALOG_FILENAME).is_file()
            ):
                try:
                    report = warehouse_backup.backup(
                        TableCatalog(output_root / warehouse_name), work / _WAREHOUSE
                    )
                except BackupInvalid as exc:
                    problems.extend(f"warehouse: {problem}" for problem in exc.problems)
                else:
                    snapshots = len(report.snapshots)
                    left_out.extend(
                        f"{warehouse_name}: snapshot {snapshot_id} (unreadable)"
                        for snapshot_id in report.left_out
                    )
            if problems:
                raise StateBackupRefused(problems)

            destination.parent.mkdir(parents=True, exist_ok=True)
            entries: list[dict[str, object]] = []
            with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as tar:
                for rel, copy, version in copies:
                    entries.append(_add(tar, copy, f"{_OUTPUT}/{rel}", "store", version))
                for rel in scan.files:
                    entries.append(_add(tar, output_root / rel, f"{_OUTPUT}/{rel}", "file"))
                if snapshots is not None:
                    for rel in _files_under(work / _WAREHOUSE, _WAREHOUSE, problems):
                        path = work / rel
                        version = stored_version(path) if path.name == CATALOG_FILENAME else None
                        entries.append(_add(tar, path, rel, "warehouse", version))
                manifest = _manifest(
                    entries, warehouse_name if snapshots is not None else None, left_out
                )
                info = tarfile.TarInfo(MANIFEST_NAME)
                info.size = len(manifest)
                info.mtime = int(datetime.now(timezone.utc).timestamp())
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(manifest))
            _fsync(partial)
            os.rename(partial, destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            _discard(work)
    return BackupResult(
        path=destination,
        stores=len(scan.stores),
        files=len(scan.files),
        snapshots=snapshots,
        left_out=tuple(left_out),
    )


def _manifest(
    entries: list[dict[str, object]], warehouse: str | None, left_out: list[str]
) -> bytes:
    from .. import __version__

    document = {
        "format": FORMAT,
        "kind": KIND,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "builder_version": __version__,
        "storage_backend": "sqlite",
        "consistency": (
            "the output directory at one moment: taken under the exclusive state lock, "
            "while no server, build or maintenance command held it"
        ),
        "warehouse": warehouse,
        "entries": entries,
        "left_out": left_out,
        "not_included": {
            "credential_master_key": (
                f"{MASTER_KEY_VARIABLE}: the provider keys in the archive are encrypted "
                "under it and cannot be read without it. Keep it apart from the archive "
                "and set it again where this is restored."
            ),
            "identity_provider": "the identity provider's (Keycloak's) own database",
            "settings": "the environment and the compose file",
        },
    }
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


# -- verification ------------------------------------------------------------------------


def _safe_name(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        return None
    if path.parts[0] not in (_OUTPUT, _WAREHOUSE, MANIFEST_NAME):
        return None
    if path.parts[0] == MANIFEST_NAME and len(path.parts) != 1:
        return None
    return path


def _unpack(
    archive: Path, into: Path
) -> tuple[dict[str, tuple[str, int]], bytes | None, list[str]]:
    """Extract ``archive`` into ``into``, member by member, refusing what is not plain."""
    found: dict[str, tuple[str, int]] = {}
    manifest: bytes | None = None
    problems: list[str] = []
    try:
        with tarfile.open(archive, "r:") as tar:
            for member in tar:
                path = _safe_name(member.name)
                if path is None:
                    problems.append(f"member {member.name!r} is not a path this archive writes")
                    continue
                if member.isdir():
                    continue
                if not member.isfile():
                    problems.append(f"member {member.name} is not a regular file")
                    continue
                name = path.as_posix()
                if name in found or (name == MANIFEST_NAME and manifest is not None):
                    problems.append(f"member {name} appears twice")
                    continue
                source = tar.extractfile(member)
                if source is None:
                    problems.append(f"member {name} cannot be read")
                    continue
                with source:
                    if name == MANIFEST_NAME:
                        if member.size > _MANIFEST_LIMIT:
                            problems.append(f"{MANIFEST_NAME} is too large to be one")
                            continue
                        manifest = source.read()
                        continue
                    target = into / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with target.open("wb") as out:
                        while chunk := source.read(_CHUNK):
                            digest.update(chunk)
                            size += len(chunk)
                            out.write(chunk)
                    os.chmod(target, (member.mode & 0o777) | 0o600)
                    found[name] = (digest.hexdigest(), size)
    except (tarfile.TarError, OSError, EOFError) as exc:
        problems.append(f"{archive} is damaged or cut short: {exc}")
    return found, manifest, problems


def _check(
    into: Path, found: dict[str, tuple[str, int]], raw: bytes | None
) -> tuple[dict[str, object], list[str]]:
    from ..warehouse import backup as warehouse_backup

    if raw is None:
        return {}, [f"the archive has no {MANIFEST_NAME}; it is not a state backup"]
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"{MANIFEST_NAME} cannot be read: {exc}"]
    if not isinstance(manifest, dict) or manifest.get("kind") != KIND:
        return {}, [f"{MANIFEST_NAME} does not describe a Builder state backup"]
    if manifest.get("format") != FORMAT:
        return manifest, [
            f"archive format {manifest.get('format')!r} is not {FORMAT}, the one this release reads"
        ]
    if manifest.get("storage_backend") != "sqlite":
        return manifest, ["the archive is not of a SQLite deployment"]

    problems: list[str] = []
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return manifest, [f"{MANIFEST_NAME} lists no entries"]
    supported = supported_versions()
    listed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            problems.append(f"{MANIFEST_NAME} has an entry that is not one: {entry!r}")
            continue
        name = entry["path"]
        listed.add(name)
        if name not in found:
            problems.append(f"{name} is listed but not in the archive")
            continue
        if found[name] != (entry.get("sha256"), entry.get("size")):
            problems.append(f"{name} does not match its checksum; the archive is damaged")
            continue
        if entry.get("kind") == "store" or (
            entry.get("kind") == "warehouse" and PurePosixPath(name).name == "_warehouse.sqlite"
        ):
            problems.extend(_check_store(into / name, name, entry.get("schema_version"), supported))
    for name in sorted(set(found) - listed):
        problems.append(f"{name} is in the archive but not in {MANIFEST_NAME}")
    if problems:
        return manifest, problems

    warehouse = manifest.get("warehouse")
    if warehouse is not None:
        if not isinstance(warehouse, str) or "/" in warehouse or warehouse in ("", ".", ".."):
            return manifest, [f"the warehouse directory {warehouse!r} is not a directory name"]
        problems.extend(
            f"warehouse: {problem}" for problem in warehouse_backup.verify_backup(into / _WAREHOUSE)
        )
    problems.extend(_check_uploads(into / _OUTPUT))
    return manifest, problems


def _check_store(path: Path, name: str, recorded: object, supported: dict[str, int]) -> list[str]:
    store = PurePosixPath(name).relative_to(PurePosixPath(name).parts[0]).as_posix()
    if store not in supported:
        return [f"{name} is not a store this release knows"]
    damage = _integrity(path)
    if damage is not None:
        return [f"{name} fails SQLite's integrity check: {damage}"]
    version = stored_version(path)
    if version != recorded:
        return [f"{name} records schema version {version}, the manifest says {recorded}"]
    if version is not None and version > supported[store]:
        return [
            f"{name} has schema version {version}; this release reads up to "
            f"{supported[store]}. A newer release wrote it: restore it with that release"
        ]
    return []


def _check_uploads(output: Path) -> list[str]:
    """Every upload kept as a file has that file, with the bytes its row recorded."""
    database = output / ".service" / "uploads.sqlite3"
    if not database.is_file():
        return []
    blobs = database.parent / f"{database.name}.blobs"
    with closing(open_read_only(database)) as conn:
        rows = conn.execute(
            "SELECT upload_id, blob_path, content_sha256 FROM uploads WHERE blob_path IS NOT NULL"
        ).fetchall()
    problems: list[str] = []
    for upload_id, blob_path, digest in rows:
        path = blobs / str(blob_path)
        if not _inside(path, blobs) or not path.is_file():
            problems.append(f"upload {upload_id} names a file the archive does not hold")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"upload {upload_id}'s file does not match the digest its row recorded")
    return problems


def _verify_into(archive: Path, into: Path) -> tuple[dict[str, object], list[str]]:
    found, raw, problems = _unpack(archive, into)
    if problems:
        return {}, problems
    return _check(into, found, raw)


def verify(archive: Path, *, work_dir: Path | None = None) -> list[str]:
    """Every reason ``archive`` would not restore whole; empty when there is none.

    The archive is unpacked into a working directory beside it (or in ``work_dir``),
    checked, and the copy removed.
    """
    if not archive.is_file():
        return [f"{archive} is not a file"]
    parent = work_dir if work_dir is not None else archive.parent
    into = parent / f".{archive.name}.verify-{uuid.uuid4().hex[:8]}"
    into.mkdir(parents=True)
    try:
        return _verify_into(archive, into)[1]
    finally:
        _discard(into)


# -- restore -----------------------------------------------------------------------------


@dataclass
class _Journal:
    """What a restore is moving, written before each step so a crash can be undone."""

    #: ``moving-out``, ``moving-in`` or ``done``.
    phase: str
    old: list[str]
    new: list[str]

    @classmethod
    def read(cls, staging: Path) -> _Journal:
        data = json.loads((staging / RESTORE_JOURNAL).read_text(encoding="utf-8"))
        return cls(
            phase=str(data["phase"]),
            old=[str(name) for name in data["old"]],
            new=[str(name) for name in data["new"]],
        )

    def write(self, staging: Path) -> None:
        temporary = staging / f"{RESTORE_JOURNAL}.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"phase": self.phase, "old": self.old, "new": self.new}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, staging / RESTORE_JOURNAL)
        _fsync(staging)


def _contents(root: Path) -> list[str]:
    return sorted(name for name in os.listdir(root) if name not in (LOCK_FILENAME, RESTORE_DIRNAME))


def _swap(root: Path, staging: Path, new: Path) -> Path | None:
    """Move the old contents of ``root`` aside and the new ones in, by renames only."""
    previous = staging / "previous"
    previous.mkdir()
    journal = _Journal(phase="moving-out", old=_contents(root), new=sorted(os.listdir(new)))
    journal.write(staging)
    try:
        for name in journal.old:
            os.rename(root / name, previous / name)
        journal.phase = "moving-in"
        journal.write(staging)
        for name in journal.new:
            os.rename(new / name, root / name)
        journal.phase = "done"
        journal.write(staging)
    except BaseException:
        _roll_back(root, staging, journal)
        raise
    return _finish(root, staging)


def _finish(root: Path, staging: Path) -> Path | None:
    previous = staging / "previous"
    kept: Path | None = None
    if previous.is_dir() and any(previous.iterdir()):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        kept = root / f".before-restore-{stamp}-{uuid.uuid4().hex[:6]}"
        os.rename(previous, kept)
    (staging / RESTORE_JOURNAL).unlink(missing_ok=True)
    _discard(staging)
    return kept


def _roll_back(root: Path, staging: Path, journal: _Journal) -> None:
    """Put the contents the journal says were there back, and set the new ones aside.

    Every old entry moves out before any new one moves in, so in ``moving-in`` all that
    is in ``root`` is new, and in ``moving-out`` all that is in it is old.
    """
    previous = staging / "previous"
    new = staging / "new"
    if journal.phase == "moving-in":
        new.mkdir(exist_ok=True)
        for name in journal.new:
            if os.path.lexists(root / name):
                os.rename(root / name, new / name)
    for name in journal.old:
        if os.path.lexists(previous / name):
            os.rename(previous / name, root / name)
    (staging / RESTORE_JOURNAL).unlink(missing_ok=True)


def _recover_locked(root: Path) -> str:
    staging = root / RESTORE_DIRNAME
    if not staging.exists():
        return ""
    path = staging / RESTORE_JOURNAL
    if path.is_file():
        journal = _Journal.read(staging)
        if journal.phase == "done":
            _finish(root, staging)
            return "finished an interrupted restore that had already swapped the state in"
        _roll_back(root, staging, journal)
        _discard(staging)
        return "put back the state an interrupted restore had begun to replace"
    _discard(staging)
    return "removed what an interrupted restore had unpacked; the state was not touched"


def recover(output_root: Path) -> str:
    """Undo or finish a restore into ``output_root`` that was interrupted.

    Returns:
        What was done, or "" when there was nothing to do.

    Raises:
        StateInUse: Something is using the directory.
    """
    if not output_root.is_dir():
        return ""
    with state_lock([output_root], exclusive=True, purpose="the recovery"):
        return _recover_locked(output_root)


def restore(archive: Path, output_root: Path) -> RestoreResult:
    """Replace ``output_root``'s state with the archive's, whole or not at all.

    Raises:
        StateBackupRefused: The deployment is on CUBRID, or the archive failed
            verification; every reason is listed and ``output_root`` is as it was.
        StateInUse: Something is using the directory.
    """
    from ..warehouse import backup as warehouse_backup
    from ..warehouse.errors import BackupInvalid
    from .build_index import rebuild_index

    refuse_cubrid()
    if not archive.is_file():
        raise StateBackupRefused([f"{archive} is not a file"])
    if _inside(archive, output_root):
        raise StateBackupRefused(
            [f"the archive {archive} is inside {output_root}, which it replaces"]
        )
    output_root.mkdir(parents=True, exist_ok=True)
    with state_lock([output_root], exclusive=True, purpose="the restore"):
        recovered = _recover_locked(output_root)
        staging = output_root / RESTORE_DIRNAME
        staging.mkdir()
        try:
            unpacked = staging / "unpacked"
            unpacked.mkdir()
            manifest, problems = _verify_into(archive, unpacked)
            if problems:
                raise StateBackupRefused(problems)
            new = staging / "new"
            if (unpacked / _OUTPUT).is_dir():
                (unpacked / _OUTPUT).rename(new)
            else:
                new.mkdir()
            warehouse = manifest.get("warehouse")
            if isinstance(warehouse, str):
                if os.path.lexists(new / warehouse):
                    raise StateBackupRefused(
                        [f"the warehouse directory {warehouse} is also a path of the output"]
                    )
                try:
                    warehouse_backup.restore(unpacked / _WAREHOUSE, new / warehouse)
                except BackupInvalid as exc:
                    raise StateBackupRefused(
                        [f"warehouse: {problem}" for problem in exc.problems]
                    ) from exc
            _discard(unpacked)
        except BaseException:
            _discard(staging)
            raise
        previous = _swap(output_root, staging, new)
        indexed = rebuild_index(output_root)
    return RestoreResult(
        output_root=output_root, previous=previous, indexed=indexed, recovered=recovered
    )


__all__ = [
    "FORMAT",
    "KIND",
    "MANIFEST_NAME",
    "MASTER_KEY_VARIABLE",
    "BackupResult",
    "RestoreResult",
    "StateBackupRefused",
    "backup",
    "recover",
    "refuse_cubrid",
    "restore",
    "scan_output",
    "supported_versions",
    "verify",
]
