"""Every ended run's cause is a stable code and a stage, in the index and the admin list (#1120).

``test_run_failure_record.py`` and ``test_run_ending_record.py`` cover the records: a
manifest's ``failures`` and the ``run_endings`` of a run that left no manifest, each
projected into the build index as one line. The line alone made a client parse a
sentence to tell a refused key from a restart. The index now keeps the code and the
stage of the same failure beside it, a rebuild restores them, and ``GET /admin/runs``
serves them as ``error_code`` and ``error_stage`` — only when they are Builder's own
vocabulary, so a value that reached the index any other way is not served.

A single-user deployment kept no record of a run a restart interrupted: it did not mark
such runs at all, and they stayed unfinished for good. It marks them now, as a
multi-user deployment does, without saying the keys were lost.

Canaries in the provider key, the spec, a provider's error and a raw SQL error are
looked for in every index column, the admin answer and the log records.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import kpubdata
import pytest
import yaml

from kpubdata_builder.manifest import run_failure_cause
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import BuildJobSnapshot
from kpubdata_builder.service.routes import admin
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.build_index import rebuild_index

_CANARY = "canary-secret-1120-cause"
_ALICE = Principal("oidc", "alice", "oidc:alice")
_BOB = Principal("oidc", "bob", "oidc:bob")
_ADMIN = Principal(kind="oidc", identifier="admin123", owner_id="oidc:admin", is_admin=True)
_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    """A provider whose answer the test sets; ``gate`` holds a fetch until it is set."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.answer: list[dict[str, JsonValue]] | Exception = [{"id": "1", "v": 1}]
        self.gate = gate
        self.fetching = threading.Event()

    def list(self, **_params: object) -> _Result:
        self.fetching.set()
        if self.gate is not None:
            self.gate.wait(timeout=20)
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Result(list(self.answer))

    def dataset(self, _key: str) -> _Source:
        return self


def _factory(source: _Source) -> Callable[..., object]:
    def create(
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
        environment_keys: bool = True,
    ) -> object:
        return source

    return create


def _spec(schema: str = "", *, title: str = "Air") -> str:
    return (
        f"dataset_id: air\ntitle: {title}\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: air_station\n    alias: m\n"
        + schema
        + "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )


class _CancelAt:
    def __init__(self, *, after: int) -> None:
        self._after = after
        self._checks = 0

    def cancel_requested(self) -> bool:
        self._checks += 1
        return self._checks > self._after

    def commit(self) -> bool:
        return False


def _service(tmp_path: Path, source: _Source) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=_factory(source))


def _causes(service: BuilderService) -> dict[str, tuple[str | None, str | None]]:
    return {
        entry.run_id: (entry.error_code, entry.error_stage)
        for entry in service._build_index.list_builds(limit=None)
    }


def _admin_rows(service: BuilderService) -> dict[str, dict[str, Any]]:
    response = admin.route(service, "GET", "/admin/runs", None, "", _ADMIN)
    assert response is not None and response.status_code == 200, response
    runs = cast(list[dict[str, Any]], cast(dict[str, Any], response.body)["runs"])
    return {run["run_id"]: run for run in runs}


def _admin_cause(service: BuilderService, run_id: str) -> tuple[Any, Any]:
    row = _admin_rows(service)[run_id]
    return row["error_code"], row["error_stage"]


def _index_text(tmp_path: Path) -> str:
    """Every column of every row of the index file."""
    with sqlite3.connect(tmp_path / "_builds.sqlite") as conn:
        return str(conn.execute("SELECT * FROM builds").fetchall())


def _as(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> None:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principal)


def _request(
    service: BuilderService,
    method: str,
    path: str,
    body: dict[str, JsonValue] | None = None,
    *,
    key: str | None = None,
) -> ServiceResponse:
    response = dispatch(
        service,
        method,
        path,
        body,
        provider_key_headers=[f"datago={key}"] if key is not None else (),
    )
    assert isinstance(response, ServiceResponse)
    return response


def _stop(*services: BuilderService) -> None:
    for service in services:
        service._async_builds._executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# a run with a manifest: the code and the stage of its first failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "schema", "cause"),
    [
        (
            kpubdata.AuthError("refused", provider="datago", provider_code="30"),
            "",
            ("application_required", "bronze"),
        ),
        (
            kpubdata.RateLimitError("slow", provider="datago", provider_code="22"),
            "",
            ("rate_limited", "bronze"),
        ),
        (None, "    schema:\n      required: [not_there]\n", ("pipeline_failed", "silver")),
    ],
)
def test_a_failed_run_is_indexed_and_listed_with_its_code_and_stage(
    tmp_path: Path,
    answer: Exception | None,
    schema: str,
    cause: tuple[str, str],
) -> None:
    source = _Source()
    if answer is not None:
        source.answer = answer
    service = _service(tmp_path, source)

    assert service.build(_spec(schema), run_id="r1").status_code == 502

    assert _causes(service)["r1"] == cause
    assert _admin_cause(service, "r1") == cause


def test_a_successful_run_has_no_cause(tmp_path: Path) -> None:
    service = _service(tmp_path, _Source())

    assert service.build(_spec(), run_id="r1").status_code == 200

    assert _causes(service)["r1"] == (None, None)
    assert _admin_cause(service, "r1") == (None, None)


def test_a_cancelled_run_is_listed_with_why_and_where(tmp_path: Path) -> None:
    service = _service(tmp_path, _Source())

    service.build(_spec(), run_id="r1", cancellation=_CancelAt(after=0))

    assert _causes(service)["r1"] == ("cancelled", "bronze")
    assert _admin_cause(service, "r1") == ("cancelled", "bronze")


def test_a_run_that_left_no_manifest_is_listed_with_its_recorded_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, _Source())

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(f"worker pool rejected {_CANARY}")

    monkeypatch.setattr(service._async_builds._executor, "submit", refuse)
    try:
        assert service.submit_build(_spec(), run_id="r1").status_code == 500
    finally:
        _stop(service)

    assert _causes(service)["r1"] == ("enqueue_failed", "queued")
    assert _admin_cause(service, "r1") == ("enqueue_failed", "queued")
    assert _CANARY not in str(_admin_rows(service))
    assert _CANARY not in _index_text(tmp_path)


def test_the_causes_survive_a_rebuild_of_the_index(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    assert service.build(_spec(), run_id="ok").status_code == 200
    service.build(_spec(), run_id="stopped", cancellation=_CancelAt(after=0))
    source.answer = kpubdata.AuthError("refused", provider="datago", provider_code="30")
    assert service.build(_spec(), run_id="refused").status_code == 502
    before = _causes(service)
    assert before == {
        "ok": (None, None),
        "stopped": ("cancelled", "bronze"),
        "refused": ("application_required", "bronze"),
    }
    service._build_index.close()
    (tmp_path / "_builds.sqlite").unlink()

    assert rebuild_index(tmp_path) == 3

    rebuilt = _service(tmp_path, source)
    assert _causes(rebuilt) == before
    assert _admin_cause(rebuilt, "refused") == ("application_required", "bronze")


def test_an_index_from_the_previous_schema_is_refilled_with_the_causes(tmp_path: Path) -> None:
    """An index written before the two columns existed is made again from the manifests
    when it is opened (#1096), so an upgrade does not leave earlier runs without them."""
    source = _Source()
    source.answer = kpubdata.AuthError("refused", provider="datago", provider_code="30")
    service = _service(tmp_path, source)
    assert service.build(_spec(), run_id="r1").status_code == 502
    service._build_index.close()
    with sqlite3.connect(tmp_path / "_builds.sqlite") as conn:
        conn.execute("ALTER TABLE builds DROP COLUMN error_code")
        conn.execute("ALTER TABLE builds DROP COLUMN error_stage")
        conn.execute("UPDATE schema_version SET version = 5")

    reopened = _service(tmp_path, source)

    assert _causes(reopened)["r1"] == ("application_required", "bronze")


# ---------------------------------------------------------------------------
# only Builder's vocabulary is served
# ---------------------------------------------------------------------------


def test_a_value_that_is_not_builders_vocabulary_is_not_served(tmp_path: Path) -> None:
    """The index is a file: a value in it that Builder would not write is not served."""
    service = _service(tmp_path, _Source())
    service._build_index.insert_or_replace(
        run_id="odd",
        status="failed",
        started_at=None,
        finished_at="2026-10-11T00:00:00+00:00",
        error="the run did not finish",
        error_code=f"SELECT * FROM t WHERE key='{_CANARY}'",
        error_stage=f"https://x.example/?serviceKey={_CANARY}",
    )

    row = _admin_rows(service)["odd"]

    assert (row["error_code"], row["error_stage"]) == (None, None)
    assert _CANARY not in str(row)


def test_every_code_and_stage_builder_records_is_served() -> None:
    """The allowlist is made of the vocabularies the records are written from, and the
    contract names every value in it."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    item = contract["components"]["schemas"]["AdminRunsResponse"]["properties"]["runs"]["items"]
    code_doc = item["properties"]["error_code"]["description"]
    stage_doc = item["properties"]["error_stage"]["description"]

    assert {"application_required", "pipeline_failed", "join_duplicate_key"} <= (
        admin.FAILURE_CODES
    )
    assert {"conflict", "table_exists", "cancelled", "interrupted"} <= admin.FAILURE_CODES
    assert {
        "queued",
        "started",
        "bronze",
        "silver",
        "gold",
        "export",
        "composition",
        "warehouse",
    } == admin.FAILURE_STAGES
    assert [code for code in sorted(admin.FAILURE_CODES) if f"`{code}`" not in code_doc] == []
    assert [s for s in sorted(admin.FAILURE_STAGES) if f"`{s}`" not in stage_doc] == []
    assert item["properties"]["error_code"]["type"] == ["string", "null"]
    assert item["properties"]["error_stage"]["type"] == ["string", "null"]


@pytest.mark.parametrize(
    ("manifest", "cause"),
    [
        ({}, (None, None)),
        ({"errors": ["a: broke"]}, (None, None)),
        ({"warehouse_failures": {"m": {"reason": "conflict"}}}, ("conflict", "warehouse")),
        ({"warehouse_failures": {"m": {"reason": "rows: 1, 2"}}}, (None, "warehouse")),
        (
            {
                "failures": [
                    {"source_key": "a", "stage": "gold", "code": "pipeline_failed", "summary": "s"},
                    {"source_key": "b", "stage": "bronze", "code": "retired", "summary": "t"},
                ]
            },
            ("pipeline_failed", "gold"),
        ),
    ],
)
def test_the_cause_is_that_of_the_failure_the_line_states(
    manifest: dict[str, object], cause: tuple[str | None, str | None]
) -> None:
    assert run_failure_cause(manifest) == cause


def test_a_job_row_carries_the_cause_of_its_build_response() -> None:
    """A job the index does not hold yet is listed from the registry (#1221); its code
    and stage come from the build response's outcomes, as its line does."""

    def job(response: dict[str, JsonValue] | None, error: str | None) -> BuildJobSnapshot:
        return BuildJobSnapshot(
            run_id="r",
            status="failed",
            created_at="2026-10-11T00:00:00+00:00",
            updated_at="2026-10-11T00:00:01+00:00",
            response=response,
            error=error,
        )

    refused = {
        "outcomes": [
            {
                "source_key": "m",
                "status": "failed",
                "reason": "application_required",
                "stages_completed": [],
            }
        ]
    }
    assert admin._job_failure(job(refused, f"key {_CANARY}"))[1:] == (
        "application_required",
        "bronze",
    )
    later = {"outcomes": [{"source_key": "m", "status": "failed", "stages_completed": ["bronze"]}]}
    assert admin._job_failure(job(later, "x"))[1:] == ("pipeline_failed", "silver")
    commit = {"outcomes": [], "warehouse_failures": {"m": {"reason": "conflict"}}}
    assert admin._job_failure(job(commit, "x"))[1:] == ("conflict", "warehouse")
    assert admin._job_failure(job(None, f"internal error: {_CANARY}")) == (
        "the build failed with an internal error",
        None,
        None,
    )


# ---------------------------------------------------------------------------
# canaries
# ---------------------------------------------------------------------------


def test_no_canary_reaches_the_cause_the_index_the_admin_list_or_a_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A key, a provider URL, a raw SQL error and the spec's title each hold a canary."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    caplog.set_level(logging.DEBUG)
    source = _Source()
    source.answer = RuntimeError(
        f"GET https://apis.data.go.kr/x?serviceKey={_CANARY} failed: "
        f"Binder Error: SELECT * FROM t WHERE v = '{_CANARY}'"
    )
    service = _service(tmp_path, source)
    _as(monkeypatch, _ALICE)
    try:
        response = _request(
            service,
            "POST",
            "/build",
            {"spec": _spec(title=f"Air {_CANARY}"), "run_id": "r1"},
            key=_CANARY,
        )
        assert response.status_code == 502, response.body
        assert _causes(service)["r1"] == ("pipeline_failed", "bronze")
        rows = _admin_rows(service)
        assert (rows["r1"]["error_code"], rows["r1"]["error_stage"]) == (
            "pipeline_failed",
            "bronze",
        )
    finally:
        _stop(service)

    assert _CANARY not in str(rows)
    assert _CANARY not in _index_text(tmp_path)
    assert _CANARY not in (tmp_path / "r1" / "manifest.json").read_text(encoding="utf-8")
    leaked = [record.getMessage() for record in caplog.records if _CANARY in record.getMessage()]
    assert leaked == []


# ---------------------------------------------------------------------------
# a restart of a single-user deployment
# ---------------------------------------------------------------------------


@pytest.fixture()
def single_user_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[BuilderService]:
    """A single-user service started after another was stopped with a job in a fetch.

    The first service's worker stays held until the test is over: a real restart kills
    it, and letting it run on would add a manifest behind the test.
    """
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    source = _Source(gate)
    first = _service(tmp_path, source)
    assert first.submit_build(_spec(title=f"Air {_CANARY}"), run_id="in-flight").status_code == 202
    assert source.fetching.wait(10)

    second = _service(tmp_path, _Source())
    assert second.mark_interrupted_runs() == ("in-flight",)
    try:
        yield second
    finally:
        gate.set()
        _stop(first, second)


def test_a_single_user_restart_records_the_run_it_interrupted(
    tmp_path: Path, single_user_restarted: BuilderService, caplog: pytest.LogCaptureFixture
) -> None:
    service = single_user_restarted
    summary = "the server restarted, and the run was interrupted at the bronze stage"
    entry = service._build_index.get("in-flight")
    assert entry is not None
    assert (entry.status, entry.error, entry.error_code, entry.error_stage) == (
        "failed",
        summary,
        "interrupted",
        "bronze",
    )
    row = _admin_rows(service)["in-flight"]
    assert (row["error"], row["error_code"], row["error_stage"]) == (
        summary,
        "interrupted",
        "bronze",
    )
    assert _CANARY not in str(row)
    assert _CANARY not in _index_text(tmp_path)
    assert not [r for r in caplog.records if _CANARY in r.getMessage()]


def test_a_single_user_restart_does_not_say_the_keys_were_lost(
    single_user_restarted: BuilderService,
) -> None:
    """The keys of a single-user deployment are stored and still there: the status the
    owner reads says the run was interrupted, and has no ``credentials_required``."""
    status = single_user_restarted.build_status("in-flight")

    assert status.status_code == 200
    body = cast(dict[str, Any], status.body)
    assert body["status"] == "failed"
    assert "code" not in body
    assert str(body["error"]).startswith("interrupted: the server restarted")
    assert "credentials_required" not in str(body)


def test_a_second_single_user_restart_marks_nothing(
    tmp_path: Path, single_user_restarted: BuilderService
) -> None:
    third = _service(tmp_path, _Source())
    try:
        assert third.mark_interrupted_runs() == ()
    finally:
        _stop(third)


def test_a_multi_user_restart_still_says_the_keys_were_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    gate = threading.Event()
    source = _Source(gate)
    first = _service(tmp_path, source)
    _as(monkeypatch, _ALICE)
    submitted = _request(
        first, "POST", "/builds", {"spec": _spec(), "run_id": "in-flight"}, key="k"
    )
    assert submitted.status_code == 202, submitted.body
    assert source.fetching.wait(10)
    second = _service(tmp_path, _Source())
    try:
        assert second.mark_interrupted_runs() == ("in-flight",)
        status = _request(second, "GET", "/builds/in-flight")
        assert (status.body["status"], status.body["code"]) == ("failed", "credentials_required")
        assert _admin_cause(second, "in-flight") == ("interrupted", "bronze")
        _as(monkeypatch, _BOB)
        assert _request(second, "GET", "/admin/runs").status_code == 403
        assert _request(second, "GET", "/builds/in-flight").status_code == 404
    finally:
        gate.set()
        _stop(first, second)
