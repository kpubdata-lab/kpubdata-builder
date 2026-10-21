"""A hosted deployment sends nothing under unknown source terms to an external service (#1112).

``unknown`` terms used to block only a public publish. A private destination does not
show that the data may be handed to Hugging Face or Kaggle, and the user who asks is not
the rights holder, so a multi-user deployment refuses the private publish too. A
single-user deployment and the ``local`` target keep the earlier rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
import kpubdata_builder.service.publish_api as publish_api_module
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import publish as publish_service
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.service.publish_pii_gate import PII_GATE_ENV
from kpubdata_builder.service.redistribution import build_verdict, publish_issues
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef

from .test_service import _FakeClient
from .test_service_publish import SCANNED_SPEC_YAML, _SpyPublisher

_ALICE = Principal("oidc", "alice", "oidc:alice")
_DESTINATION = "alice/air-quality-1112"
_REQUEST_TOKEN = "hf_request_token_1112"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (PII_GATE_ENV, _OWNERSHIP_ENV, "OIDC_ISSUER", "HF_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _ALICE)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _SpyPublisher:
    publisher = _SpyPublisher("huggingface")
    monkeypatch.setitem(publish_api_module.PUBLISHER_REGISTRY, "huggingface", publisher)
    return publisher


def _service(tmp_path: Path, terms: str | None) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        terms_lookup=lambda _id: terms,
        publish_visibility_probe=lambda *_: "absent",
    )


def _build(service: BuilderService, run_id: str = "r1") -> None:
    response = dispatch(service, "POST", "/build", {"spec": SCANNED_SPEC_YAML, "run_id": run_id})
    assert isinstance(response, ServiceResponse) and response.status_code == 200, response.body


def _readiness(service: BuilderService, target: str = "huggingface") -> ServiceResponse:
    response = dispatch(
        service, "GET", "/builds/r1/publish/readiness", None, query=f"target={target}"
    )
    assert isinstance(response, ServiceResponse)
    return response


def _publish(service: BuilderService, options: dict[str, object]) -> ServiceResponse:
    response = dispatch(
        service,
        "POST",
        "/builds/r1/publish",
        {"target": "huggingface", "destination": _DESTINATION, "options": options},
        publish_credential_headers=[f"HF_TOKEN={_REQUEST_TOKEN}"],
    )
    assert isinstance(response, ServiceResponse)
    return response


def _codes(response: ServiceResponse) -> list[str]:
    return [cast(str, b["code"]) for b in cast(list[dict[str, object]], response.body["blockers"])]


def _spec(*sources: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="terms.hosted",
        title="t",
        description="d",
        sources=sources,
        exports=(ExportTarget(kind="jsonl", output_path="d.jsonl"),),
        license="CC-BY-4.0",
    )


# ---------------------------------------------------------------- the rule


def test_unknown_terms_refuse_a_private_publish_when_asked() -> None:
    verdict = build_verdict(_spec(SourceRef(provider="datago", dataset="x")), lambda _id: None)

    kept = publish_issues(verdict, public=False, confirmed_non_commercial=False, spec=None)
    refused = publish_issues(
        verdict,
        public=False,
        confirmed_non_commercial=False,
        spec=None,
        unknown_private_allowed=False,
    )

    assert kept == []
    assert [i.code for i in refused] == ["redistribution_unknown"]
    assert "privately or publicly" in refused[0].message


def test_one_unknown_source_refuses_a_build_of_allowed_ones() -> None:
    """Negative: mixed sources take the most restricted verdict."""
    terms = {"datago.ok": "allowed", "datago.unsure": None}
    verdict = build_verdict(
        _spec(
            SourceRef(provider="datago", dataset="ok"),
            SourceRef(provider="datago", dataset="unsure"),
        ),
        lambda dataset_id: terms[dataset_id],
    )

    issues = publish_issues(
        verdict,
        public=False,
        confirmed_non_commercial=False,
        spec=None,
        unknown_private_allowed=False,
    )

    assert verdict.verdict == "unknown"
    assert [i.code for i in issues] == ["redistribution_unknown"]


@pytest.mark.parametrize("terms", ["allowed", "non_commercial", "forbidden"])
def test_the_flag_changes_nothing_but_unknown(terms: str) -> None:
    verdict = build_verdict(_spec(SourceRef(provider="datago", dataset="x")), lambda _id: terms)
    for public in (False, True):
        before = publish_issues(verdict, public=public, confirmed_non_commercial=True, spec=None)
        after = publish_issues(
            verdict,
            public=public,
            confirmed_non_commercial=True,
            spec=None,
            unknown_private_allowed=False,
        )
        assert before == after


def test_only_a_multi_user_deployment_counts_as_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not publish_service.hosted_external_publish("huggingface")
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    assert publish_service.hosted_external_publish("huggingface")
    assert publish_service.hosted_external_publish("kaggle")
    assert not publish_service.hosted_external_publish("local")


def test_oidc_counts_as_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER", "https://id.example.com/realms/x")
    assert publish_service.hosted_external_publish("huggingface")


# ---------------------------------------------------------------- over HTTP


@pytest.mark.usefixtures("multi_user")
class TestHosted:
    @pytest.mark.parametrize("options", [{"private": True}, {"private": False}])
    def test_unknown_terms_publish_neither_privately_nor_publicly(
        self, tmp_path: Path, spy: _SpyPublisher, options: dict[str, object]
    ) -> None:
        service = _service(tmp_path, None)
        _build(service)

        readiness = _readiness(service)
        response = _publish(service, options)

        assert readiness.body["ready"] is False
        assert "redistribution_unknown" in _codes(readiness)
        assert response.status_code == 409
        assert "redistribution_unknown" in _codes(response)
        assert spy.calls == []

    def test_kaggle_is_external_too(self, tmp_path: Path) -> None:
        service = _service(tmp_path, None)
        _build(service)

        assert "redistribution_unknown" in _codes(_readiness(service, "kaggle"))

    def test_the_local_target_keeps_the_single_user_rule(self, tmp_path: Path) -> None:
        service = _service(tmp_path, None)
        _build(service)

        assert "redistribution_unknown" not in _codes(_readiness(service, "local"))

    def test_declared_terms_still_publish(self, tmp_path: Path, spy: _SpyPublisher) -> None:
        service = _service(tmp_path, "allowed")
        _build(service)

        response = _publish(service, {"private": True})

        assert response.status_code == 200, response.body
        assert len(spy.calls) == 1

    def test_the_refusal_carries_no_request_value(self, tmp_path: Path, spy: _SpyPublisher) -> None:
        """Leakage: the refusal names the code, not the token."""
        service = _service(tmp_path, None)
        _build(service)

        response = _publish(service, {"private": True})

        assert _REQUEST_TOKEN not in repr(response.body)
        assert spy.calls == []


class TestSingleUser:
    def test_a_private_publish_of_unknown_terms_goes_through_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: _SpyPublisher
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "hf_server_token_1112")
        service = _service(tmp_path, None)
        _build(service)

        response = _publish(service, {"private": True})

        assert response.status_code == 200, response.body
        assert len(spy.calls) == 1
