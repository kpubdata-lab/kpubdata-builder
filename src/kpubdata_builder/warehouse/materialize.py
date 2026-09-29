"""Turn a finished Gold package into a committed table snapshot (#703).

The build promise is "collect Korean public data into my own environment and query
it". That promise is kept with nothing published, so a build has to be able to end at
a committed table — and until now it could not, because nothing wrote to the catalog.

The order here is the contract from ADR-less #699, restated because getting it wrong
is silent:

1. ``begin_snapshot`` — the catalog knows about it, so GC does not mistake the staging
   directory for an orphan
2. copy the Gold files into staging
3. write the manifest, then record the digest of what was written
4. ``mark_validated``
5. ``promote`` — one rename, the files reach their final path
6. ``commit_snapshot`` — one transaction: state to committed, compare-and-swap the
   pointer

Files are final **before** the pointer moves. A crash at any step leaves the previous
snapshot readable, because nothing writes over it.

Publishing is not here and that is the point: a materialised table needs no publish
credential, and a failed publish cannot touch a snapshot that already exists.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..spec import JsonValue
from .catalog import SnapshotRow, TableCatalog, TableRow
from .errors import SnapshotConflict, SnapshotStateError
from .layout import SnapshotLayout, SnapshotManifest, content_digest


@dataclass(frozen=True)
class MaterializeResult:
    """What a materialise produced.

    Attributes:
        table: The table row after the pointer moved.
        snapshot: The committed snapshot.
        snapshot_dir: Where its files live. Immutable from here on.
    """

    table: TableRow
    snapshot: SnapshotRow
    snapshot_dir: Path


def materialize(
    catalog: TableCatalog,
    *,
    workspace_id: str,
    logical_name: str,
    source_dir: Path,
    run_id: str,
    schema_version: int = 1,
    coverage_hash: str = "",
    owner_id: str | None = None,
    coverage_fingerprint: str | None = None,
    source_params_fingerprint: str | None = None,
    schema_contract_version: str | None = None,
    row_count: int | None = None,
    expected_revision: int | None = None,
    coverage: Mapping[str, JsonValue] | None = None,
) -> MaterializeResult:
    """Commit the contents of ``source_dir`` as a new snapshot of a table.

    Creates the table when it does not exist yet, so a first build does not need a
    separate step.

    The digest is computed **after** the files are in staging and recorded before the
    commit, which is the only order ``verify_before_commit`` can check later. A digest
    recorded before the write describes something that may not have happened.

    Args:
        catalog: Where the snapshot is recorded.
        workspace_id: Owning workspace. One personal workspace, for now.
        logical_name: The table's human-readable name, unique per workspace.
        source_dir: A finished Gold directory. Copied, not moved — the run workspace
            stays intact because the manifest's output paths point into it.
        run_id: The build that produced this.
        schema_version: Table schema version.
        coverage_hash: Hash of the collected range, for the snapshot record.
        owner_id: Who produced it. Drift baselines never cross owners (#700), and a
            snapshot with no owner is never assumed to be anyone's.
        coverage_fingerprint: What population was collected. Leaving it out means
            "cannot compare on that axis" rather than "matches".
        source_params_fingerprint: The request parameters behind the collection.
        schema_contract_version: The schema contract in force.
        row_count: Records in the table, when known.
        expected_revision: The table revision the build started from (#787). A refresh
            committed by another build since then makes this commit a conflict — the
            older data does not replace the newer. None reads the revision just before
            the commit, which only protects against a commit racing this one.
        coverage: Whether the fetch collected what the provider reported (#816), stored
            with the snapshot as JSON. None records nothing — which reads as unknown,
            never as complete.

    Returns:
        The committed snapshot and where it lives.

    Raises:
        FileNotFoundError: ``source_dir`` does not exist.
        SnapshotStateError: Verification found the promoted files empty or changed.
        SnapshotConflict: Another refresh committed first. Not a retry.

    When a commit is refused for either reason, the snapshot is marked ``abandoned``
    before the exception propagates, so garbage collection can reclaim its files. The
    commit is never retried — see #699 — but leaving the bytes behind for ever is not
    the alternative.
    """
    if not source_dir.is_dir():
        raise FileNotFoundError(f"no such gold directory: {source_dir}")

    table = catalog.create_table(workspace_id, logical_name)
    snapshot = catalog.begin_snapshot(
        table.id,
        run_id=run_id,
        schema_version=schema_version,
        coverage_hash=coverage_hash,
        # A placeholder: the real digest can only be known once the bytes are in
        # staging, and it is recorded below before the commit.
        artifact_digest="",
        row_count=row_count,
        owner_id=owner_id,
        coverage_fingerprint=coverage_fingerprint,
        source_params_fingerprint=source_params_fingerprint,
        schema_contract_version=schema_contract_version,
        coverage=json.dumps(coverage, sort_keys=True) if coverage is not None else None,
    )

    layout = SnapshotLayout(catalog.root, table.id)
    staging = layout.begin(snapshot.id)
    for entry in sorted(source_dir.iterdir()):
        target = staging / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)

    digest = content_digest(staging)
    catalog.set_artifact_digest(snapshot.id, digest)
    layout.write_manifest(
        snapshot.id,
        SnapshotManifest(
            snapshot_id=snapshot.id,
            table_id=table.id,
            logical_name=logical_name,
            run_id=run_id,
            schema_version=schema_version,
            coverage_hash=coverage_hash,
            artifact_digest=digest,
            row_count=row_count,
            created_at=snapshot.created_at,
        ),
    )
    catalog.mark_validated(snapshot.id)
    promoted = layout.promote(snapshot.id)

    try:
        updated = catalog.commit_snapshot(
            snapshot.id,
            expected_revision=(
                expected_revision
                if expected_revision is not None
                else catalog.get_table(table.id).revision
            ),
            verify_before_commit=True,
        )
    except (SnapshotConflict, SnapshotStateError):
        # The snapshot will never be committed, so mark it reclaimable and let the
        # exception through. Not retried: #699 documents why — the loser built on a
        # state that no longer exists, and retrying would overwrite the commit that
        # won. Without this it stayed `validated` for ever with its files promoted,
        # and nothing would ever look at it again (#738).
        catalog.abandon(snapshot.id)
        raise
    return MaterializeResult(
        table=updated,
        snapshot=catalog.get_snapshot(snapshot.id),
        snapshot_dir=promoted,
    )


__all__ = ["MaterializeResult", "materialize"]
