"""``GET /builds/{run_id}/events`` HTTP API test (#496).

Event emission itself (which boundary emits which event) is covered by test_pipeline_events.py.
This file verifies the route adapter layer — existence/ownership/bounded query
(limit/tail)/secret non-exposure —
via actual ``BuilderService.build()``/``dispatch``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

VALID_SPEC_YAML = (
    "dataset_id: dataset.events\n"
    "title: Events Fixture\n"
    "description: fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "    alias: air\n"
    "    params:\n"
    "      api_key: SUPER-SECRET-API-KEY\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: out/data.jsonl\n"
    "    options:\n"
    "      kaggle_key: SUPER-SECRET-EXPORT-KEY\n"
)

# spec where fetch itself fails (dataset unknown to FakeClient).
FETCH_FAILURE_SPEC_YAML = VALID_SPEC_YAML.replace("dataset: air_quality", "dataset: missing")


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> list[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _service(tmp_path: Path, *, rows: int = 2) -> BuilderService:
    records = [{"id": str(i), "v": i * 10} for i in range(1, rows + 1)]
    client = _FakeClient({"datago.air_quality": records})
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: client)


def _build(service: BuilderService, run_id: str, spec_yaml: str = VALID_SPEC_YAML) -> int:
    resp = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": run_id})
    return resp.status_code


def _file_source_spec_yaml(upload_id: str) -> str:
    return (
        f"""
dataset_id: dataset.async-uploaded
title: Async Uploaded Fixture
description: file source build (#498 async owner propagation regression)
sources:
  - kind: file
    upload_id: {upload_id}
    format: csv
    encoding: utf-8
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n"
    )


class _ObservedAsyncService(BuilderService):
    """``_run_build_job`` (all of ``build()`` call + owner_id manifest correction)
    completion; sets ``completed`` — helper to deterministically await the actual async build
    completion
    (manifest correction reflected) without polling/sleep."""

    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: object,
        completed: threading.Event,
        async_max_workers: int = 1,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=client_factory,  # type: ignore[arg-type]
            async_max_workers=async_max_workers,
        )
        self._completed = completed

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        try:
            return super()._run_build_job(spec_yaml, run_id, created_by, cancellation)
        finally:
            self._completed.set()


class _BlockingAsyncService(BuilderService):
    """holds async worker until ``release``.

    ``_run_build_job`` (actual entry point of worker pool execution) sets ``entered``
    then waits for ``release`` — meanwhile registry state is already "running"
    (``AsyncBuildExecutor._run`` transitions via ``begin_run`` *before* calling runner)
    but run directory/manifest are not yet created (``BuilderService.build()`` not yet called).
    After ``release``, actually calls ``build()`` to complete normally and sets ``completed`` —
    both active and completed states are deterministically reproduced by this single class (#496
    follow-up).
    """

    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: object,
        entered: threading.Event,
        release: threading.Event,
        completed: threading.Event,
        async_max_workers: int = 1,
        async_max_queue_size: int = 10,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=client_factory,  # type: ignore[arg-type]
            async_max_workers=async_max_workers,
            async_max_queue_size=async_max_queue_size,
        )
        self._entered = entered
        self._release = release
        self._completed = completed

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        self._entered.set()
        self._release.wait(timeout=5)
        try:
            return super()._run_build_job(spec_yaml, run_id, created_by, cancellation)
        finally:
            self._completed.set()


class TestGetBuildEventsRouting:
    def test_completed_run_returns_ordered_events(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 200
        assert resp.body["run_id"] == "r1"
        events = cast(list[dict[str, object]], resp.body["events"])
        assert events[0]["event"] == "run_started"
        assert events[-1]["event"] == "run_finished"
        seqs = [cast(int, e["seq"]) for e in events]
        assert seqs == sorted(seqs)

    def test_failed_run_still_returns_200_with_failure_events(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", FETCH_FAILURE_SPEC_YAML) == 502

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert events[-1]["event"] == "run_failed"
        assert events[-1]["status"] == "fail"

    def test_partial_run_shows_success_then_failure(self, tmp_path: Path) -> None:
        """Previous success events do not disappear on failure (append-only, #496)."""
        service = _service(tmp_path)
        spec_yaml = (
            "dataset_id: dataset.partial\n"
            "title: Partial Fixture\n"
            "description: fixture\n"
            "sources:\n"
            "  - provider: datago\n"
            "    dataset: air_quality\n"
            "    alias: good\n"
            "  - provider: datago\n"
            "    dataset: missing\n"
            "    alias: bad\n"
            "exports:\n"
            "  - kind: jsonl\n"
            "    output_path: out/data.jsonl\n"
        )
        assert _build(service, "r1", spec_yaml) == 502

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        good_events = [e["event"] for e in events if e.get("source_key") == "good"]
        bad_events = [e["event"] for e in events if e.get("source_key") == "bad"]
        assert "stage_completed" in good_events
        assert "stage_failed" in bad_events

    def test_empty_timeline_for_run_with_no_events(self, tmp_path: Path) -> None:
        """Run created without going through BuilderService.build() (exists but no events)."""
        run_dir = tmp_path / "legacy"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

        resp = dispatch(_service(tmp_path), "GET", "/builds/legacy/events", None)
        assert resp.status_code == 200
        assert resp.body["events"] == []

    def test_unknown_run_returns_404(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/nope/events", None)
        assert resp.status_code == 404

    def test_unsafe_run_id_returns_400(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/../escape/events", None)
        assert resp.status_code == 400

    def test_does_not_shadow_other_builds_subroutes(self, tmp_path: Path) -> None:
        """Other /builds/{run_id}/* routes (e.g., manifest) still work normally."""
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/manifest", None)
        assert resp.status_code == 200
        resp2 = dispatch(service, "GET", "/builds/r1/stages", None)
        assert resp2.status_code == 200


class TestLimitAndTail:
    def _build_many_sources(self, service: BuilderService, run_id: str, count: int) -> int:
        sources = "\n".join(
            f"  - provider: datago\n    dataset: air_quality\n    alias: s{i}\n"
            for i in range(count)
        )
        spec_yaml = (
            "dataset_id: dataset.many\n"
            "title: Many Fixture\n"
            "description: fixture\n"
            f"sources:\n{sources}"
            "exports:\n"
            "  - kind: jsonl\n"
            "    output_path: out/data.jsonl\n"
        )
        return _build(service, run_id, spec_yaml)

    def test_default_limit_returns_from_start(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=1")
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert len(events) == 1
        assert events[0]["event"] == "run_started"

    def test_tail_true_returns_most_recent_ascending(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=1&tail=true")
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert len(events) == 1
        assert events[0]["event"] == "run_finished"

    def test_invalid_limit_zero_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=0")
        assert resp.status_code == 400

    def test_invalid_limit_non_integer_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=abc")
        assert resp.status_code == 400

    def test_limit_above_max_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=1001")
        assert resp.status_code == 400

    def test_invalid_tail_value_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="tail=yes")
        assert resp.status_code == 400

    def test_bool_as_int_limit_is_rejected_like_other_routes(self, tmp_path: Path) -> None:
        """query strings are always strings so bool bypass is impossible anyway, but
        abnormal strings are rejected as 400 like other routes."""
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=true")
        assert resp.status_code == 400


class TestOwnership:
    def test_cross_owner_returns_404_before_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        assert _build(service, "r1") == 200

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 404

        # when ownership is denied, event store lookup logic is not reached.
        monkeypatch.setattr(
            service._event_store,
            "list_for_run",
            lambda *a, **kw: pytest.fail("event store leaked past 403"),
        )
        resp2 = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp2.status_code == 404

    def test_owner_can_read_own_run_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        assert _build(service, "r1") == 200

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 200
        assert cast(list[object], resp.body["events"])

    def test_unknown_run_404_before_ownership_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-existent run returns 404 without distinguishing cross-owner (existence not
        exposed).
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        resp = dispatch(service, "GET", "/builds/nope/events", None)
        assert resp.status_code == 404


class TestSecurityNoSecretLeak:
    def test_no_credential_or_path_in_events_response(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        body_text = json.dumps(resp.body)
        assert "SUPER-SECRET-API-KEY" not in body_text
        assert "SUPER-SECRET-EXPORT-KEY" not in body_text
        assert str(tmp_path) not in body_text
        assert "Authorization" not in body_text
        assert "Bearer" not in body_text

    def test_no_stack_trace_or_exception_repr_on_failure(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", FETCH_FAILURE_SPEC_YAML) == 502

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        body_text = json.dumps(resp.body)
        assert "Traceback" not in body_text
        assert "KeyError" not in body_text
        assert str(tmp_path) not in body_text

    def test_no_credential_leak_even_on_failed_source(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        # cause silver validation failure to also verify message carried in stage_failed event.
        silver_failure_spec = VALID_SPEC_YAML.replace(
            "    alias: air\n",
            "    alias: air\n    schema:\n      required: [does_not_exist]\n",
        )
        assert _build(service, "r1", silver_failure_spec) == 502

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        body_text = json.dumps(resp.body)
        assert "SUPER-SECRET-API-KEY" not in body_text
        assert "SUPER-SECRET-EXPORT-KEY" not in body_text


class TestActiveAsyncRunEvents:
    """events of runs submitted via ``POST /builds`` (async) can be queried even during execution
    (#496 follow-up: BLOCKER).

    ``check_run_exists``/``check_ownership`` assume run directory·manifest.json already exist,
    but async run has no run directory until worker starts and manifest appears only after run ends.
    During that span (queued/running), event store already has ``run_submitted`` (and subsequent
    events),
    so this endpoint must not return 404/403.
    """

    def _submit(
        self, service: BuilderService, run_id: str, spec_yaml: str = VALID_SPEC_YAML
    ) -> ServiceResponse:
        resp = dispatch(service, "POST", "/builds", {"spec": spec_yaml, "run_id": run_id})
        assert isinstance(resp, ServiceResponse)
        return resp

    def test_queued_run_returns_200_with_run_submitted(self, tmp_path: Path) -> None:
        """Queued run is queryable even when single worker is busy with another run."""
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
            async_max_workers=1,
        )
        first = self._submit(service, "run1")
        assert first.status_code == 202
        assert entered.wait(timeout=5)  # worker is holding run1.

        second = self._submit(service, "run2")
        assert second.status_code == 202
        assert second.body["status"] == "queued"
        # run2 doesn't even have run directory yet — old check_run_exists would return 404.
        assert not (tmp_path / "run2").exists()

        resp = dispatch(service, "GET", "/builds/run2/events", None)
        release.set()
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert [e["event"] for e in events] == ["run_submitted"]
        assert completed.wait(timeout=5)

    def test_running_run_returns_200_before_manifest_exists(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)
        assert not (tmp_path / "run1" / "manifest.json").exists()

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert [e["event"] for e in events] == ["run_submitted"]
        assert completed.wait(timeout=5)

    def test_ownership_enforced_active_owner_can_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 200
        assert completed.wait(timeout=5)

    def test_ownership_enforced_other_principal_gets_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 404
        assert completed.wait(timeout=5)

    def test_unknown_run_still_404_when_not_in_async_registry(self, tmp_path: Path) -> None:
        """Still 404 if in neither async registry nor persisted run."""
        service = _service(tmp_path)
        resp = dispatch(service, "GET", "/builds/nope/events", None)
        assert resp.status_code == 404

    def test_completed_async_run_still_uses_manifest_ownership_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Completed run remains as terminal entry in async registry, but existing
        completed persisted run retrieval (#496 original API) must continue working normally."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)
        release.set()
        assert completed.wait(timeout=5)

        assert (tmp_path / "run1" / "manifest.json").exists()

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        event_names = [e["event"] for e in events]
        assert "run_submitted" in event_names
        assert "run_finished" in event_names

    def test_same_label_different_owner_id_active_run_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with same created_by/label (legacy), different stable owner_id (#505)
        must be rejected — core case where active run access must prioritize owner_id comparison."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="same", owner_id="oidc:owner-a"),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        # label("oidc:same") is same as previous principal but owner_id differs.
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="same", owner_id="oidc:owner-b"),
        )
        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 404
        assert completed.wait(timeout=5)

    def test_matching_owner_id_active_run_returns_200(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a"),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 200
        assert completed.wait(timeout=5)

    def test_owner_id_never_leaks_into_build_status_or_events_wire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        secret_owner_id = "oidc:super-secret-owner-hash"
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id=secret_owner_id),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert "owner_id" not in submitted.body
        assert secret_owner_id not in json.dumps(submitted.body)
        assert entered.wait(timeout=5)

        status = dispatch(service, "GET", "/builds/run1", None)
        assert "owner_id" not in status.body
        assert secret_owner_id not in json.dumps(status.body)

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 200
        assert "owner_id" not in json.dumps(resp.body)
        assert secret_owner_id not in json.dumps(resp.body)
        assert completed.wait(timeout=5)

    def test_enqueue_failed_terminal_without_manifest_only_owner_can_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even terminal job terminated without manifest due to worker pool enqueue failure
        (build() never called, no run directory exists), only owner can query — registry
        snapshot's owner_id is sole
        determination ground."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = BuilderService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
        )

        def _broken_executor_submit(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("simulated worker pool rejection")

        monkeypatch.setattr(service._async_builds._executor, "submit", _broken_executor_submit)

        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")
        response = service.submit_build(
            VALID_SPEC_YAML, run_id="run1", created_by=owner.label, owner_id=owner.owner_id
        )
        assert response.status_code >= 500
        assert not (tmp_path / "run1").exists()

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        resp_owner = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp_owner.status_code == 200
        events = cast(list[dict[str, object]], resp_owner.body["events"])
        assert [e["event"] for e in events] == ["run_submitted", "run_failed"]

        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="b", owner_id="oidc:owner-b"),
        )
        resp_other = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp_other.status_code == 404

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        resp2 = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp2.status_code == 404


class TestAsyncManifestOwnerIdPropagation:
    """After async run completion, persisted manifest.owner_id contains submitting principal's
        stable
    owner_id (#496 follow-up: security BLOCKER).

    Before fix, ``_run_build_job`` passed no owner_id to ``build()``, so async run's
    manifest.owner_id was always None —
    the moment manifest was written, ``check_active_run_access`` switched to manifest path,
    reverting from stable
    owner_id comparison (#505) to legacy created_by/label comparison. With two principals using same
    label (OIDC sub 8-char truncation collision etc.), that fallback could only on completed runs
    permit cross-owner
    access — the A/B scenarios below reproduce that.
    """

    def _submit(
        self, service: BuilderService, run_id: str, spec_yaml: str = VALID_SPEC_YAML
    ) -> ServiceResponse:
        resp = dispatch(service, "POST", "/builds", {"spec": spec_yaml, "run_id": run_id})
        assert isinstance(resp, ServiceResponse)
        return resp

    def test_completed_manifest_records_submitting_principals_stable_owner_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A (``oidc:same``/``oidc:owner-A``) async build → completion → manifest creation.

        Requirement scenario 1: persisted manifest's owner_id == oidc:owner-A.
        """
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        principal_a = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_a)

        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        manifest_data = json.loads(
            (tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest_data["owner_id"] == "oidc:owner-A"

    def test_completed_run_owner_gets_200_other_same_label_principal_gets_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement scenarios 2/3: A gets 200, B (different owner_id) using same label gets
        403.
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        principal_a = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A")
        principal_b = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-B")
        assert (
            principal_a.label == principal_b.label
        )  # regression prerequisite: legacy labels are same.

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_a)
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)
        assert (tmp_path / "run1" / "manifest.json").exists()

        resp_owner = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp_owner.status_code == 200

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_b)
        resp_other = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp_other.status_code == 404

    def test_active_same_label_different_owner_still_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression prevention: even active period without manifest yet (#496 previous round fix)
        must still work — verify this fix doesn't break that path."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A"),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)
        assert not (tmp_path / "run1" / "manifest.json").exists()

        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="same", owner_id="oidc:owner-B"),
        )
        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 404
        assert completed.wait(timeout=5)

    def test_owner_id_not_exposed_via_wire_after_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even after manifest correction, owner_id in build_status/events/manifest response
        is never exposed — public API/OpenAPI contract unchanged."""
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        secret_owner_id = "oidc:super-secret-owner-hash"
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id=secret_owner_id),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        status = dispatch(service, "GET", "/builds/run1", None)
        assert "owner_id" not in status.body
        assert secret_owner_id not in json.dumps(status.body)

        events_resp = dispatch(service, "GET", "/builds/run1/events", None)
        assert events_resp.status_code == 200
        assert "owner_id" not in json.dumps(events_resp.body)
        assert secret_owner_id not in json.dumps(events_resp.body)

        manifest_resp = dispatch(service, "GET", "/builds/run1/manifest", None)
        assert manifest_resp.status_code == 200
        assert "owner_id" not in manifest_resp.body
        assert secret_owner_id not in json.dumps(manifest_resp.body)

    def test_async_file_source_resolver_still_does_not_receive_owner_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#498 known limitation persistence check: ``kind=file`` submitted via async path
        even though manifest owner_id is now correctly filled, still does not pass owner_id to file
        source resolver — if upload owner themselves submit, async path cannot find that upload
        and build must fail
        (unlike sync ``/build``)."""
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({}),
            completed=completed,
        )
        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")

        created = service.create_upload(
            b"id,amount\n1,1000\n",
            format="csv",
            encoding="utf-8",
            original_filename="trades.csv",
            principal=owner,
        )
        assert created.status_code == 200
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        submitted = self._submit(service, "run1", _file_source_spec_yaml(upload_id))
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        # manifest ownership is now correct — but file resolver
        # still doesn't receive owner_id so can't find upload; build itself fails
        # (#498 async limitation, maintained as-is).
        manifest_data = json.loads(
            (tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest_data["owner_id"] == "oidc:owner-a"
        assert manifest_data["errors"], "file resolver가 owner_id 없이 업로드를 찾지 못해야 한다"

        status = dispatch(service, "GET", "/builds/run1", None)
        assert status.body["status"] == "failed"

    def test_completed_run_build_index_records_stable_owner_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not just manifest but also BuildIndex (#505 SSOT, ``GET /builds`` listing path)
        must carry same stable owner_id — two repositories must not create SSOT
        mismatch with different values."""
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        principal_a = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_a)

        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        manifest_data = json.loads(
            (tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8")
        )
        index_entry = service._build_index.get("run1")
        assert index_entry is not None
        assert index_entry.owner_id == "oidc:owner-A"
        assert index_entry.owner_id == manifest_data["owner_id"]

    def test_build_list_hides_completed_run_from_different_owner_with_same_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """persisted ownership path using BuildIndex (``GET /builds`` listing) also
        must not permit different owner_id principal with same label."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        principal_a = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A")
        principal_b = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-B")
        assert principal_a.label == principal_b.label

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_a)
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        list_as_owner = dispatch(service, "GET", "/builds", None)
        assert list_as_owner.status_code == 200
        owner_run_ids = [
            b["run_id"] for b in cast(list[dict[str, object]], list_as_owner.body["builds"])
        ]
        assert "run1" in owner_run_ids

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_b)
        list_as_other = dispatch(service, "GET", "/builds", None)
        assert list_as_other.status_code == 200
        other_run_ids = [
            b["run_id"] for b in cast(list[dict[str, object]], list_as_other.body["builds"])
        ]
        assert "run1" not in other_run_ids
        assert "owner_id" not in json.dumps(list_as_other.body)
