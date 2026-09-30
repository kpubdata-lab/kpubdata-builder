"""The Builder sign-up ledger (#785, option B of ADR 0012's 2026-09-30 amendment)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal, compute_owner_id, validate_oidc_config
from kpubdata_builder.spec import JsonValue

_ISSUER = "https://idp.example"


def _user(sub: str, *, admitted: bool = False, admin: bool = False) -> Principal:
    return Principal(
        kind="oidc",
        identifier=sub[:8],
        owner_id=compute_owner_id("oidc", _ISSUER, sub),
        is_admin=admin,
        admitted=admitted or admin,
        display_name=f"{sub}@example.com",
    )


_NEWCOMER = _user("newcomer")
_LISTED = _user("listed", admitted=True)
_ADMIN = _user("admin", admin=True)


def _service(tmp_path: Path) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: object())


def _as(
    service: BuilderService,
    monkeypatch: pytest.MonkeyPatch,
    principal: Principal,
    method: str,
    path: str,
    query: str = "",
) -> ServiceResponse:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principal)
    response = dispatch(service, method, path, None, query)
    assert isinstance(response, ServiceResponse)
    return response


def _code(response: ServiceResponse) -> JsonValue:
    return cast(dict[str, JsonValue], response.body).get("code")


def test_a_newcomer_waits_until_approved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative: a pending user can neither build nor read."""
    service = _service(tmp_path)

    for method, path in (("GET", "/builds"), ("GET", "/datasets"), ("POST", "/query")):
        response = _as(service, monkeypatch, _NEWCOMER, method, path)
        assert (response.status_code, _code(response)) == (403, "signup_pending")

    approved = _as(
        service, monkeypatch, _ADMIN, "POST", f"/admin/users/{_NEWCOMER.owner_id}/approve"
    )
    assert approved.status_code == 200
    assert approved.body["status"] == "approved"
    assert approved.body["decided_by"] == _ADMIN.owner_id

    assert _as(service, monkeypatch, _NEWCOMER, "GET", "/builds").status_code == 200


def test_rejection_shuts_out_even_a_listed_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: blocking works without a restart, whatever the environment list says."""
    service = _service(tmp_path)
    assert _as(service, monkeypatch, _LISTED, "GET", "/builds").status_code == 200

    rejected = _as(service, monkeypatch, _ADMIN, "POST", f"/admin/users/{_LISTED.owner_id}/reject")
    blocked = _as(service, monkeypatch, _LISTED, "GET", "/builds")

    assert rejected.body["status"] == "rejected"
    assert (blocked.status_code, _code(blocked)) == (403, "signup_rejected")


def test_a_listed_user_is_recorded_as_approved_by_the_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    _as(service, monkeypatch, _NEWCOMER, "GET", "/builds")
    _as(service, monkeypatch, _LISTED, "GET", "/builds")

    users = _as(service, monkeypatch, _ADMIN, "GET", "/admin/users")
    by_id = {u["user_id"]: u for u in cast(list[dict[str, JsonValue]], users.body["users"])}

    assert by_id[_LISTED.owner_id]["status"] == "approved"
    assert by_id[_LISTED.owner_id]["decided_by"] == "allowlist"
    assert by_id[_NEWCOMER.owner_id]["status"] == "pending"
    pending = _as(service, monkeypatch, _ADMIN, "GET", "/admin/users", "status=pending")
    assert [u["user_id"] for u in cast(list[dict[str, JsonValue]], pending.body["users"])] == [
        _NEWCOMER.owner_id
    ]


def test_a_pending_user_put_on_a_list_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    assert _as(service, monkeypatch, _NEWCOMER, "GET", "/builds").status_code == 403
    now_listed = _user("newcomer", admitted=True)

    assert _as(service, monkeypatch, now_listed, "GET", "/builds").status_code == 200


def test_the_ledger_holds_no_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    _as(service, monkeypatch, _NEWCOMER, "GET", "/builds")

    users = _as(service, monkeypatch, _ADMIN, "GET", "/admin/users")
    (entry,) = [
        u
        for u in cast(list[dict[str, JsonValue]], users.body["users"])
        if u["user_id"] == _NEWCOMER.owner_id
    ]

    assert set(entry) == {
        "user_id",
        "display_name",
        "status",
        "first_seen_at",
        "last_seen_at",
        "decided_at",
        "decided_by",
    }
    assert entry["display_name"] == "newcomer@example.com"
    assert "newcomer" not in cast(str, entry["user_id"])


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/users"),
        ("POST", "/admin/users/someone/approve"),
        ("POST", "/admin/users/someone/reject"),
    ],
)
def test_non_administrators_are_refused_and_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
) -> None:
    service = _service(tmp_path)
    with caplog.at_level(logging.INFO):
        response = _as(service, monkeypatch, _LISTED, method, path)

    assert response.status_code == 403
    assert any("denied" in r.getMessage() or "denied" in str(r.__dict__) for r in caplog.records)


def test_decisions_are_audited_and_unknown_users_are_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service(tmp_path)
    _as(service, monkeypatch, _NEWCOMER, "GET", "/builds")

    with caplog.at_level(logging.INFO):
        _as(service, monkeypatch, _ADMIN, "POST", f"/admin/users/{_NEWCOMER.owner_id}/approve")
    missing = _as(service, monkeypatch, _ADMIN, "POST", "/admin/users/nobody/approve")

    assert any("admin.users.approve" in str(r.__dict__) for r in caplog.records)
    assert (missing.status_code, _code(missing)) == (404, "user_not_found")


def test_an_administrator_is_never_locked_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    _as(service, monkeypatch, _ADMIN, "GET", "/builds")
    _as(service, monkeypatch, _ADMIN, "POST", f"/admin/users/{_ADMIN.owner_id}/reject")

    assert _as(service, monkeypatch, _ADMIN, "GET", "/builds").status_code == 200


def test_a_deployment_without_oidc_never_touches_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service_principal = Principal(kind="service", owner_id="svc")

    assert _as(service, monkeypatch, service_principal, "GET", "/builds").status_code == 200
    assert not (tmp_path / ".service" / "users.sqlite3").exists()


def test_startup_accepts_an_administrator_instead_of_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", "aud")
    for name in ("OIDC_ALLOWED_HD", "OIDC_ALLOWED_SUBJECTS", "OIDC_ALLOWED_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", f"{_ISSUER}|admin")

    validate_oidc_config()

    monkeypatch.delenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS")
    with pytest.raises(RuntimeError, match="no administrator"):
        validate_oidc_config()
