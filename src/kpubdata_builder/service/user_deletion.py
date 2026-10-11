"""Delete one user and their data, step by step, without bringing them back (#1102).

An operator runs ``kpubdata-builder delete-user`` for a deletion request. There was no
way to remove a user and everything they left: the routes delete one upload, one
analysis, one key at a time, and a job still running could write a run back after its
owner's data was gone.

**The tombstone comes first.** The first thing a deletion writes is its journal,
``.service/deleted-users/<subject>.json``. From then on the server refuses the user
on every request — with the sign-up ledger's ``signup_rejected``, before the ledger is
read, so signing in again does not record them anew — and refuses a build of theirs
however it arrives:

- a submission recorded before the check is cancelled at once (``BuilderService``);
- a queued job is cancelled when a worker takes it, and never runs;
- a running build is cancelled at its next stage boundary, and cannot commit to a
  normal ending once its owner is deleted.

The deletion waits for every run of the user's that has not ended (``--wait``), and for
every run whose owner cannot be known until it ends — a synchronous build — and refuses,
naming the runs, when one is still going. Stopping the server ends them too: its
shutdown drain ends the queued jobs and cancels the running ones (``docs/deploy.md``
§7).

**Then the data, one step at a time:** saved analyses (their holds released first),
warehouse exports, warehouse tables and snapshots, runs (workspace, index row, events,
submission and ending), uploads, saved provider keys, provider test results, and the
sign-up ledger row. A snapshot under a live lease or hold, or the current snapshot of a
table that is not the user's own, is not touched: the step stops and says what blocks
it. Each step is idempotent and each is recorded in the journal as it finishes, so a
deletion that crashed or was refused is finished by running the command again — every
step runs again and finds only what is left. The last step looks for the user in every
store; the deletion is complete only when it finds nothing.

**The audit record names no one.** The journal and the audit log line of each step
(``admin_audit.record_user_deletion``) carry the deletion's *subject*: a hash of the
user's id under a label of its own. It cannot be turned back into the id, or into the
email the sign-up ledger held; it can only be matched by someone who already has the
id — which is how a restore recognises a deleted user (``store/state_backup.py``).

**A restore does not bring a user back.** The journals are carried from the state a
restore replaces into the one it restores, and the deletion is run again on whatever the
archive held of the user.

**Not done here.** Removing the user from the identity provider (Keycloak), and the
retention of backups taken before the deletion, are not decided (#1102). Publish
receipts — the record of what went out to an external destination — are kept: a
publication outside Builder is not deleted by deleting the user.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..pipeline.cancellation import CancellationProbe
from ..spec import JsonValue
from .admin_audit import record_user_deletion

#: Where the journals are, under the output directory's ``.service``.
DELETIONS_DIRNAME = "deleted-users"
_LOCK_NAME = ".lock"
_SUBJECT_LABEL = b"kpubdata-builder/deleted-user\x00"
_FORMAT = 1

Step = Literal[
    "tombstone",
    "jobs",
    "analyses",
    "exports",
    "warehouse",
    "runs",
    "uploads",
    "provider_keys",
    "provider_tests",
    "ledger",
    "verify",
]
#: The steps, in the order they run. Each is idempotent.
STEPS: tuple[Step, ...] = (
    "tombstone",
    "jobs",
    "analyses",
    "exports",
    "warehouse",
    "runs",
    "uploads",
    "provider_keys",
    "provider_tests",
    "ledger",
    "verify",
)


class DeletionRefused(Exception):
    """The deletion did not start: the id is not a user's, or another deletion runs."""


def deletions_dir(output_root: Path) -> Path:
    """The directory of the deletion journals of ``output_root``."""
    return output_root / ".service" / DELETIONS_DIRNAME


def deletion_subject(owner_id: str) -> str:
    """The pseudonym a deletion is recorded under: a hash of ``owner_id``, labelled."""
    return hashlib.sha256(_SUBJECT_LABEL + owner_id.encode("utf-8")).hexdigest()


def _journal_path(output_root: Path, subject: str) -> Path:
    return deletions_dir(output_root) / f"{subject}.json"


def is_deleted(output_root: Path, owner_id: str) -> bool:
    """Whether a deletion of ``owner_id`` has begun. A file test, cheap per request."""
    return _journal_path(output_root, deletion_subject(owner_id)).is_file()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_subject(name: str) -> bool:
    return len(name) == 64 and all(c in "0123456789abcdef" for c in name)


@dataclass
class _Journal:
    """What a deletion has done: one file per user, written whole each time."""

    subject: str
    requested_at: str
    completed_at: str | None = None
    history: list[dict[str, JsonValue]] = field(default_factory=list)

    @classmethod
    def read(cls, path: Path) -> _Journal:
        data = json.loads(path.read_text(encoding="utf-8"))
        history = data.get("history")
        completed = data.get("completed_at")
        return cls(
            subject=str(data["subject"]),
            requested_at=str(data["requested_at"]),
            completed_at=completed if isinstance(completed, str) else None,
            history=[entry for entry in history if isinstance(entry, dict)]
            if isinstance(history, list)
            else [],
        )

    def write(self, path: Path) -> None:
        body = {
            "format": _FORMAT,
            "subject": self.subject,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "history": self.history,
        }
        temporary = path.with_name(f"{path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def journals(output_root: Path) -> dict[str, Path]:
    """Every deletion journal of ``output_root``, by subject."""
    directory = deletions_dir(output_root)
    if not directory.is_dir():
        return {}
    return {
        path.stem: path
        for path in sorted(directory.glob("*.json"))
        if _is_subject(path.stem) and path.is_file()
    }


def pending_subjects(output_root: Path) -> tuple[str, ...]:
    """The deletions that have begun and not completed."""
    pending: list[str] = []
    for subject, path in journals(output_root).items():
        try:
            if _Journal.read(path).completed_at is None:
                pending.append(subject)
        except (OSError, ValueError, KeyError):
            pending.append(subject)
    return tuple(pending)


@dataclass(frozen=True)
class StepResult:
    """One step: what it removed (or would), and what stopped it."""

    step: Step
    removed: int = 0
    blocked: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeletionReport:
    """What a deletion did, or — not applied — would do."""

    subject: str
    applied: bool
    complete: bool
    steps: tuple[StepResult, ...]

    @property
    def blocked(self) -> tuple[str, ...]:
        return tuple(reason for step in self.steps for reason in step.blocked)


# -- the stores ---------------------------------------------------------------------------


class _State:
    """The stores of one output directory, each opened only when its file is there.

    A deletion must not leave behind a store the deployment never had.
    """

    def __init__(self, output_root: Path, warehouse_root: Path | None) -> None:
        from ..events.store import BuildEventStore, events_store_path
        from ..warehouse.catalog import CATALOG_FILENAME, TableCatalog

        self.output_root = output_root
        self.warehouse_root = warehouse_root
        self.service = output_root / ".service"
        self._stack = ExitStack()
        self.events = (
            BuildEventStore(output_root) if events_store_path(output_root).is_file() else None
        )
        if self.events is not None:
            self._stack.callback(self.events.close)
        self.catalog = (
            TableCatalog(warehouse_root)
            if warehouse_root is not None and (warehouse_root / CATALOG_FILENAME).is_file()
            else None
        )
        if self.catalog is not None:
            self._stack.callback(self.catalog.close)

    def close(self) -> None:
        self._stack.close()

    def path(self, name: str) -> Path | None:
        path = self.service / name
        return path if path.is_file() else None


@contextmanager
def _opened(output_root: Path, warehouse_root: Path | None) -> Iterator[_State]:
    state = _State(output_root, warehouse_root)
    try:
        yield state
    finally:
        state.close()


def _manifest_owner(run_dir: Path) -> tuple[bool, str | None]:
    """(whether a manifest is there, the owner it names)."""
    path = run_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        return False, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, None
    owner = raw.get("owner_id") if isinstance(raw, dict) else None
    return True, owner if isinstance(owner, str) else None


def _workspaces(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.name.startswith((".", "_")) or entry.is_symlink() or not entry.is_dir():
            continue
        yield entry


def _gc_dir(output_root: Path) -> Path:
    from ..retention import RUN_GC_DIRNAME

    return output_root / RUN_GC_DIRNAME


def owners_in_state(output_root: Path, warehouse_root: Path | None) -> dict[str, frozenset[str]]:
    """Every owner each store names, by store: what a restore and the last step read."""
    from ..credentials.store import stored_owners
    from ..uploads.store import SQLiteUploadRepository
    from .analyses_api import AnalysisStore
    from .exports_api import ExportStore
    from .provider_tests import ProviderTestLog
    from .user_ledger import UserLedger

    found: dict[str, frozenset[str]] = {}
    with _opened(output_root, warehouse_root) as state:
        runs: set[str] = set()
        if state.events is not None:
            runs |= state.events.submission_owners()
        for root in (output_root, _gc_dir(output_root)):
            for run_dir in _workspaces(root):
                owner = _manifest_owner(run_dir)[1]
                if owner is not None:
                    runs.add(owner)
        found["runs"] = frozenset(runs)
        ledger = state.path("users.sqlite3")
        if ledger is not None:
            found["ledger"] = frozenset(entry.user_id for entry in UserLedger(ledger).list())
        uploads = state.path("uploads.sqlite3")
        if uploads is not None:
            found["uploads"] = SQLiteUploadRepository(uploads).owners()
        analyses = state.path("analyses.sqlite3")
        if analyses is not None:
            found["analyses"] = AnalysisStore(analyses).owners()
        keys = state.path("provider-credentials.sqlite3")
        if keys is not None:
            found["provider_keys"] = stored_owners(keys)
        tests = state.path("provider_tests.sqlite3")
        if tests is not None:
            found["provider_tests"] = ProviderTestLog(tests).owners()
        exports = ExportStore(state.service / "exports").records()
        found["exports"] = frozenset(
            str(record["owner_id"]) for record in exports if isinstance(record.get("owner_id"), str)
        )
        if state.catalog is not None:
            found["warehouse"] = state.catalog.snapshot_owners()
    return found


# -- the steps ----------------------------------------------------------------------------


def _blocking_runs(state: _State, owner_id: str) -> tuple[str, ...]:
    """The runs that have not ended and are the user's, or may be."""
    if state.events is None:
        return ()
    blocking: list[str] = []
    for run_id in state.events.unfinished_runs():
        submission = state.events.submission(run_id)
        if submission is not None:
            if submission.owner_id == owner_id:
                blocking.append(f"run {run_id} of this user has not ended")
            continue
        has_manifest, owner = _manifest_owner(state.output_root / run_id)
        if owner == owner_id:
            blocking.append(f"run {run_id} of this user has not ended")
        elif not has_manifest:
            blocking.append(
                f"run {run_id} has not ended, and whose it is is known only when it does"
            )
    return tuple(blocking)


def _step_jobs(
    state: _State,
    owner_id: str,
    *,
    wait_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> StepResult:
    deadline = clock() + max(wait_seconds, 0.0)
    blocking = _blocking_runs(state, owner_id)
    while blocking and clock() < deadline:
        sleep(min(poll_seconds, max(deadline - clock(), 0.0)))
        blocking = _blocking_runs(state, owner_id)
    return StepResult("jobs", blocked=blocking)


def _step_analyses(state: _State, owner_id: str, *, apply: bool) -> StepResult:
    from .analyses_api import AnalysisStore
    from .ownership import owner_workspace

    path = state.path("analyses.sqlite3")
    if path is None:
        return StepResult("analyses")
    store = AnalysisStore(path)
    analyses = store.owned_by(owner_id, owner_workspace(owner_id))
    if apply:
        for analysis in analyses:
            # The hold first: an analysis row gone with its hold left would keep the
            # snapshot for good, and nothing would say why.
            if state.catalog is not None:
                state.catalog.release_hold(analysis.hold_id)
            store.delete(analysis.workspace_id, analysis.analysis_id)
    return StepResult("analyses", removed=len(analyses))


def _step_exports(state: _State, owner_id: str, *, apply: bool) -> StepResult:
    from .exports_api import ExportStore
    from .ownership import owner_workspace

    workspace = owner_workspace(owner_id)
    store = ExportStore(state.service / "exports")
    owned = [
        str(record["export_id"])
        for record in store.records()
        if record.get("owner_id") == owner_id or record.get("workspace_id") == workspace
    ]
    if apply:
        for export_id in owned:
            store.remove(export_id)
    return StepResult("exports", removed=len(owned))


def _own_holds(state: _State, owner_id: str) -> frozenset[str]:
    """The holds of the user's saved analyses: the analyses step releases them."""
    from .analyses_api import AnalysisStore
    from .ownership import owner_workspace

    path = state.path("analyses.sqlite3")
    if path is None:
        return frozenset()
    analyses = AnalysisStore(path).owned_by(owner_id, owner_workspace(owner_id))
    return frozenset(analysis.hold_id for analysis in analyses)


def _snapshot_blocker(state: _State, snapshot_id: str, own_holds: frozenset[str]) -> str | None:
    catalog = state.catalog
    assert catalog is not None  # noqa: S101 - the caller checked
    holds = [hold for hold in catalog.live_holds(snapshot_id) if hold.hold_id not in own_holds]
    if holds:
        kinds = ", ".join(sorted({hold.kind for hold in holds}))
        return (
            f"snapshot {snapshot_id} is held ({kinds}); release the hold "
            "(`warehouse-hold release`) or wait for it to lapse"
        )
    if catalog.live_lease_count(snapshot_id):
        return f"snapshot {snapshot_id} is being read by a query; run this again shortly"
    return None


def _step_warehouse(state: _State, owner_id: str, *, apply: bool) -> StepResult:
    from ..warehouse.errors import (
        ImmutableSnapshot,
        SnapshotHeld,
        SnapshotInUse,
        SnapshotNotFound,
        SnapshotStateError,
    )
    from ..warehouse.gc import delete_snapshot
    from ..warehouse.layout import SnapshotLayout, thaw
    from .ownership import owner_workspace

    catalog = state.catalog
    if catalog is None:
        return StepResult("warehouse")
    removed = 0
    blocked: list[str] = []
    # Applied, the analyses step has released these already; a dry run counts as if.
    own_holds = _own_holds(state, owner_id)
    own_tables = {table.id for table in catalog.list_tables(owner_workspace(owner_id))}
    for table_id in sorted(own_tables):
        snapshots = catalog.list_snapshots(table_id)
        reasons = [r for r in (_snapshot_blocker(state, s.id, own_holds) for s in snapshots) if r]
        if reasons:
            blocked.extend(reasons)
            continue
        if not apply:
            removed += len(snapshots)
            continue
        try:
            catalog.detach_current(table_id)
            for snapshot in snapshots:
                with suppress(SnapshotNotFound):
                    delete_snapshot(catalog, snapshot.id)
                removed += 1
            catalog.forget_table(table_id)
        except (SnapshotHeld, SnapshotInUse, ImmutableSnapshot, SnapshotStateError) as exc:
            # A lease or a hold that arrived after the check above.
            blocked.append(f"table {table_id}: {exc}")
            continue
        table_dir = SnapshotLayout(catalog.root, table_id).table_dir
        if table_dir.is_dir():
            thaw(table_dir)
            shutil.rmtree(table_dir, ignore_errors=True)
    # What the user built into a workspace that is not their own: a deployment that did
    # not enforce ownership when they built.
    for snapshot in catalog.snapshots_owned_by(owner_id):
        if snapshot.table_id in own_tables:
            continue
        if catalog.get_table(snapshot.table_id).current_snapshot_id == snapshot.id:
            blocked.append(
                f"snapshot {snapshot.id} is the current snapshot of table "
                f"{snapshot.table_id}, which other users share; replace it with another "
                "build of the table first"
            )
            continue
        reason = _snapshot_blocker(state, snapshot.id, own_holds)
        if reason is not None:
            blocked.append(reason)
            continue
        if apply:
            try:
                delete_snapshot(catalog, snapshot.id)
            except SnapshotNotFound:
                pass
            except (SnapshotHeld, SnapshotInUse, ImmutableSnapshot) as exc:
                blocked.append(f"snapshot {snapshot.id}: {exc}")
                continue
        removed += 1
    return StepResult("warehouse", removed=removed, blocked=tuple(blocked))


def _owned_runs(state: _State, owner_id: str) -> list[str]:
    run_ids: set[str] = set()
    if state.events is not None:
        run_ids.update(state.events.runs_submitted_by(owner_id))
    for root in (state.output_root, _gc_dir(state.output_root)):
        for run_dir in _workspaces(root):
            if _manifest_owner(run_dir)[1] == owner_id:
                run_ids.add(run_dir.name)
    return sorted(run_ids)


def _step_runs(state: _State, owner_id: str, *, apply: bool) -> StepResult:
    from ..retention import _close_index
    from ..stages._path_safety import validate_path_segment
    from ..store import make_build_index, rebuild_index

    run_ids = _owned_runs(state, owner_id)
    if not apply or not run_ids:
        return StepResult("runs", removed=len(run_ids))
    safe: list[str] = []
    blocked: list[str] = []
    for run_id in run_ids:
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError:
            blocked.append("a run of this user has a name that is not a run id; remove it by hand")
            continue
        safe.append(run_id)
    gc_dir = _gc_dir(state.output_root)
    index = make_build_index(state.output_root)
    try:
        for run_id in safe:
            workspace = state.output_root / run_id
            # As ``prune-runs`` deletes (#1099): out of the output directory in one rename,
            # then the index row, then the files. A crash leaves the run in ``.run-gc``,
            # which the next pass of either finishes.
            if workspace.is_dir() and not workspace.is_symlink():
                gc_dir.mkdir(exist_ok=True)
                os.rename(workspace, gc_dir / run_id)
            index.delete(run_id)
            if (gc_dir / run_id).exists():
                shutil.rmtree(gc_dir / run_id)
        stale = [run_id for run_id in safe if index.get(run_id) is not None]
    finally:
        _close_index(index)
    if stale:
        rebuild_index(state.output_root)
    # The events last: the submission is how a resumed deletion finds the run.
    if state.events is not None:
        state.events.forget_runs(safe)
    if gc_dir.is_dir() and not any(gc_dir.iterdir()):
        gc_dir.rmdir()
    return StepResult("runs", removed=len(safe), blocked=tuple(blocked))


def _step_uploads(state: _State, owner_id: str, *, apply: bool) -> StepResult:
    from ..uploads.store import SQLiteUploadRepository

    path = state.path("uploads.sqlite3")
    if path is None:
        return StepResult("uploads")
    repository = SQLiteUploadRepository(path)
    uploads = repository.list_for_owner(owner_id)
    if apply:
        for upload in uploads:
            repository.delete(owner_id, upload.upload_id)
    return StepResult("uploads", removed=len(uploads))


def _step_provider_keys(state: _State, owner_id: str, *, apply: bool) -> StepResult:
    from ..credentials.store import forget_owner, owner_key_count

    path = state.path("provider-credentials.sqlite3")
    if path is None:
        return StepResult("provider_keys")
    if apply:
        return StepResult("provider_keys", removed=forget_owner(path, owner_id))
    return StepResult("provider_keys", removed=owner_key_count(path, owner_id))


def _step_provider_tests(state: _State, owner_id: str, *, apply: bool) -> StepResult:
    from .provider_tests import ProviderTestLog

    path = state.path("provider_tests.sqlite3")
    if path is None:
        return StepResult("provider_tests")
    log = ProviderTestLog(path)
    if apply:
        return StepResult("provider_tests", removed=log.forget_owner(owner_id))
    return StepResult("provider_tests", removed=len(log.last_tests(owner_id)))


def _step_ledger(state: _State, owner_id: str, *, apply: bool) -> StepResult:
    from .user_ledger import UserLedger

    path = state.path("users.sqlite3")
    if path is None:
        return StepResult("ledger")
    ledger = UserLedger(path)
    if apply:
        return StepResult("ledger", removed=int(ledger.forget(owner_id)))
    return StepResult("ledger", removed=int(any(e.user_id == owner_id for e in ledger.list())))


def _residue(output_root: Path, warehouse_root: Path | None, owner_id: str) -> tuple[str, ...]:
    return tuple(
        store
        for store, owners in sorted(owners_in_state(output_root, warehouse_root).items())
        if owner_id in owners
    )


# -- running a deletion -------------------------------------------------------------------


def _check_target(owner_id: str) -> None:
    from .auth import _admin_identities, compute_owner_id

    if not owner_id.startswith("oidc:"):
        raise DeletionRefused(
            f"{owner_id!r} is not a user id: a user's id starts with 'oidc:' "
            "(`GET /admin/users` lists them)"
        )
    admins = {compute_owner_id("oidc", issuer, sub) for issuer, sub in _admin_identities()}
    if owner_id in admins:
        raise DeletionRefused(
            "this user is an administrator (KPUBDATA_BUILDER_ADMIN_SUBJECTS); remove them "
            "from the list and restart the server first"
        )


@contextmanager
def _exclusive(output_root: Path) -> Iterator[None]:
    directory = deletions_dir(output_root)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / _LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeletionRefused(
                "another deletion is running in this output directory; run this again when "
                "it has finished"
            ) from None
        yield
    finally:
        os.close(fd)


def _record(journal: _Journal, path: Path, result: StepResult) -> None:
    outcome = "blocked" if result.blocked else "done"
    entry: dict[str, JsonValue] = {
        "step": result.step,
        "at": _now(),
        "outcome": outcome,
        "removed": result.removed,
    }
    if result.blocked:
        entry["blocked"] = list(result.blocked)
    journal.history.append(entry)
    journal.write(path)
    record_user_deletion(journal.subject, result.step, outcome=outcome, removed=result.removed)


def _run_steps(
    state: _State,
    owner_id: str,
    *,
    apply: bool,
    wait_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    on_step: Callable[[StepResult], None],
) -> tuple[list[StepResult], bool]:
    """Run the data steps in order; stop at the first one something blocks."""
    results: list[StepResult] = []
    steps: Sequence[Callable[[], StepResult]] = (
        lambda: _step_jobs(
            state,
            owner_id,
            wait_seconds=wait_seconds if apply else 0.0,
            poll_seconds=poll_seconds,
            clock=clock,
            sleep=sleep,
        ),
        lambda: _step_analyses(state, owner_id, apply=apply),
        lambda: _step_exports(state, owner_id, apply=apply),
        lambda: _step_warehouse(state, owner_id, apply=apply),
        lambda: _step_runs(state, owner_id, apply=apply),
        lambda: _step_uploads(state, owner_id, apply=apply),
        lambda: _step_provider_keys(state, owner_id, apply=apply),
        lambda: _step_provider_tests(state, owner_id, apply=apply),
        lambda: _step_ledger(state, owner_id, apply=apply),
    )
    for step in steps:
        result = step()
        results.append(result)
        on_step(result)
        if result.blocked and apply:
            return results, False
    return results, True


def delete_user(
    output_root: Path,
    owner_id: str,
    *,
    warehouse_root: Path | None = None,
    apply: bool = False,
    wait_seconds: float = 0.0,
    poll_seconds: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DeletionReport:
    """Delete — or, by default, only count — ``owner_id``'s data (#1102).

    The caller holds the state lock on ``output_root`` and the warehouse, as every writer
    does; ``serve`` may run beside it, and is what cancels the user's builds.

    Args:
        output_root: The output directory.
        owner_id: The user's id, as ``GET /admin/users`` gives it.
        warehouse_root: The table catalog root, when there is a warehouse.
        apply: Delete. Without it nothing is written — not even the tombstone.
        wait_seconds: How long to wait for the user's runs to end before refusing.
        poll_seconds: How often to look while waiting.
        clock: Monotonic seconds (tests).
        sleep: Waits (tests).

    Raises:
        DeletionRefused: ``owner_id`` is not a user's, is an administrator's, or has
            nothing here and no deletion begun; or another deletion is running.
    """
    _check_target(owner_id)
    subject = deletion_subject(owner_id)
    path = _journal_path(output_root, subject)
    if not path.is_file() and not any(
        owner_id in owners for owners in owners_in_state(output_root, warehouse_root).values()
    ):
        raise DeletionRefused(
            "nothing here belongs to this user id, and no deletion of it has begun; check "
            "the id (`GET /admin/users`)"
        )
    if not apply:
        with _opened(output_root, warehouse_root) as state:
            results, _ = _run_steps(
                state,
                owner_id,
                apply=False,
                wait_seconds=0.0,
                poll_seconds=poll_seconds,
                clock=clock,
                sleep=sleep,
                on_step=lambda _result: None,
            )
        return DeletionReport(subject, applied=False, complete=False, steps=tuple(results))

    with _exclusive(output_root):
        if path.is_file():
            journal = _Journal.read(path)
            journal.completed_at = None
        else:
            journal = _Journal(subject=subject, requested_at=_now())
        # The tombstone: from this write on, the server refuses the user.
        tombstone = StepResult("tombstone")
        _record(journal, path, tombstone)
        results = [tombstone]
        complete = False
        # A second pass picks up what arrived during the first: a submission the server
        # recorded just before it saw the tombstone.
        for _attempt in range(2):
            with _opened(output_root, warehouse_root) as state:
                done, finished = _run_steps(
                    state,
                    owner_id,
                    apply=True,
                    wait_seconds=wait_seconds,
                    poll_seconds=poll_seconds,
                    clock=clock,
                    sleep=sleep,
                    on_step=lambda result: _record(journal, path, result),
                )
            results.extend(done)
            if not finished:
                break
            residue = _residue(output_root, warehouse_root, owner_id)
            verify = StepResult(
                "verify",
                blocked=tuple(f"this user's data is still in: {store}" for store in residue),
            )
            _record(journal, path, verify)
            results.append(verify)
            if not residue:
                complete = True
                break
        if complete:
            journal.completed_at = _now()
            journal.write(path)
    return DeletionReport(subject, applied=True, complete=complete, steps=tuple(results))


class OwnerDeletionProbe:
    """A build's cancellation probe that also stops it once its owner is deleted.

    ``deleted`` reads the tombstone; when it is there, ``on_deleted`` asks the run to
    stop — through the job registry for an async job, so that it reads ``cancelling``.
    ``commit`` asks first, so a build whose owner was deleted cannot end normally.
    """

    def __init__(
        self,
        inner: CancellationProbe,
        deleted: Callable[[], bool],
        on_deleted: Callable[[], object],
    ) -> None:
        self._inner = inner
        self._deleted = deleted
        self._on_deleted = on_deleted

    def cancel_requested(self) -> bool:
        if self._inner.cancel_requested():
            return True
        if self._deleted():
            self._on_deleted()
        return self._inner.cancel_requested()

    def commit(self) -> bool:
        self.cancel_requested()
        return self._inner.commit()


def _owners_by_subject(output_root: Path, warehouse_root: Path | None) -> dict[str, str]:
    by_subject: dict[str, str] = {}
    for owners in owners_in_state(output_root, warehouse_root).values():
        for owner in owners:
            by_subject.setdefault(deletion_subject(owner), owner)
    return by_subject


def reopen_found(output_root: Path, warehouse_root: Path | None) -> tuple[str, ...]:
    """Reopen every deletion whose user has data here again; returns their subjects.

    What a restore runs after it swapped an older state in: a user deleted since the
    backup was taken is in it.
    """
    found = _owners_by_subject(output_root, warehouse_root)
    reopened: list[str] = []
    for subject, path in journals(output_root).items():
        if subject not in found:
            continue
        journal = _Journal.read(path)
        journal.completed_at = None
        journal.history.append({"step": "reopened", "at": _now(), "outcome": "restored"})
        journal.write(path)
        record_user_deletion(subject, "reopened", outcome="restored", removed=0)
        reopened.append(subject)
    return tuple(reopened)


def resume_pending(
    output_root: Path,
    *,
    warehouse_root: Path | None = None,
    wait_seconds: float = 0.0,
) -> tuple[DeletionReport, ...]:
    """Run every deletion that has begun and not completed again.

    The user's id is not in the journal; it is found by matching the subject against
    the owners the stores name. A pending deletion whose user is in none of them has
    nothing left to delete, and is completed.
    """
    pending = pending_subjects(output_root)
    if not pending:
        return ()
    found = _owners_by_subject(output_root, warehouse_root)
    reports: list[DeletionReport] = []
    for subject in pending:
        owner_id = found.get(subject)
        if owner_id is not None:
            reports.append(
                delete_user(
                    output_root,
                    owner_id,
                    warehouse_root=warehouse_root,
                    apply=True,
                    wait_seconds=wait_seconds,
                )
            )
            continue
        with _exclusive(output_root):
            path = _journal_path(output_root, subject)
            journal = _Journal.read(path)
            verify = StepResult("verify")
            _record(journal, path, verify)
            journal.completed_at = _now()
            journal.write(path)
        reports.append(DeletionReport(subject, applied=True, complete=True, steps=(verify,)))
    return tuple(reports)


def carry_journals(source: Path, target: Path) -> int:
    """Copy ``source``'s deletion journals into the output directory ``target``.

    A restore runs this on the state it is about to swap in, so that what was deleted
    after the backup stays deleted. ``source``'s journal wins over one of the same
    subject in ``target``: it is the later one. Returns how many it copied.
    """
    copied = 0
    for subject, path in journals(source).items():
        destination = _journal_path(target, subject)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied += 1
    return copied


__all__ = [
    "DELETIONS_DIRNAME",
    "STEPS",
    "DeletionRefused",
    "DeletionReport",
    "OwnerDeletionProbe",
    "Step",
    "StepResult",
    "carry_journals",
    "deletion_subject",
    "deletions_dir",
    "delete_user",
    "is_deleted",
    "journals",
    "owners_in_state",
    "pending_subjects",
    "reopen_found",
    "resume_pending",
]
