"""CUBRID backend contract/integration tests (ADR 0016).

Excluded from the base suite (`-m 'not cubrid'`). Run via: ``pytest -m cubrid``
(requires sqlalchemy).

engine fixture connects to **real CUBRID** when KPUBDATA_BUILDER_CUBRID_URL is
set; otherwise validates SQLAlchemy Core logic with in-memory SQLite engine
(dialect-independent). Real CUBRID integration runs in a dedicated CI job
(.github/workflows/cubrid.yml) that injects the URL.

This fallback has a trap (#587): if URL injection is omitted in the dedicated
CI job, the fixture quietly falls back to SQLite, **passing green without
touching CUBRID dialect code at all**. Setting KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID=1
forbids the fallback — the CI job enables this flag, so missing URLs cause
failure instead of silent passing.

Tests assert only their own keys (unique run_id/owner_id) and are safe even
on a shared CUBRID.
"""

from __future__ import annotations

import base64
import os

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine  # noqa: E402

from kpubdata_builder.credentials.crypto import AesGcmCredentialCipher  # noqa: E402
from kpubdata_builder.credentials.store_cubrid import CubridCredentialRepository  # noqa: E402
from kpubdata_builder.store.build_index import BuildEntry  # noqa: E402
from kpubdata_builder.store.build_index_cubrid import CubridBuildIndex  # noqa: E402

pytestmark = pytest.mark.cubrid


_REQUIRE_REAL_ENV = "KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID"


def _require_real_cubrid() -> bool:
    """Whether running in the dedicated CI job. If true, forbids SQLite fallback (#587)."""
    return os.environ.get(_REQUIRE_REAL_ENV, "").strip().lower() in ("1", "true", "yes")


@pytest.fixture
def engine():  # type: ignore[no-untyped-def]
    url = os.environ.get("KPUBDATA_BUILDER_CUBRID_URL")
    if not url and _require_real_cubrid():
        raise AssertionError(
            f"{_REQUIRE_REAL_ENV} is set but KPUBDATA_BUILDER_CUBRID_URL is empty — this run "
            "would silently fall back to in-memory SQLite and prove nothing about the CUBRID "
            "dialect. Set the URL or unset the guard."
        )
    if url:
        eng = create_engine(url, pool_pre_ping=True, future=True)
    else:
        eng = create_engine("sqlite:///:memory:", future=True)
    yield eng
    eng.dispose()


def test_ci_job_runs_against_a_real_cubrid_engine(engine) -> None:  # type: ignore[no-untyped-def]
    """Verify that the dedicated CI job runs on actual CUBRID dialect (#587).

    Locally (guard unset), SQLite fallback is expected, so skip.
    """
    if not _require_real_cubrid():
        pytest.skip(f"{_REQUIRE_REAL_ENV} not set — SQLite fallback is expected locally")
    assert engine.dialect.name == "cubrid", (
        f"expected the cubrid dialect, got {engine.dialect.name!r} — the contract tests are "
        "not exercising CUBRID"
    )
    # Verify driver too: bare cubrid:// resolves to legacy C-extension (ADR 0016).
    assert engine.dialect.driver == "pycubrid", (
        f"expected the pycubrid driver, got {engine.dialect.driver!r}"
    )


def test_build_index_crud_and_ordering(engine) -> None:  # type: ignore[no-untyped-def]
    idx = CubridBuildIndex(engine)
    idx.insert_or_replace(
        "cbx-1",
        "ok",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:01:00Z",
        spec_digest="d1",
        created_by="dev:local",
        dataset_id="cbx-ds",
        owner_id="oidc:a",
    )
    got = idx.get("cbx-1")
    assert got is not None and got.status == "ok" and got.dataset_id == "cbx-ds"

    # Full row replace (sqlite INSERT OR REPLACE semantics)
    idx.insert_or_replace("cbx-1", "failed", None, "2026-01-01T00:02:00Z", error="boom")
    got = idx.get("cbx-1")
    assert got is not None and got.status == "failed" and got.error == "boom"
    assert got.dataset_id is None  # Unset fields are cleared on replace

    idx.insert_or_replace("cbx-2", "ok", None, "2026-01-02T00:00:00Z", dataset_id="cbx-ds")
    by_ds = {e.run_id for e in idx.list_by_dataset("cbx-ds")}
    assert by_ds == {"cbx-2"}  # cbx-1 lost dataset_id from replace

    idx.delete("cbx-1")
    idx.delete("cbx-2")
    assert idx.get("cbx-1") is None and idx.get("cbx-2") is None


def test_build_index_rebuild(engine) -> None:  # type: ignore[no-untyped-def]
    idx = CubridBuildIndex(engine)
    n = idx.rebuild(
        [
            BuildEntry("cbx-r1", "ok", "s", "f1", "dg", None, "dev:local", "cbx-ds2", "oidc:x"),
            BuildEntry("cbx-r2", "failed", "s", "f2", None, "err", "dev:local", None, None),
        ]
    )
    assert n == 2
    got = idx.get("cbx-r1")
    assert got is not None and got.dataset_id == "cbx-ds2"
    # rebuild truncates, so previous runs are gone (rebuilt from canonical manifest).
    assert idx.get("cbx-1") is None


def test_build_index_monitoring_queries(engine) -> None:  # type: ignore[no-untyped-def]
    """Validate that upstream monitoring (#516/#527) methods work on real CUBRID."""
    idx = CubridBuildIndex(engine)
    idx.rebuild(
        [
            BuildEntry(
                "cbm-1", "ok", "s", "2026-01-01T00:00:00Z", "d", None, "dev:local", "ds", "oidc:me"
            ),
            BuildEntry(
                "cbm-2",
                "failed",
                "s",
                "2026-01-02T00:00:00Z",
                None,
                "e",
                "dev:local",
                "ds",
                "oidc:other",
            ),
            BuildEntry("cbm-3", "ok", "s", "2026-01-03T00:00:00Z", "d", None, "svc", "ds", None),
        ]
    )
    # list_between: [start, end) ascending, exclude >= end
    between = idx.list_between("2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z")
    assert [e.run_id for e in between] == ["cbm-1", "cbm-2"]
    # latest_successful_finished_at: most recent among successful (ok)
    assert idx.latest_successful_finished_at() == "2026-01-03T00:00:00Z"
    # list_recent_owned: match by owner_id
    owned = idx.list_recent_owned(limit=10, principal_owner_id="oidc:me", principal_label="oidc:x")
    assert {e.run_id for e in owned} == {"cbm-1"}
    # principal owner_id absent, fallback to created_by
    owned2 = idx.list_recent_owned(limit=10, principal_owner_id=None, principal_label="svc")
    assert {e.run_id for e in owned2} == {"cbm-3"}


def test_build_index_write_failure_is_swallowed(engine) -> None:  # type: ignore[no-untyped-def]
    """ADR 0003 rule 4: index write failures must not propagate as exceptions."""
    idx = CubridBuildIndex(engine)

    class _BoomEngine:
        def begin(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("cubrid unavailable")

    idx._engine = _BoomEngine()  # type: ignore[assignment]
    # Must be silently swallowed without exception.
    idx.insert_or_replace("cbx-x", "ok", None, None)
    idx.delete("cbx-x")


def test_credential_roundtrip_and_aad_binding(engine) -> None:  # type: ignore[no-untyped-def]
    cipher = AesGcmCredentialCipher.from_base64(base64.b64encode(os.urandom(32)).decode())
    repo = CubridCredentialRepository(engine, cipher)
    owner = "oidc:cbx-owner"

    assert repo.get_metadata(owner, "datago").configured is False
    m = repo.put(owner, "DataGo", "secret-key")
    assert m.configured and m.masked == "********" and m.provider == "datago"
    assert repo.get_secret(owner, "datago") == "secret-key"
    # AAD is bound to owner+provider — different owners cannot decrypt (no row → None)
    assert repo.get_secret("oidc:cbx-other", "datago") is None
    # upsert (rotate)
    repo.put(owner, "datago", "rotated")
    assert repo.get_secret(owner, "datago") == "rotated"
    assert "datago" in repo.list_configured_providers(owner)
    assert repo.delete(owner, "datago") is True
    assert repo.delete(owner, "datago") is False


def test_credential_rejects_empty_owner_and_credential(engine) -> None:  # type: ignore[no-untyped-def]
    cipher = AesGcmCredentialCipher.from_base64(base64.b64encode(os.urandom(32)).decode())
    repo = CubridCredentialRepository(engine, cipher)
    with pytest.raises(ValueError):
        repo.put("", "datago", "x")
    with pytest.raises(ValueError):
        repo.put("oidc:cbx-owner", "datago", "   ")


def test_artifact_store_manifest_authoritative(tmp_path, engine) -> None:  # type: ignore[no-untyped-def]
    from kpubdata_builder.store.artifacts.cubrid import CubridArtifactStore
    from kpubdata_builder.store.artifacts.local import LocalArtifactStore

    store = CubridArtifactStore(tmp_path, engine)
    manifest = {"build_id": "cbx-run", "created_by": "dev:local", "errors": []}
    store.put_manifest("cbx-run", manifest)

    # CUBRID canonical + FS mirror
    assert store.get_manifest("cbx-run") == manifest
    assert LocalArtifactStore(tmp_path).get_manifest("cbx-run") == manifest
    assert store.run_dir("cbx-run") == tmp_path / "cbx-run"

    # FS mirror corrupt, CUBRID canonical wins
    (tmp_path / "cbx-run" / "manifest.json").write_text("{ broken", encoding="utf-8")
    assert store.get_manifest("cbx-run") == manifest

    # FS-only run without CUBRID row → FS fallback
    LocalArtifactStore(tmp_path).put_manifest("cbx-fsonly", {"build_id": "cbx-fsonly"})
    assert store.get_manifest("cbx-fsonly") == {"build_id": "cbx-fsonly"}

    ids = set(store.list_run_ids())
    assert {"cbx-run", "cbx-fsonly"} <= ids


# Startup gate "normal connection" validation moved to
# tests/cubrid/test_cubrid_fail_closed.py. As validate_storage_config()
# opens a real connection (#587), the version here was (a) subsumed into
# that file's tests, and (b) didn't dispose the global Engine, leaking to
# subsequent tests. Unified under the version with disposal fixture.


def test_startup_validation_refuses_a_bare_cubrid_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting driver resolves to legacy C-extension — must block before startup.

    If this path is open, startup silently succeeds and **fails on first query**
    with ImportError: Could not import CUBRIDdb (ADR 0016).
    """
    from kpubdata_builder.store.backend import cubrid_url

    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.setenv("KPUBDATA_BUILDER_CUBRID_URL", "cubrid://dba:@127.0.0.1:33000/kpubdata")
    # Omitting driver is normalized, not rejected — corrected to pycubrid.
    assert cubrid_url().startswith("cubrid+pycubrid://")


@pytest.mark.parametrize("driver", ["cubriddb", "cubrid", "aiopycubrid"])
def test_startup_validation_refuses_unsupported_drivers(
    driver: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup must not allow drivers not installed by [cubrid] extra."""
    from kpubdata_builder.store.backend import cubrid_url

    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.setenv(
        "KPUBDATA_BUILDER_CUBRID_URL", f"cubrid+{driver}://dba:@127.0.0.1:33000/kpubdata"
    )
    with pytest.raises(RuntimeError):
        cubrid_url()
