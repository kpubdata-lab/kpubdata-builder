"""관리자 역할과 관리 엔드포인트 (#679).

여기서 가장 중요한 것은 **부정 테스트**다. 관리 기능의 위험은 동작하지 않는
것이 아니라 필요 이상으로 동작하는 것이다 — 이 제품은 BYOK 이고, 관리자가
사용자 데이터를 열 수 있는지는 아직 결정되지 않았다.
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


class _FakeService:
    def __init__(self, index: _FakeIndex) -> None:
        self._build_index = index


def _service(entries: list[_Entry] | None = None, *, fail: bool = False) -> Any:
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
    return cast(Any, _FakeService(_FakeIndex(rows, fail=fail)))


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
        """403 본문이 run 개수나 존재 여부를 알려주지 않는다."""
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
    """(a) 메타데이터만 — #679 의 결정이 내려질 때까지 가장 좁은 범위."""

    def test_runs_response_carries_no_artifact_bytes(self) -> None:
        response = _call(_service(), "/admin/runs", _ADMIN)
        assert not isinstance(response, FileResponse)
        for run in response.body["runs"]:
            assert set(run) <= {"run_id", "status", "started_at", "finished_at", "owner_id"}

    @pytest.mark.parametrize("path", ["/admin/runs", "/admin/config"])
    def test_admin_routes_never_return_files(self, path: str) -> None:
        """관리 경로는 파일을 돌려주지 않는다 — 산출물 다운로드 경로가 되면
        메타데이터 전용이라는 성질이 조용히 사라진다."""
        assert not isinstance(_call(_service(), path, _ADMIN), FileResponse)

    def test_runs_response_omits_created_by(self) -> None:
        """``created_by`` 는 표시용 라벨이라 사용자 신원이 드러날 수 있다.
        소유자 구분은 되돌릴 수 없는 ``owner_id`` 해시로 충분하다."""
        response = _call(_service(), "/admin/runs", _ADMIN)
        for run in response.body["runs"]:
            assert "created_by" not in run

    def test_config_reports_state_not_values(self) -> None:
        response = _call(_service(), "/admin/config", _ADMIN)
        assert set(response.body) == {
            "enforce_ownership",
            "publish_server_credential_fallback",
        }
        for value in response.body.values():
            assert isinstance(value, bool)


class TestOwnershipIsNotWidened:
    """OIDC 관리자는 ``ownership_allows`` 를 통과하지 않는다.

    통과시키면 관리자가 남의 run 산출물 바이트를 받을 수 있고, 그것은 #679 의
    (c) 를 결정 없이 확정하는 것이다.
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
        """#679 이전부터 있던 권한이다. 빼면 단일 사용자 배포와 API 키 기반
        Studio 배포가 깨진다."""
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


class TestIndexFailure:
    def test_index_failure_returns_503_not_a_partial_list(self) -> None:
        """파일시스템 폴백으로 내려가지 않는다 — 폴백은 소유자 정보가 덜
        정확한데, 관리자가 그것을 사실로 보면 잘못된 근거로 판단한다."""
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
        """거부도 남는다 — 누가 관리 경로를 두드렸는지가 허용된 요청만큼 중요하다."""
        with caplog.at_level(logging.INFO, logger="kpubdata_builder.admin_audit"):
            _call(_service(), "/admin/config", _USER)
        assert "outcome=denied" in caplog.text

    def test_audit_line_shape_is_pinned(self, caplog: pytest.LogCaptureFixture) -> None:
        """감사 한 줄에 무엇이 들어가는지를 고정한다.

        ``Principal`` 에서 꺼내는 것은 ``label`` 과 ``owner_id`` 뿐이고 둘 다
        설계상 secret 을 담지 않는다. 나중에 누군가 "디버깅에 편하다" 며 필드를
        더할 때 이 테스트가 걸린다 — 그 필드가 credential 을 실어 나를 수 있다.
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

    def test_missing_target_renders_as_placeholder(self, caplog: pytest.LogCaptureFixture) -> None:
        principal = Principal(kind="dev", owner_id="dev:x", is_admin=True)
        with caplog.at_level(logging.INFO, logger="kpubdata_builder.admin_audit"):
            record_admin_action(principal, "admin.config.read")
        assert "target=-" in caplog.records[0].getMessage()
