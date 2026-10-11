"""Path segments are decoded once, after routing, for every parameterised route (#1277)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal, compute_owner_id
from kpubdata_builder.service.routes._parsing import decode_segment

_ISSUER = "https://idp.example"


def _principal(sub: str, *, admin: bool = False) -> Principal:
    return Principal(
        kind="oidc",
        identifier=sub[:8],
        owner_id=compute_owner_id("oidc", _ISSUER, sub),
        is_admin=admin,
        admitted=True,
        display_name=f"{sub}@example.com",
    )


_ADMIN = _principal("admin", admin=True)
_NEWCOMER = Principal(
    kind="oidc",
    identifier="newcomer",
    owner_id=compute_owner_id("oidc", _ISSUER, "newcomer"),
    is_admin=False,
    admitted=False,
    display_name="newcomer@example.com",
)


def _call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, principal: Principal, method: str, path: str
) -> ServiceResponse:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: object())
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principal)
    response = dispatch(service, method, path, None, "")
    assert isinstance(response, ServiceResponse)
    return response


def _encoded(owner_id: str) -> str:
    return owner_id.replace(":", "%3A")


def test_decode_segment_decodes_exactly_once() -> None:
    assert decode_segment("oidc%3Aabc") == "oidc:abc"
    assert decode_segment("oidc%253Aabc") == "oidc%3Aabc"


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("encode", [True, False])
def test_a_decision_accepts_the_encoded_and_the_raw_colon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, encode: bool
) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: object())
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _NEWCOMER)
    dispatch(service, "GET", "/builds", None, "")  # records the newcomer as pending
    user_id = _NEWCOMER.owner_id
    assert user_id is not None
    segment = _encoded(user_id) if encode else user_id
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _ADMIN)
    response = dispatch(service, "POST", f"/admin/users/{segment}/{action}", None, "")
    assert isinstance(response, ServiceResponse)
    assert response.status_code == 200
    assert response.body["user_id"] == user_id


def test_a_double_encoded_user_id_is_not_decoded_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = _NEWCOMER.owner_id
    assert user_id is not None
    response = _call(
        tmp_path,
        monkeypatch,
        _ADMIN,
        "POST",
        f"/admin/users/{user_id.replace(':', '%253A')}/approve",
    )
    assert response.status_code == 404


def test_an_encoded_slash_does_not_change_which_route_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "abc%2Fmanifest" is one segment: the run-status route, not the manifest route.
    response = _call(tmp_path, monkeypatch, _ADMIN, "GET", "/builds/abc%2Fmanifest")
    assert response.status_code == 400
    assert "run_id" in str(response.body["error"])
    response = _call(tmp_path, monkeypatch, _ADMIN, "POST", "/admin/users/a%2Fapprove/approve")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/builds/run%2D1", "/builds/run%2D1/manifest", "/builds/run%2D1/stages", "/uploads/upl%5F1"],
)
def test_other_parameterised_routes_decode_their_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    response = _call(tmp_path, monkeypatch, _ADMIN, "GET", path)
    assert response.status_code == 404
    assert "%" not in str(response.body)
