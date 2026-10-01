"""In a multi-user deployment a publish token lives only as long as its request (#925).

ADR 0020 item 2, confirmed by the owner on 2026-10-01: a Hugging Face or Kaggle publish
token is a key under the same rule as a provider key (#683). It comes only from the
request's ``X-Publish-Credential`` header; nothing stored is read, and the server's
``HF_TOKEN`` / ``KAGGLE_*`` never serve anyone, whatever
``REQUIRE_OWN_PUBLISH_CREDENTIAL`` says. A single-user deployment is unchanged.
"""

from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
import kpubdata_builder.service.publish_api as publish_api_module
from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.publish_credentials import (
    _slot,
    current_publish_credential,
    parse_publish_credential_headers,
    request_scope,
    resolve_publish_credentials,
    server_fallback_allowed,
)

from .test_service import _FakeClient
from .test_service_publish import LICENSED_SPEC_YAML, _SpyPublisher

_REQUEST_TOKEN = "hf_test_token_925"
_STORED_TOKEN = "hf_stored_before_switch_925"
_SERVER_TOKEN = "hf_server_operator_925"
_ALICE = Principal("oidc", "alice", "oidc:alice")
_HF_SLOT = _slot("huggingface", "HF_TOKEN")


class _UntouchableRepo:
    """A repository that fails the test if anything reads it."""

    def get_secret(self, owner_id: str, provider: str) -> str | None:
        raise AssertionError("a multi-user publish must not read the credential store")


@pytest.fixture
def multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")


@pytest.fixture(autouse=True)
def _server_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's tokens are always present: the point is that they are not used."""
    monkeypatch.setenv("HF_TOKEN", _SERVER_TOKEN)
    monkeypatch.setenv("KAGGLE_USERNAME", "operator")
    monkeypatch.setenv("KAGGLE_KEY", "kaggle_server_key_925")
    monkeypatch.delenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", raising=False)


def _repository(tmp_path: Path) -> SQLiteCredentialRepository:
    repository = SQLiteCredentialRepository(
        tmp_path / "credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
    )
    repository.put(cast(str, _ALICE.owner_id), _HF_SLOT, _STORED_TOKEN)
    return repository


def _files_holding(root: Path, needle: str) -> list[Path]:
    return [
        path for path in root.rglob("*") if path.is_file() and needle.encode() in path.read_bytes()
    ]


# ---------------------------------------------------------------- the header


class TestParsing:
    def test_names_are_matched_without_case(self) -> None:
        assert parse_publish_credential_headers(
            [f"hf_token={_REQUEST_TOKEN}", "KAGGLE_USERNAME=me, kaggle_key=kaggle_test_key"]
        ) == {
            "HF_TOKEN": _REQUEST_TOKEN,
            "KAGGLE_USERNAME": "me",
            "KAGGLE_KEY": "kaggle_test_key",
        }

    @pytest.mark.parametrize(
        "header",
        [
            _REQUEST_TOKEN,
            f"={_REQUEST_TOKEN}",
            "HF_TOKEN=",
            f"DATAGO_KEY={_REQUEST_TOKEN}",
            f"HF_TOKEN={_REQUEST_TOKEN},HF_TOKEN=hf_other_token",
        ],
    )
    def test_a_bad_header_is_refused_without_echoing_a_value(self, header: str) -> None:
        with pytest.raises(ValueError) as caught:
            parse_publish_credential_headers([header])
        assert _REQUEST_TOKEN not in str(caught.value)
        assert "hf_other_token" not in str(caught.value)

    def test_the_value_is_forgotten_when_the_request_ends(self) -> None:
        with request_scope({"HF_TOKEN": _REQUEST_TOKEN}):
            assert current_publish_credential("HF_TOKEN") == _REQUEST_TOKEN
        assert current_publish_credential("HF_TOKEN") is None


# ---------------------------------------------------------------- resolution


class TestMultiUserResolution:
    def test_no_request_token_is_refused_despite_a_stored_and_a_server_token(
        self, tmp_path: Path, multi_user: None
    ) -> None:
        """Negative: a stored slot and the server HF_TOKEN both exist; neither is used."""
        resolution = resolve_publish_credentials(
            _repository(tmp_path), _ALICE.owner_id, "huggingface"
        )

        assert resolution.refused and resolution.request_only
        assert dict(resolution.values) == {}

    def test_the_store_is_never_read(self, multi_user: None) -> None:
        resolve_publish_credentials(_UntouchableRepo(), _ALICE.owner_id, "huggingface")  # type: ignore[arg-type]
        with request_scope({"HF_TOKEN": _REQUEST_TOKEN}):
            resolve_publish_credentials(_UntouchableRepo(), _ALICE.owner_id, "huggingface")  # type: ignore[arg-type]

    def test_the_server_fallback_is_closed_whatever_the_switch_says(
        self, monkeypatch: pytest.MonkeyPatch, multi_user: None
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", "false")

        assert server_fallback_allowed() is False
        resolution = resolve_publish_credentials(None, None, "huggingface")
        assert resolution.refused and dict(resolution.values) == {}

    def test_a_request_token_is_used(self, tmp_path: Path, multi_user: None) -> None:
        with request_scope({"HF_TOKEN": _REQUEST_TOKEN}):
            resolution = resolve_publish_credentials(
                _repository(tmp_path), _ALICE.owner_id, "huggingface"
            )

        assert dict(resolution.values) == {"HF_TOKEN": _REQUEST_TOKEN}
        assert not resolution.refused

    def test_half_a_kaggle_pair_is_refused_not_completed_from_the_server(
        self, multi_user: None
    ) -> None:
        with request_scope({"KAGGLE_USERNAME": "me"}):
            resolution = resolve_publish_credentials(None, _ALICE.owner_id, "kaggle")

        assert resolution.refused and dict(resolution.values) == {}

    def test_a_whole_kaggle_pair_is_used(self, multi_user: None) -> None:
        pair = {"KAGGLE_USERNAME": "me", "KAGGLE_KEY": "kaggle_test_key"}
        with request_scope(pair):
            resolution = resolve_publish_credentials(None, _ALICE.owner_id, "kaggle")

        assert dict(resolution.values) == pair

    def test_local_still_needs_nothing(self, multi_user: None) -> None:
        assert resolve_publish_credentials(None, _ALICE.owner_id, "local").not_required


class TestSingleUserIsUnchanged:
    def test_the_stored_token_still_wins(self, tmp_path: Path) -> None:
        resolution = resolve_publish_credentials(
            _repository(tmp_path), _ALICE.owner_id, "huggingface"
        )
        assert dict(resolution.values) == {"HF_TOKEN": _STORED_TOKEN}

    def test_the_server_token_is_still_the_fallback(self) -> None:
        assert server_fallback_allowed() is True
        assert dict(resolve_publish_credentials(None, None, "huggingface").values) == {
            "HF_TOKEN": _SERVER_TOKEN
        }

    def test_the_header_is_ignored(self, tmp_path: Path) -> None:
        with request_scope({"HF_TOKEN": _REQUEST_TOKEN}):
            resolution = resolve_publish_credentials(
                _repository(tmp_path), _ALICE.owner_id, "huggingface"
            )
        assert dict(resolution.values) == {"HF_TOKEN": _STORED_TOKEN}

    def test_the_reconcile_probe_still_reads_the_server_token(self) -> None:
        assert publish_api_module._probe_token() == _SERVER_TOKEN


# ---------------------------------------------------------------- over HTTP


def _service(tmp_path: Path, repository: SQLiteCredentialRepository) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    (tmp_path / "out").mkdir()
    return BuilderService(
        output_root=tmp_path / "out",
        client_factory=lambda **_: client,
        credential_repository=repository,
        terms_lookup=lambda _id: "allowed",
        publish_visibility_probe=lambda *_: "absent",
    )


def _blocker_codes(response: ServiceResponse) -> list[str]:
    return [cast(str, b["code"]) for b in cast(list[dict[str, object]], response.body["blockers"])]


class TestMultiUserPublishOverHttp:
    @pytest.fixture
    def setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
    ) -> tuple[BuilderService, _SpyPublisher, SQLiteCredentialRepository]:
        monkeypatch.setattr(app_module, "authenticate", lambda **_: _ALICE)
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", spy)
        repository = _repository(tmp_path)
        service = _service(tmp_path, repository)
        built = dispatch(service, "POST", "/build", {"spec": LICENSED_SPEC_YAML, "run_id": "r1"})
        assert isinstance(built, ServiceResponse) and built.status_code == 200, built.body
        return service, spy, repository

    def test_without_a_request_token_publish_fails_closed(
        self, setup: tuple[BuilderService, _SpyPublisher, SQLiteCredentialRepository]
    ) -> None:
        """Negative: stored token and server HF_TOKEN present, header absent."""
        service, spy, _ = setup

        readiness = dispatch(
            service, "GET", "/builds/r1/publish/readiness", None, query="target=huggingface"
        )
        response = dispatch(
            service,
            "POST",
            "/builds/r1/publish",
            {"target": "huggingface", "destination": "alice/air"},
        )

        assert isinstance(readiness, ServiceResponse)
        assert readiness.body["ready"] is False
        assert "credential_required" in _blocker_codes(readiness)
        assert isinstance(response, ServiceResponse) and response.status_code == 409
        assert "credential_required" in _blocker_codes(response)
        assert "X-Publish-Credential" in json.dumps(response.body)
        assert spy.calls == []

    def test_the_request_token_publishes_and_is_not_kept(
        self,
        setup: tuple[BuilderService, _SpyPublisher, SQLiteCredentialRepository],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        service, spy, repository = setup
        caplog.set_level(logging.DEBUG)

        response = dispatch(
            service,
            "POST",
            "/builds/r1/publish",
            {"target": "huggingface", "destination": "alice/air"},
            publish_credential_headers=[f"HF_TOKEN={_REQUEST_TOKEN}"],
        )

        assert isinstance(response, ServiceResponse) and response.status_code == 200, response.body
        assert len(spy.calls) == 1
        assert spy.calls[0][1]["credentials"] == {"HF_TOKEN": _REQUEST_TOKEN}
        # Never returned, logged or written anywhere — receipts, audit, run files.
        assert _REQUEST_TOKEN not in json.dumps(response.body)
        assert _REQUEST_TOKEN not in caplog.text
        assert _files_holding(tmp_path, _REQUEST_TOKEN) == []
        # The store is left as it was: nothing saved, the old slot not overwritten.
        assert repository.get_secret(cast(str, _ALICE.owner_id), _HF_SLOT) == _STORED_TOKEN
        assert current_publish_credential("HF_TOKEN") is None

    def test_a_malformed_header_answers_400_without_the_value(
        self, setup: tuple[BuilderService, _SpyPublisher, SQLiteCredentialRepository]
    ) -> None:
        service, spy, _ = setup

        response = dispatch(
            service,
            "POST",
            "/builds/r1/publish",
            {"target": "huggingface", "destination": "alice/air"},
            publish_credential_headers=[f"TOKEN={_REQUEST_TOKEN}"],
        )

        assert isinstance(response, ServiceResponse) and response.status_code == 400
        assert response.body["code"] == "invalid_publish_credential"
        assert _REQUEST_TOKEN not in json.dumps(response.body)
        assert spy.calls == []


class TestMultiUserReconcileProbe:
    def test_the_probe_never_uses_the_server_token(self, multi_user: None) -> None:
        assert publish_api_module._probe_token() == ""

    def test_the_probe_uses_the_request_token(
        self, monkeypatch: pytest.MonkeyPatch, multi_user: None
    ) -> None:
        seen: list[object] = []

        class _Api:
            def __init__(self, token: object = None) -> None:
                seen.append(token)

            def dataset_info(self, **_kwargs: object) -> object:
                return object()

        module = types.ModuleType("huggingface_hub")
        module.HfApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)
        api = publish_api_module.PublishApiService.__new__(publish_api_module.PublishApiService)

        assert api._probe_remote_publish_target("huggingface", "alice/air") is None
        with request_scope({"HF_TOKEN": _REQUEST_TOKEN}):
            assert api._probe_remote_publish_target("huggingface", "alice/air") is True
        assert seen == [_REQUEST_TOKEN]


class TestVisibilityLookupWithoutAToken:
    def test_passed_credentials_without_a_token_never_reach_the_ambient_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []

        class _Api:
            def __init__(self, token: object = None) -> None:
                seen.append(token)

            def repo_info(self, **_kwargs: object) -> object:
                return types.SimpleNamespace(private=True)

        module = types.ModuleType("huggingface_hub")
        module.HfApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        HuggingFacePublisher().destination_visibility("alice/air", credentials={})
        HuggingFacePublisher().destination_visibility("alice/air")

        # token=False forbids huggingface_hub's own HF_TOKEN / cached-login lookup.
        assert seen == [False, _SERVER_TOKEN]
