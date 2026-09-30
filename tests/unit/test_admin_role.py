"""Admin role and admin endpoints (#679).

Most important here are **negative tests**. Admin risk is not failure to work
but working beyond necessity — this product is BYOK, and whether admins can
access user data is still undecided.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

import pytest

from kpubdata_builder.service import ownership as ownership_module
from kpubdata_builder.service.admin_audit import record_admin_action
from kpubdata_builder.service.auth import Principal, compute_owner_id
from kpubdata_builder.service.responses import FileResponse, ServiceResponse
from kpubdata_builder.service.routes import admin


@dataclass(frozen=True)
class _Entry:
    run_id: str
    status: str
    started_at: str | None
    finished_at: str | None
    created_by: str | None
    owner_id: str | None
    error: str | None = None


class _FakeIndex:
    def __init__(self, entries: list[_Entry], *, fail: bool = False) -> None:
        self._entries = entries
        self._fail = fail
        self.requested_limit: int | None = None

    def list_builds(self, *, limit: int) -> list[_Entry]:
        if self._fail:
            raise RuntimeError("index unavailable")
        self.requested_limit = limit
        return self._entries[:limit]


@dataclass(frozen=True)
class _Job:
    """Shaped like BuildJobSnapshot: created_at/updated_at, not started/finished."""

    run_id: str
    status: str
    created_at: str
    updated_at: str
    owner_id: str | None
    error: str | None = None


class _FakeAsyncBuilds:
    def __init__(self, jobs: list[_Job]) -> None:
        self._jobs = jobs

    def list_all(self) -> list[_Job]:
        return list(self._jobs)


class _FakeService:
    def __init__(self, index: _FakeIndex, jobs: list[_Job] | None = None) -> None:
        self._build_index = index
        self._async_builds = _FakeAsyncBuilds(jobs or [])


def _service(
    entries: list[_Entry] | None = None,
    *,
    fail: bool = False,
    jobs: list[_Job] | None = None,
) -> Any:
    rows = (
        entries
        if entries is not None
        else [
            _Entry(
                "run-a",
                "succeeded",
                "2026-09-27T00:00:00Z",
                "2026-09-27T00:01:00Z",
                "oidc:aaa",
                compute_owner_id("oidc", "https://idp", "alice"),
            ),
            _Entry(
                "run-b",
                "failed",
                "2026-09-27T00:02:00Z",
                "2026-09-27T00:03:00Z",
                "oidc:bbb",
                compute_owner_id("oidc", "https://idp", "bob"),
            ),
        ]
    )
    return cast(Any, _FakeService(_FakeIndex(rows, fail=fail), jobs))


_ADMIN = Principal(kind="oidc", identifier="admin123", owner_id="oidc:admin", is_admin=True)
_USER = Principal(kind="oidc", identifier="user1234", owner_id="oidc:user")


def _call(service: Any, path: str, principal: Principal, query: str = "") -> Any:
    return admin.route(service, "GET", path, None, query, principal)


class TestAccessGate:
    @pytest.mark.parametrize("path", ["/admin/runs", "/admin/config"])
    def test_non_admin_gets_403(self, path: str) -> None:
        response = _call(_service(), path, _USER)
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 403

    @pytest.mark.parametrize("path", ["/admin/runs", "/admin/config"])
    def test_admin_gets_200(self, path: str) -> None:
        response = _call(_service(), path, _ADMIN)
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200

    def test_non_admin_response_does_not_leak_whether_data_exists(self) -> None:
        """403 response does not reveal run count or existence."""
        populated = _call(_service(), "/admin/runs", _USER)
        empty = _call(_service([]), "/admin/runs", _USER)
        assert populated.body == empty.body

    def test_non_get_methods_are_not_routed(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert admin.route(_service(), method, "/admin/runs", None, "", _ADMIN) is None

    def test_non_admin_paths_are_passed_through(self) -> None:
        assert _call(_service(), "/builds", _ADMIN) is None
        assert _call(_service(), "/healthz", _ADMIN) is None

    def test_unknown_admin_path_is_not_a_silent_200(self) -> None:
        assert _call(_service(), "/admin/nope", _ADMIN) is None


class TestMetadataOnly:
    """(a) Metadata only — narrowest scope until #679 decision."""

    def test_runs_response_carries_no_artifact_bytes(self) -> None:
        response = _call(_service(), "/admin/runs", _ADMIN)
        assert not isinstance(response, FileResponse)
        for run in response.body["runs"]:
            assert set(run) <= {
                "run_id",
                "status",
                "started_at",
                "finished_at",
                "owner_id",
                "error",
            }

    @pytest.mark.parametrize("path", ["/admin/runs", "/admin/config"])
    def test_admin_routes_never_return_files(self, path: str) -> None:
        """Admin path does not serve files — if it becomes artifact download path
        metadata-only property silently disappears."""
        assert not isinstance(_call(_service(), path, _ADMIN), FileResponse)

    def test_runs_response_omits_created_by(self) -> None:
        """``created_by`` is display label so user identity may be revealed.
        Owner distinction is sufficient with irreversible ``owner_id`` hash."""
        response = _call(_service(), "/admin/runs", _ADMIN)
        for run in response.body["runs"]:
            assert "created_by" not in run

    def test_the_failure_reason_is_given_with_keys_masked(self) -> None:
        """#679 (a): status, times, owner and why it failed — never a key."""
        entry = _Entry(
            "run-f",
            "failed",
            "2026-09-27T00:00:00Z",
            "2026-09-27T00:01:00Z",
            "oidc:aaa",
            "hash-a",
            error="upstream 500 for https://api.example/x?serviceKey=canary679&pageNo=1",
        )

        (run,) = _call(_service([entry]), "/admin/runs", _ADMIN).body["runs"]

        assert "canary679" not in run["error"]
        assert "upstream 500" in run["error"]

    def test_config_reports_state_not_values(self) -> None:
        response = _call(_service(), "/admin/config", _ADMIN)
        assert set(response.body) == {
            "enforce_ownership",
            "publish_server_credential_fallback",
        }
        for value in response.body.values():
            assert isinstance(value, bool)


class TestOwnershipIsNotWidened:
    """OIDC admin does not pass ``ownership_allows``.

    Passing it allows admin to receive bytes of others' run outputs, which
    would settle #679(c) without decision.
    """

    def test_oidc_admin_does_not_gain_blanket_run_access(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
        assert (
            ownership_module.ownership_allows(
                created_by="oidc:someone",
                owner_id=compute_owner_id("oidc", "https://idp", "someone-else"),
                principal=_ADMIN,
            )
            is False
        )

    @pytest.mark.parametrize("kind", ["dev", "service"])
    def test_grandfathered_principals_keep_full_access(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#679 pre-existing permission. Removing breaks single-user deployment and API key-based
        Studio deployment breaks."""
        monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
        principal = Principal(kind=kind, owner_id=f"{kind}:x", is_admin=True)
        assert (
            ownership_module.ownership_allows(
                created_by="oidc:someone",
                owner_id=compute_owner_id("oidc", "https://idp", "someone-else"),
                principal=principal,
            )
            is True
        )

    def test_ordinary_user_still_reaches_own_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
        owner = compute_owner_id("oidc", "https://idp", "alice")
        principal = Principal(kind="oidc", identifier="alice123", owner_id=owner)
        assert (
            ownership_module.ownership_allows(
                created_by="oidc:alice", owner_id=owner, principal=principal
            )
            is True
        )


class TestInFlightRuns:
    """BuildIndex is only written once a manifest exists, so queued and running
    jobs are absent from it -- and a stuck run is what an operator looks for."""

    def test_a_running_job_appears(self) -> None:
        jobs = [
            _Job("run-live", "running", "2026-09-27T01:00:00Z", "2026-09-27T01:00:30Z", "oidc:c")
        ]
        response = _call(_service(jobs=jobs), "/admin/runs", _ADMIN)
        ids = {run["run_id"] for run in response.body["runs"]}
        assert "run-live" in ids
        assert {"run-a", "run-b"} <= ids

    def test_a_running_job_has_no_invented_timestamps(self) -> None:
        """Registry snapshots contain neither a real start nor finish timestamp."""
        jobs = [_Job("run-live", "running", "2026-09-27T01:00:00Z", "2026-09-27T01:00:30Z", None)]
        response = _call(_service(jobs=jobs), "/admin/runs", _ADMIN)
        live = next(r for r in response.body["runs"] if r["run_id"] == "run-live")
        assert live["finished_at"] is None
        assert live["started_at"] is None

    def test_a_queued_job_has_no_started_at(self) -> None:
        """created_at is when the request was accepted, not when the run began."""
        jobs = [_Job("run-wait", "queued", "2026-09-27T01:00:00Z", "2026-09-27T01:00:00Z", None)]
        response = _call(_service(jobs=jobs), "/admin/runs", _ADMIN)
        waiting = next(r for r in response.body["runs"] if r["run_id"] == "run-wait")
        assert waiting["started_at"] is None
        assert waiting["finished_at"] is None

    def test_a_terminal_job_keeps_its_finished_at(self) -> None:
        jobs = [_Job("run-done", "failed", "2026-09-27T01:00:00Z", "2026-09-27T01:02:00Z", None)]
        response = _call(_service(jobs=jobs), "/admin/runs", _ADMIN)
        done = next(r for r in response.body["runs"] if r["run_id"] == "run-done")
        assert done["started_at"] is None
        assert done["finished_at"] == "2026-09-27T01:02:00Z"

    def test_the_index_entry_wins_for_the_same_run(self) -> None:
        """A terminal index row is more recent than the registry's snapshot."""
        jobs = [_Job("run-a", "running", "2026-09-27T00:00:00Z", "2026-09-27T00:00:10Z", None)]
        response = _call(_service(jobs=jobs), "/admin/runs", _ADMIN)
        rows = [r for r in response.body["runs"] if r["run_id"] == "run-a"]
        assert len(rows) == 1
        assert rows[0]["status"] == "succeeded"

    def test_a_queued_job_is_not_sorted_away_by_its_empty_started_at(self) -> None:
        """A queued job reports no started_at, but it is the newest thing in the
        list. Ordering on the empty value would push it behind every finished run
        and the limit would cut it first -- the entry added to make in-flight runs
        visible would disappear."""
        jobs = [_Job("run-new", "queued", "2026-09-27T09:00:00Z", "2026-09-27T09:00:00Z", None)]
        response = _call(_service(jobs=jobs), "/admin/runs", _ADMIN, "limit=1")
        assert [run["run_id"] for run in response.body["runs"]] == ["run-new"]

    def test_the_limit_still_caps_the_merged_list(self) -> None:
        jobs = [_Job(f"j{i}", "queued", f"2026-09-27T02:{i:02d}:00Z", "x", None) for i in range(10)]
        response = _call(_service(jobs=jobs), "/admin/runs", _ADMIN, "limit=3")
        assert response.body["count"] == 3

    def test_index_rows_remain_ordered_by_finished_at(self) -> None:
        entries = [
            _Entry(
                "finished-new",
                "succeeded",
                "2026-09-27T01:00:00Z",
                "2026-09-27T03:00:00Z",
                None,
                None,
            ),
            _Entry(
                "started-new",
                "succeeded",
                "2026-09-27T02:00:00Z",
                "2026-09-27T02:30:00Z",
                None,
                None,
            ),
        ]
        response = _call(_service(entries), "/admin/runs", _ADMIN, "limit=2")
        assert [run["run_id"] for run in response.body["runs"]] == [
            "finished-new",
            "started-new",
        ]


class TestIndexFailure:
    def test_index_failure_returns_503_not_a_partial_list(self) -> None:
        """Does not fall back to filesystem — fallback has less owner info
        It's accurate, but if admin treats it as fact, they judge based on wrong premise."""
        response = _call(_service(fail=True), "/admin/runs", _ADMIN)
        assert response.status_code == 503
        assert "runs" not in response.body


class TestLimit:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("", 50),
            ("limit=10", 10),
            ("limit=99999", 200),
            ("limit=0", 1),
            ("limit=-5", 1),
            ("limit=abc", 50),
            ("limit=3&limit=7", 7),
        ],
    )
    def test_limit_is_clamped(self, query: str, expected: int) -> None:
        service = _service()
        _call(service, "/admin/runs", _ADMIN, query)
        assert service._build_index.requested_limit == expected


class TestAudit:
    def test_allowed_action_is_recorded(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="kpubdata_builder.admin_audit"):
            _call(_service(), "/admin/runs", _ADMIN)
        assert "admin.runs.list" in caplog.text
        assert "outcome=allowed" in caplog.text

    def test_denied_action_is_recorded(self, caplog: pytest.LogCaptureFixture) -> None:
        """Denials also logged — who touched admin paths as important as allowed requests."""
        with caplog.at_level(logging.INFO, logger="kpubdata_builder.admin_audit"):
            _call(_service(), "/admin/config", _USER)
        assert "outcome=denied" in caplog.text

    def test_audit_line_shape_is_pinned(self, caplog: pytest.LogCaptureFixture) -> None:
        """Fixes what goes into one audit line.

        Only ``label`` and ``owner_id`` extracted from ``Principal``, both by
        design contain no secrets. When someone later adds a field for "debug
        convenience", this test catches it — that field might carry credentials.
        """
        principal = Principal(
            kind="service", identifier="apikey:ci", owner_id="service:abcd", is_admin=True
        )
        with caplog.at_level(logging.INFO, logger="kpubdata_builder.admin_audit"):
            record_admin_action(principal, "admin.runs.list", target="limit=50")
        assert len(caplog.records) == 1
        assert caplog.records[0].getMessage() == (
            "admin action: actor=service:apikey:ci owner_id=service:abcd "
            "action=admin.runs.list target=limit=50 outcome=allowed"
        )

    def test_audit_records_are_emitted_at_the_default_threshold(self) -> None:
        """The service configures no logging at all, so the root threshold is
        WARNING and an INFO audit record would be discarded entirely. An audit
        trail that silently vanishes is worse than none -- it makes you believe
        there is one."""
        from kpubdata_builder.service import admin_audit

        assert admin_audit._audit_logger.isEnabledFor(logging.INFO)

    def test_audit_output_exists_without_deployment_configuration(self) -> None:
        """Somewhere up the chain there has to be a handler, or the record goes
        nowhere even when the level allows it."""
        from kpubdata_builder.service import admin_audit

        admin_audit._ensure_audit_output()
        logger: logging.Logger | None = admin_audit._audit_logger
        while logger is not None:
            if logger.handlers:
                return
            if not logger.propagate:
                break
            logger = logger.parent
        pytest.fail("no handler anywhere on the audit logger chain")

    def test_the_fallback_decision_is_deferred_to_the_first_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deciding at import time would attach a stderr handler before the host
        had a chance to configure logging, and every record would then be
        written twice."""
        from kpubdata_builder.service import admin_audit

        monkeypatch.setattr(admin_audit, "_fallback_handler", None)
        monkeypatch.setattr(admin_audit._audit_logger, "handlers", [])
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [logging.NullHandler()])

        record_admin_action(
            Principal(kind="dev", owner_id="dev:x", is_admin=True), "admin.config.read"
        )

        assert admin_audit._audit_logger.handlers == []
        assert admin_audit._fallback_handler is None

    def test_ensuring_output_twice_does_not_duplicate_handlers(self) -> None:
        """Duplicated handlers would write each audit record more than once,
        which makes the records uncountable."""
        from kpubdata_builder.service import admin_audit

        before = len(admin_audit._audit_logger.handlers)
        admin_audit._ensure_audit_output()
        assert len(admin_audit._audit_logger.handlers) == before

    def test_missing_target_renders_as_placeholder(self, caplog: pytest.LogCaptureFixture) -> None:
        principal = Principal(kind="dev", owner_id="dev:x", is_admin=True)
        with caplog.at_level(logging.INFO, logger="kpubdata_builder.admin_audit"):
            record_admin_action(principal, "admin.config.read")
        assert "target=-" in caplog.records[0].getMessage()
