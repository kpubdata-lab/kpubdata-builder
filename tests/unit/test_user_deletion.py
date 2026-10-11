"""Deleting a user and their data, without bringing them back (#1102).

Each guarantee the deletion makes has a test here:

- a dry run changes nothing, not even the tombstone;
- applied, every store loses the user and keeps every other user;
- the server refuses a deleted user and does not record them anew;
- a build of the user that has not ended blocks the deletion, which says so; a queued
  one is cancelled before it starts, and a running one at its next boundary;
- builds submitted while the deletion runs leave nothing of the user behind;
- a hold or a lease on a snapshot stops the warehouse step, which says what blocks it;
- a deletion that dies midway is finished from its journal, without the user's id;
- the audit record — journal and log — names the subject, never the user;
- a restore of a backup taken before the deletion does not bring the user back.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from kpubdata_builder.cli import main
from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.events import BuildEvent
from kpubdata_builder.events.store import BuildEventStore
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch, user_deletion
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.analyses_api import AnalysisStore, SavedAnalysis
from kpubdata_builder.service.auth import Principal, compute_owner_id
from kpubdata_builder.service.jobs import RunCancellation
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV, owner_workspace
from kpubdata_builder.service.provider_tests import ProviderTestLog
from kpubdata_builder.service.providers import ProviderTestResult
from kpubdata_builder.service.user_deletion import (
    DeletionRefused,
    OwnerDeletionProbe,
    delete_user,
    deletion_subject,
    is_deleted,
    owners_in_state,
    pending_subjects,
    resume_pending,
)
from kpubdata_builder.service.user_ledger import UserLedger
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store import state_backup
from kpubdata_builder.uploads.store import SQLiteUploadRepository
from kpubdata_builder.warehouse import TableCatalog

_ISSUER = "https://idp.example"
_ALICE = compute_owner_id("oidc", _ISSUER, "alice")
_BOB = compute_owner_id("oidc", _ISSUER, "bob")
_SPEC = (
    "dataset_id: dataset.delete\n"
    "title: Delete Sample\n"
    "description: Deletion fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "    alias: air\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: out/data.jsonl\n"
)


def _principal(owner_id: str) -> Principal:
    return Principal(kind="oidc", identifier=owner_id[5:13], owner_id=owner_id, admitted=True)


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items: Iterable[dict[str, JsonValue]] = items


class _Dataset:
    def list(self, **_params: JsonValue) -> _Result:
        return _Result([{"id": "1", "v": 10}, {"id": "2", "v": 20}])


class _Client:
    def dataset(self, _source_key: str) -> _Dataset:
        return _Dataset()


def _service(root: Path, **kwargs: Any) -> BuilderService:
    kwargs.setdefault("async_max_workers", 1)
    return BuilderService(output_root=root, client_factory=lambda **_: _Client(), **kwargs)


def _wait_for_end(service: BuilderService, run_id: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service._async_builds.get(run_id)
        if job is not None and job.status in ("succeeded", "failed", "cancelled"):
            return job.status
        time.sleep(0.02)
    raise AssertionError(f"{run_id} did not end")


def _build(service: BuilderService, owner_id: str) -> str:
    response = service.submit_build(_SPEC, created_by=f"oidc:{owner_id[5:13]}", owner_id=owner_id)
    assert response.status_code == 202, response.body
    run_id = str(response.body["run_id"])
    assert _wait_for_end(service, run_id) == "succeeded"
    return run_id


def _populate(root: Path, warehouse: Path, owner_id: str, run_id: str) -> None:
    """Everything a user can leave, besides runs: one of each."""
    service_dir = root / ".service"
    SQLiteUploadRepository(service_dir / "uploads.sqlite3").put(
        owner_id, content=b"a,b\n1,2\n", format="csv", encoding="utf-8", original_filename="f.csv"
    )
    SQLiteCredentialRepository(
        service_dir / "provider-credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
    ).put(owner_id, "datago", "secret-key")
    ProviderTestLog(service_dir / "provider_tests.sqlite3").record(
        owner_id,
        ProviderTestResult(
            provider="datago",
            status="connected",
            configured=True,
            latency_ms=1,
            checked_at="2026-10-12T00:00:00+00:00",
        ),
    )
    UserLedger(service_dir / "users.sqlite3").observe(
        Principal(
            kind="oidc",
            identifier=owner_id[5:13],
            owner_id=owner_id,
            display_name=f"{owner_id[5:13]}@example.com",
        )
    )
    catalog = TableCatalog(warehouse)
    try:
        # The table the user's build committed, in the user's own workspace.
        (table,) = catalog.list_tables(owner_workspace(owner_id))
        snapshot_id = table.current_snapshot_id
        assert snapshot_id is not None
        assert catalog.get_snapshot(snapshot_id).run_id == run_id
        hold = catalog.place_hold(snapshot_id, kind="saved_analysis", reason="analysis")
        AnalysisStore(service_dir / "analyses.sqlite3").put(
            SavedAnalysis(
                analysis_id=f"an_{owner_id[-8:]}",
                workspace_id=owner_workspace(owner_id),
                owner_id=owner_id,
                name="n",
                sql="SELECT 1",
                limit=10,
                table="t",
                snapshot_id=snapshot_id,
                hold_id=hold.hold_id,
                result_meta={},
                created_at="2026-10-12T00:00:00+00:00",
            )
        )
    finally:
        catalog.close()
    export_dir = service_dir / "exports" / f"exp_{owner_id[-32:]}"
    export_dir.mkdir(parents=True)
    (export_dir / "export.json").write_text(
        json.dumps(
            {
                "export_id": export_dir.name,
                "owner_id": owner_id,
                "workspace_id": owner_workspace(owner_id),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path, BuilderService]]:
    """Two users with one of everything each; Bob must survive Alice's deletion."""
    # A multi-user deployment: each owner builds into a workspace of their own.
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    root = tmp_path / "data"
    warehouse = root / "warehouse"
    root.mkdir()
    service = _service(root, warehouse_root=warehouse)
    try:
        alice_run = _build(service, _ALICE)
        bob_run = _build(service, _BOB)
        _populate(root, warehouse, _ALICE, alice_run)
        _populate(root, warehouse, _BOB, bob_run)
        yield root, warehouse, service
    finally:
        service._async_builds.shutdown()


def _where(root: Path, warehouse: Path, owner_id: str) -> set[str]:
    return {
        store for store, owners in owners_in_state(root, warehouse).items() if owner_id in owners
    }


_EVERYWHERE = {
    "runs",
    "ledger",
    "uploads",
    "analyses",
    "provider_keys",
    "provider_tests",
    "exports",
    "warehouse",
}


# ------------------------------------------------------------------- dry run and apply


def test_a_dry_run_counts_and_changes_nothing(state: tuple[Path, Path, BuilderService]) -> None:
    root, warehouse, _service_ = state

    report = delete_user(root, _ALICE, warehouse_root=warehouse)

    assert not report.applied and not report.complete
    removed = {step.step: step.removed for step in report.steps}
    assert removed == {
        "jobs": 0,
        "analyses": 1,
        "exports": 1,
        "warehouse": 1,
        "runs": 1,
        "uploads": 1,
        "provider_keys": 1,
        "provider_tests": 1,
        "ledger": 1,
    }
    assert not is_deleted(root, _ALICE)
    assert _where(root, warehouse, _ALICE) == _EVERYWHERE


def test_applied_it_removes_the_user_everywhere_and_no_one_else(
    state: tuple[Path, Path, BuilderService],
) -> None:
    root, warehouse, _service_ = state
    bob_runs = sorted(p.name for p in root.iterdir() if not p.name.startswith((".", "_")))

    report = delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)

    assert report.complete, report.blocked
    assert _where(root, warehouse, _ALICE) == set()
    assert _where(root, warehouse, _BOB) == _EVERYWHERE
    catalog = TableCatalog(warehouse)
    try:
        assert catalog.list_tables(owner_workspace(_ALICE)) == []
        assert len(catalog.list_tables(owner_workspace(_BOB))) == 1
    finally:
        catalog.close()
    events = BuildEventStore(root)
    try:
        assert events.runs_submitted_by(_ALICE) == ()
        assert len(events.runs_submitted_by(_BOB)) == 1
    finally:
        events.close()
    left = sorted(p.name for p in root.iterdir() if not p.name.startswith((".", "_")))
    assert len(left) == len(bob_runs) - 1
    assert pending_subjects(root) == ()

    # Run again: every step is idempotent, and the deletion is still complete.
    again = delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)
    assert again.complete
    assert all(step.removed == 0 for step in again.steps)


# --------------------------------------------------------------------- the server side


def _as(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, owner_id: str, path: str
) -> ServiceResponse:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _principal(owner_id))
    response = dispatch(service, "GET", path, None, "")
    assert isinstance(response, ServiceResponse)
    return response


def test_a_deleted_user_is_refused_and_not_signed_up_again(
    state: tuple[Path, Path, BuilderService], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: the next request after the deletion is refused, and records nothing."""
    root, warehouse, service = state
    assert delete_user(root, _ALICE, warehouse_root=warehouse, apply=True).complete

    refused = _as(service, monkeypatch, _ALICE, "/builds")
    other = _as(service, monkeypatch, _BOB, "/builds")

    assert (refused.status_code, refused.body.get("code")) == (403, "signup_rejected")
    assert other.status_code == 200
    users = {entry.user_id for entry in UserLedger(root / ".service" / "users.sqlite3").list()}
    assert _ALICE not in users and _BOB in users
    # A build submitted directly — past the route's gate — is refused too.
    submitted = service.submit_build(_SPEC, owner_id=_ALICE)
    assert (submitted.status_code, submitted.body.get("code")) == (403, "signup_rejected")


def test_the_probe_stops_a_build_once_its_owner_is_deleted() -> None:
    deleted = threading.Event()
    inner = RunCancellation()
    probe = OwnerDeletionProbe(inner, deleted.is_set, inner.request)

    assert probe.cancel_requested() is False
    deleted.set()
    assert probe.cancel_requested() is True
    # The build cannot end normally once its owner is deleted.
    assert probe.commit() is False


def test_a_build_that_committed_before_the_deletion_ends_normally() -> None:
    deleted = threading.Event()
    inner = RunCancellation()
    probe = OwnerDeletionProbe(inner, deleted.is_set, inner.request)

    assert probe.commit() is True
    deleted.set()
    assert probe.cancel_requested() is False


def _unfinished(root: Path, run_id: str, owner_id: str | None) -> BuildEventStore:
    store = BuildEventStore(root)
    store.append(
        BuildEvent(
            seq=0,
            timestamp=datetime.now(timezone.utc),
            run_id=run_id,
            event=cast(Any, "run_submitted"),
            status="ok",
            message="submitted",
        )
    )
    if owner_id is not None:
        store.record_submission(
            run_id,
            owner_id=owner_id,
            created_by=None,
            submitted_at=datetime.now(timezone.utc),
        )
    return store


def test_a_build_that_has_not_ended_blocks_the_deletion_and_is_named(
    state: tuple[Path, Path, BuilderService],
) -> None:
    """Negative: nothing is deleted under a running build; the user is shut out at once."""
    root, warehouse, _service_ = state
    store = _unfinished(root, "run-still-going", _ALICE)

    report = delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)

    assert not report.complete
    assert report.blocked == ("run run-still-going of this user has not ended",)
    assert is_deleted(root, _ALICE)
    assert _where(root, warehouse, _ALICE) == _EVERYWHERE
    assert pending_subjects(root) == (deletion_subject(_ALICE),)

    store.append(
        BuildEvent(
            seq=0,
            timestamp=datetime.now(timezone.utc),
            run_id="run-still-going",
            event=cast(Any, "run_cancelled"),
            status="ok",
            message="cancelled",
        )
    )
    store.close()
    assert delete_user(root, _ALICE, warehouse_root=warehouse, apply=True).complete
    assert _where(root, warehouse, _ALICE) == set()


def test_a_build_whose_owner_is_not_known_yet_blocks_too(
    state: tuple[Path, Path, BuilderService],
) -> None:
    """A synchronous build names its owner only when it ends: it may be the user's."""
    root, warehouse, _service_ = state
    _unfinished(root, "sync-run", None).close()

    report = delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)

    assert not report.complete
    assert "sync-run" in report.blocked[0]


class _GatedService(BuilderService):
    """Holds Bob's builds at a gate, so that Alice's job waits in the queue."""

    def __init__(self, gate: threading.Event, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gate = gate
        self.built_for: list[str | None] = []

    def build(self, spec_yaml: str, **kwargs: Any) -> ServiceResponse:
        self.built_for.append(kwargs.get("owner_id"))
        if kwargs.get("owner_id") == _BOB:
            assert self.gate.wait(timeout=30)
        return super().build(spec_yaml, **kwargs)


def test_a_queued_job_of_a_deleted_user_never_starts(tmp_path: Path) -> None:
    gate = threading.Event()
    service = _GatedService(
        gate, output_root=tmp_path, client_factory=lambda **_: _Client(), async_max_workers=1
    )
    try:
        bob = service.submit_build(_SPEC, owner_id=_BOB)
        alice = service.submit_build(_SPEC, owner_id=_ALICE)
        alice_run = str(alice.body["run_id"])
        queued = service._async_builds.get(alice_run)
        assert queued is not None and queued.status == "queued"

        blocked = delete_user(tmp_path, _ALICE, apply=True)
        assert blocked.blocked == (f"run {alice_run} of this user has not ended",)

        gate.set()
        assert _wait_for_end(service, str(bob.body["run_id"])) == "succeeded"
        assert _wait_for_end(service, alice_run) == "cancelled"
        assert _ALICE not in service.built_for

        report = delete_user(tmp_path, _ALICE, apply=True, wait_seconds=10)
        assert report.complete, report.blocked
        assert _where(tmp_path, tmp_path / "warehouse", _ALICE) == set()
    finally:
        gate.set()
        service._async_builds.shutdown()


def test_builds_submitted_while_the_user_is_deleted_leave_nothing(tmp_path: Path) -> None:
    """Submissions racing the deletion: each is either refused or deleted with the user."""
    service = _service(tmp_path, async_max_workers=2)
    outcomes: list[int] = []
    stop = threading.Event()

    def submit() -> None:
        while not stop.is_set():
            outcomes.append(service.submit_build(_SPEC, owner_id=_ALICE).status_code)
            time.sleep(0.005)

    try:
        _build(service, _ALICE)
        submitter = threading.Thread(target=submit)
        submitter.start()
        time.sleep(0.1)
        try:
            report = delete_user(tmp_path, _ALICE, apply=True, wait_seconds=30, poll_seconds=0.05)
            refused_after = service.submit_build(_SPEC, owner_id=_ALICE).status_code
        finally:
            stop.set()
            submitter.join(timeout=30)
        for job in service._async_builds.list_all():
            _wait_for_end(service, job.run_id)
    finally:
        service._async_builds.shutdown()

    assert report.complete, report.blocked
    assert refused_after == 403
    # Submissions got in before the deletion began, and were refused after it.
    assert 202 in outcomes and 403 in outcomes
    assert set(outcomes) <= {202, 403, 429}
    assert _where(tmp_path, tmp_path / "warehouse", _ALICE) == set()


# ------------------------------------------------------------------------ the warehouse


def test_a_held_snapshot_stops_the_warehouse_step_and_says_why(
    state: tuple[Path, Path, BuilderService],
) -> None:
    root, warehouse, _service_ = state
    catalog = TableCatalog(warehouse)
    try:
        table = catalog.list_tables(owner_workspace(_ALICE))[0]
        assert table.current_snapshot_id is not None
        hold = catalog.place_hold(table.current_snapshot_id, kind="audit", reason="evidence")
    finally:
        catalog.close()

    report = delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)

    assert not report.complete
    assert len(report.blocked) == 1
    assert "is held (audit)" in report.blocked[0]
    # The step stopped there: the run the snapshot was made from is still there.
    assert "runs" in _where(root, warehouse, _ALICE)

    catalog = TableCatalog(warehouse)
    try:
        catalog.release_hold(hold.hold_id)
    finally:
        catalog.close()
    assert delete_user(root, _ALICE, warehouse_root=warehouse, apply=True).complete


def test_a_leased_snapshot_stops_the_warehouse_step(
    state: tuple[Path, Path, BuilderService],
) -> None:
    root, warehouse, _service_ = state
    catalog = TableCatalog(warehouse)
    try:
        table = catalog.list_tables(owner_workspace(_ALICE))[0]
        lease = catalog.resolve_current(table.id)
        report = delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)
        assert not report.complete
        assert "being read by a query" in report.blocked[0]
        catalog.release(lease.lease_id)
    finally:
        catalog.close()
    assert delete_user(root, _ALICE, warehouse_root=warehouse, apply=True).complete


# --------------------------------------------------------------------- crash and resume


def test_a_deletion_that_died_midway_is_finished_without_the_id(
    state: tuple[Path, Path, BuilderService], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, warehouse, _service_ = state

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("the process died here")

    monkeypatch.setattr(user_deletion, "_step_uploads", crash)
    with pytest.raises(OSError):
        delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)
    monkeypatch.undo()

    subject = deletion_subject(_ALICE)
    assert pending_subjects(root) == (subject,)
    # The steps before the crash happened; the ones after it did not.
    assert _where(root, warehouse, _ALICE) == {
        "uploads",
        "provider_keys",
        "provider_tests",
        "ledger",
    }
    journal = json.loads((root / ".service" / "deleted-users" / f"{subject}.json").read_text())
    assert [entry["step"] for entry in journal["history"]][-1] == "runs"

    reports = resume_pending(root, warehouse_root=warehouse)

    assert [report.complete for report in reports] == [True]
    assert pending_subjects(root) == ()
    assert _where(root, warehouse, _ALICE) == set()


# ------------------------------------------------------------------------- the audit


def test_the_audit_record_names_the_subject_and_never_the_user(
    state: tuple[Path, Path, BuilderService], caplog: pytest.LogCaptureFixture
) -> None:
    root, warehouse, _service_ = state
    caplog.set_level(logging.INFO, logger="kpubdata_builder.admin_audit")

    delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)

    subject = deletion_subject(_ALICE)
    journal_text = (root / ".service" / "deleted-users" / f"{subject}.json").read_text()
    journal = json.loads(journal_text)
    assert journal["subject"] == subject and journal["completed_at"] is not None
    assert [entry["step"] for entry in journal["history"]] == list(user_deletion.STEPS)
    lines = [r.getMessage() for r in caplog.records if r.name == "kpubdata_builder.admin_audit"]
    assert len(lines) == len(user_deletion.STEPS)
    for text in (journal_text, *lines):
        assert _ALICE not in text
        assert _ALICE.split(":", 1)[1] not in text
        assert "alice" not in text
    assert all(f"subject={subject}" in line for line in lines)


# ------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("owner_id", "message"),
    [
        (compute_owner_id("service", "default"), "not a user id"),
        (compute_owner_id("oidc", _ISSUER, "nobody"), "nothing here belongs"),
    ],
)
def test_an_id_that_is_not_a_user_here_is_refused(
    state: tuple[Path, Path, BuilderService], owner_id: str, message: str
) -> None:
    root, warehouse, _service_ = state

    with pytest.raises(DeletionRefused, match=message):
        delete_user(root, owner_id, warehouse_root=warehouse, apply=True)
    assert user_deletion.journals(root) == {}


def test_an_administrator_is_refused(
    state: tuple[Path, Path, BuilderService], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, warehouse, _service_ = state
    monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", f"{_ISSUER}|alice")

    with pytest.raises(DeletionRefused, match="administrator"):
        delete_user(root, _ALICE, warehouse_root=warehouse, apply=True)


# ------------------------------------------------------------------------- restore


def test_a_restore_does_not_bring_a_deleted_user_back(
    state: tuple[Path, Path, BuilderService], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, warehouse, service = state
    service._async_builds.shutdown()
    archive = tmp_path / "state.tar"
    state_backup.backup(root, archive, warehouse=warehouse)
    assert delete_user(root, _ALICE, warehouse_root=warehouse, apply=True).complete

    result = state_backup.restore(archive, root)

    assert result.deletions_reapplied == (deletion_subject(_ALICE),)
    assert result.deletions_pending == ()
    assert _where(root, warehouse, _ALICE) == set()
    assert _where(root, warehouse, _BOB) == _EVERYWHERE
    assert is_deleted(root, _ALICE)
    restored = _service(root, warehouse_root=warehouse)
    try:
        refused = _as(restored, monkeypatch, _ALICE, "/builds")
        assert (refused.status_code, refused.body.get("code")) == (403, "signup_rejected")
    finally:
        restored._async_builds.shutdown()


def test_a_restore_with_no_deletion_changes_nothing_of_the_users(
    state: tuple[Path, Path, BuilderService], tmp_path: Path
) -> None:
    root, warehouse, service = state
    service._async_builds.shutdown()
    archive = tmp_path / "state.tar"
    state_backup.backup(root, archive, warehouse=warehouse)

    result = state_backup.restore(archive, root)

    assert result.deletions_reapplied == ()
    assert _where(root, warehouse, _ALICE) == _EVERYWHERE


# ------------------------------------------------------------------------------ CLI


def test_the_command_dry_runs_applies_and_resumes(
    state: tuple[Path, Path, BuilderService], capsys: pytest.CaptureFixture[str]
) -> None:
    root, warehouse, _service_ = state
    common = ["--output-dir", str(root), "--warehouse", str(warehouse)]

    assert main(["delete-user", _ALICE, *common]) == 0
    dry = capsys.readouterr().out
    assert "dry run" in dry and "would delete: uploads (1)" in dry
    assert not is_deleted(root, _ALICE)

    assert main(["delete-user", _ALICE, *common, "--apply"]) == 0
    applied = capsys.readouterr().out
    assert f"deletion {deletion_subject(_ALICE)} is complete" in applied
    assert _ALICE not in applied

    assert main(["delete-user", "--resume", *common]) == 0
    assert "no unfinished deletion" in capsys.readouterr().out


def test_the_command_wants_an_id_or_resume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["delete-user", "--output-dir", str(tmp_path)]) == 2
    assert main(["delete-user", _ALICE, "--resume", "--output-dir", str(tmp_path)]) == 2
    assert "either USER_ID or --resume" in capsys.readouterr().err


def test_the_command_says_what_blocks_and_exits_1(
    state: tuple[Path, Path, BuilderService], capsys: pytest.CaptureFixture[str]
) -> None:
    root, warehouse, _service_ = state
    _unfinished(root, "run-still-going", _ALICE).close()

    code = main(
        ["delete-user", _ALICE, "--output-dir", str(root), "--warehouse", str(warehouse), "--apply"]
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "blocked: jobs: run run-still-going of this user has not ended" in out
    assert "is not complete; the user stays shut out" in out
