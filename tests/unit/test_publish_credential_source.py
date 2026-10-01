"""GET /version tells a client where this deployment takes publish credentials (#938).

A non-admin client could not tell a multi-user deployment, which takes a publish token
only from the request's ``X-Publish-Credential`` header (#925), from a single-user one
with ``REQUIRE_OWN_PUBLISH_CREDENTIAL`` on, which ignores that header and wants a stored
credential (#635). Both report ``credential_required``, so a client had to send a token
once to find out. ``publish_credential`` says it up front, and it must agree with what
readiness then does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.publish_credentials import publish_credential_source

from .test_service import _FakeClient
from .test_service_publish import LICENSED_SPEC_YAML

_REQUIRE_OWN = "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL"
_ALICE = Principal("oidc", "alice", "oidc:alice")


@pytest.fixture(autouse=True)
def _single_user_with_a_server_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ENFORCE_OWNERSHIP", "OIDC_ISSUER", _REQUIRE_OWN, "KPUBDATA_BUILDER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    monkeypatch.setenv("HF_TOKEN", "hf_server_operator_938")


@pytest.fixture
def multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        terms_lookup=lambda _id: "allowed",
        publish_visibility_probe=lambda *_: "absent",
    )


def _version(tmp_path: Path) -> dict[str, object]:
    response = dispatch(_service(tmp_path), "GET", "/version", None)
    assert isinstance(response, ServiceResponse) and response.status_code == 200
    return response.body


class TestSingleUser:
    def test_by_default_the_stored_credential_then_the_server(self, tmp_path: Path) -> None:
        assert _version(tmp_path)["publish_credential"] == "stored_or_server"

    @pytest.mark.parametrize("value", ["true", "1", "TRUE"])
    def test_require_own_credential_means_stored_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(_REQUIRE_OWN, value)

        assert _version(tmp_path)["publish_credential"] == "stored"

    def test_require_own_credential_false_keeps_the_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_REQUIRE_OWN, "false")

        assert _version(tmp_path)["publish_credential"] == "stored_or_server"


class TestMultiUser:
    def test_the_request_header_only(self, tmp_path: Path, multi_user: None) -> None:
        assert _version(tmp_path)["publish_credential"] == "request"

    @pytest.mark.parametrize("value", ["true", "false"])
    def test_whatever_require_own_credential_says(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None, value: str
    ) -> None:
        monkeypatch.setenv(_REQUIRE_OWN, value)

        assert _version(tmp_path)["publish_credential"] == "request"

    def test_an_oidc_deployment_is_multi_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kpubdata_builder.service.ownership.oidc_enabled", lambda: True)

        assert publish_credential_source() == "request"


class TestWhatItDoesNotDisclose:
    """Negative: the policy is told, never whether a credential exists."""

    @pytest.mark.parametrize("multi", [False, True])
    def test_the_server_token_does_not_change_the_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool
    ) -> None:
        if multi:
            monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
        with_token = _version(tmp_path)["publish_credential"]
        monkeypatch.delenv("HF_TOKEN")
        without_token = _version(tmp_path)["publish_credential"]

        assert with_token == without_token
        assert "hf_server_operator_938" not in json.dumps(_version(tmp_path))

    def test_an_unauthenticated_caller_gets_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE")
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")

        response = dispatch(_service(tmp_path), "GET", "/version", None)

        assert isinstance(response, ServiceResponse) and response.status_code == 401
        assert "publish_credential" not in response.body

    def test_a_non_admin_api_key_caller_gets_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE")
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")

        response = dispatch(_service(tmp_path), "GET", "/version", None, api_key="secret")

        assert isinstance(response, ServiceResponse) and response.status_code == 200
        assert response.body["publish_credential"] == "stored_or_server"


def _credential_blockers(service: BuilderService) -> list[dict[str, str]]:
    readiness = dispatch(
        service, "GET", "/builds/r1/publish/readiness", None, query="target=huggingface"
    )
    assert isinstance(readiness, ServiceResponse) and readiness.status_code == 200
    blockers = cast(list[dict[str, str]], readiness.body["blockers"])
    return [b for b in blockers if b["code"].startswith("credential_")]


class TestItAgreesWithReadiness:
    """The answer is the policy readiness applies to a requester with nothing stored."""

    @pytest.fixture
    def service(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
        monkeypatch.setattr(app_module, "authenticate", lambda **_: _ALICE)
        service = _service(tmp_path)
        built = dispatch(service, "POST", "/build", {"spec": LICENSED_SPEC_YAML, "run_id": "r1"})
        assert isinstance(built, ServiceResponse) and built.status_code == 200, built.body
        return service

    def test_stored_or_server_uses_the_server_token(self, service: BuilderService) -> None:
        assert publish_credential_source() == "stored_or_server"
        assert _credential_blockers(service) == []

    def test_stored_refuses_without_a_stored_credential(
        self, service: BuilderService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_REQUIRE_OWN, "true")

        assert publish_credential_source() == "stored"
        [blocker] = _credential_blockers(service)
        assert blocker["code"] == "credential_required"
        assert "stored for this principal" in blocker["message"]

    def test_request_asks_for_the_header(
        self, service: BuilderService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")

        assert publish_credential_source() == "request"
        [blocker] = _credential_blockers(service)
        assert blocker["code"] == "credential_required"
        assert "X-Publish-Credential" in blocker["message"]
