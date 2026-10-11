"""The reconciliation dry run against a real CUBRID (ADR 0022).

ADR 0016's artifact store commits a manifest row first and mirrors it to a file second.
These tests make the second step fail the way it can in production and check that the
dry run names every run whose newest manifest exists only in CUBRID, and writes nothing.

Like the backend contract tests, the engine is a real CUBRID when
KPUBDATA_BUILDER_CUBRID_URL is set (the CUBRID workflow) and an in-memory SQLite engine
otherwise; KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID forbids the fallback. The database may
be shared, so every assertion is about this module's own run ids.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Engine, create_engine, func, select, table  # noqa: E402

from kpubdata_builder.credentials.crypto import AesGcmCredentialCipher  # noqa: E402
from kpubdata_builder.credentials.store_cubrid import CubridCredentialRepository  # noqa: E402
from kpubdata_builder.store import backend, cubrid_reconcile  # noqa: E402
from kpubdata_builder.store.artifacts.cubrid import CubridArtifactStore  # noqa: E402
from kpubdata_builder.store.artifacts.local import LocalArtifactStore  # noqa: E402

pytestmark = pytest.mark.cubrid


@pytest.fixture
def engine() -> Iterator[Engine]:
    url = os.environ.get("KPUBDATA_BUILDER_CUBRID_URL")
    real_required = os.environ.get("KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID", "").strip().lower()
    if not url and real_required in ("1", "true", "yes"):
        raise AssertionError("KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID is set but no URL is")
    eng = create_engine(url or "sqlite:///:memory:", pool_pre_ping=True, future=True)
    yield eng
    eng.dispose()


def _fail_mirror(store: CubridArtifactStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(run_id: str, manifest: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store._local, "put_manifest", refuse)


def _row_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table("manifests"))).scalar_one())


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_dry_run_names_the_runs_only_cubrid_holds(
    tmp_path: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = uuid.uuid4().hex[:10]
    same, stale, lost, garbled = (f"rc-{tag}-{name}" for name in ("same", "stale", "lost", "bad"))
    file_only = f"rc-{tag}-fileonly"
    store = CubridArtifactStore(tmp_path, engine)

    store.put_manifest(same, {"build_id": same, "status": "ok"})
    store.put_manifest(stale, {"build_id": stale, "status": "running"})
    store.put_manifest(garbled, {"build_id": garbled, "status": "ok"})
    (tmp_path / garbled / "manifest.json").write_text("{not json", encoding="utf-8")
    LocalArtifactStore(tmp_path).put_manifest(file_only, {"build_id": file_only})

    # The row commits, the mirror write fails: the newest manifest is in CUBRID alone.
    with monkeypatch.context() as patch:
        _fail_mirror(store, patch)
        for run_id, status in ((stale, "ok"), (lost, "failed")):
            with pytest.raises(OSError):
                store.put_manifest(run_id, {"build_id": run_id, "status": status})

    rows_before = _row_count(engine)
    files_before = _snapshot(tmp_path)

    diff = cubrid_reconcile.reconcile(engine, tmp_path)

    mine = {same, stale, lost, garbled}
    assert [r for r in diff.missing_on_disk if r in mine] == [lost]
    assert [r for r in diff.differs if r in mine] == [stale]
    assert [r for r in diff.unreadable_on_disk if r in mine] == [garbled]
    assert diff.disk_only == 1
    assert diff.needs_restore
    # A dry run: neither side changed.
    assert _row_count(engine) == rows_before
    assert _snapshot(tmp_path) == files_before


def test_after_restore_the_dry_run_finds_nothing_to_do(
    tmp_path: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = f"rc-{uuid.uuid4().hex[:10]}-restored"
    store = CubridArtifactStore(tmp_path, engine)
    with monkeypatch.context() as patch:
        _fail_mirror(store, patch)
        with pytest.raises(OSError):
            store.put_manifest(run_id, {"build_id": run_id, "status": "ok"})
    assert run_id in cubrid_reconcile.reconcile(engine, tmp_path).missing_on_disk

    # The restore ADR 0022 describes: write the row's document as the file.
    stored = store.get_manifest(run_id)
    assert stored is not None
    LocalArtifactStore(tmp_path).put_manifest(run_id, stored)

    diff = cubrid_reconcile.reconcile(engine, tmp_path)
    assert run_id not in diff.missing_on_disk + diff.differs + diff.unreadable_on_disk
    assert LocalArtifactStore(tmp_path).get_manifest(run_id) == stored


def test_saved_keys_are_listed_by_owner_and_provider_without_the_secret(
    tmp_path: Path, engine: Engine
) -> None:
    owner = f"rc-owner-{uuid.uuid4().hex[:10]}"
    secret = "do-not-print-this-key"
    repo = CubridCredentialRepository(engine, AesGcmCredentialCipher(os.urandom(32)))
    repo.put(owner, "datago", secret)

    diff = cubrid_reconcile.reconcile(engine, tmp_path)

    assert f"{owner}/datago" in diff.credentials
    assert secret not in json.dumps(diff.as_dict())


def test_main_reports_json_and_exits_one_when_a_run_needs_restoring(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = f"rc-{uuid.uuid4().hex[:10]}-main"
    store = CubridArtifactStore(tmp_path, engine)
    with monkeypatch.context() as patch:
        _fail_mirror(store, patch)
        with pytest.raises(OSError):
            store.put_manifest(run_id, {"build_id": run_id})
    monkeypatch.setattr(backend, "get_engine", lambda: engine)

    code = cubrid_reconcile.main(["--output-dir", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert run_id in report["missing_on_disk"]
    assert report["needs_restore"] is True
