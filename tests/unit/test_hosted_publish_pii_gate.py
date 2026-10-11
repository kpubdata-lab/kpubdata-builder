"""A hosted publish refuses a run whose personal-data scan is not known to be clean (#1113).

The gate is on in a multi-user deployment whatever the setting says, and off in a
single-user one unless ``KPUBDATA_BUILDER_REQUIRE_PII_SCAN_FOR_PUBLISH`` turns it on.
Each state the default check reads has its own code; a refused publish calls no
publisher and leaves one audit row that names the code and nothing from the request.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import cast

import pytest
import yaml

import kpubdata_builder.service.app as app_module
import kpubdata_builder.service.publish_api as publish_api_module
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import publish as publish_service
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.service.publish_pii_gate import (
    PII_GATE_ENV,
    PII_SCAN_ISSUE_CODES,
    PiiScanState,
    default_pii_scan_check,
    pii_gate_required,
    pii_scan_state,
)
from kpubdata_builder.spec import BuildSpec
from kpubdata_builder.spec.loader import parse_spec

from .test_service import _FakeClient
from .test_service_publish import (
    FAILING_SPEC_YAML,
    LICENSED_SPEC_YAML,
    SCANNED_SPEC_YAML,
    _SpyPublisher,
)

_ALICE = Principal("oidc", "alice", "oidc:alice")
_DESTINATION = "alice/air-quality-1113"
_REQUEST_TOKEN = "hf_request_token_1113"
#: Synthetic, never real: a value shaped like a resident registration number.
_SYNTHETIC_RRN = "900101-1234567"

WARN_SPEC_YAML = LICENSED_SPEC_YAML + "pii:\n  mode: warn\n"
ACCEPTED_COLUMNS_SPEC_YAML = LICENSED_SPEC_YAML + "pii:\n  mode: block\n  allow_columns: [v]\n"


def _spec(yaml_text: str) -> BuildSpec:
    return parse_spec(cast(dict[str, object], yaml.safe_load(yaml_text)))


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


def _service(tmp_path: Path, *, synthetic_pii: bool = False) -> BuilderService:
    rows: list[dict[str, object]] = [{"id": "1", "v": 10}, {"id": "2", "v": 20}]
    if synthetic_pii:
        rows[0]["note"] = _SYNTHETIC_RRN
        rows[1]["note"] = "-"
    client = _FakeClient({"datago.air_quality": rows})
    return BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        terms_lookup=lambda _id: "allowed",
        publish_visibility_probe=lambda *_: "absent",
    )


def _build(service: BuilderService, run_id: str, spec_yaml: str) -> ServiceResponse:
    response = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": run_id})
    assert isinstance(response, ServiceResponse)
    return response


def _readiness(service: BuilderService, run_id: str) -> ServiceResponse:
    response = dispatch(
        service, "GET", f"/builds/{run_id}/publish/readiness", None, query="target=huggingface"
    )
    assert isinstance(response, ServiceResponse)
    return response


def _publish(service: BuilderService, run_id: str) -> ServiceResponse:
    response = dispatch(
        service,
        "POST",
        f"/builds/{run_id}/publish",
        {"target": "huggingface", "destination": _DESTINATION},
        publish_credential_headers=[f"HF_TOKEN={_REQUEST_TOKEN}"],
    )
    assert isinstance(response, ServiceResponse)
    return response


def _codes(response: ServiceResponse) -> list[str]:
    return [cast(str, b["code"]) for b in cast(list[dict[str, object]], response.body["blockers"])]


def _audit(service: BuilderService, run_id: str) -> list[dict[str, str]]:
    response = dispatch(service, "GET", f"/builds/{run_id}/publish/audit", None)
    assert isinstance(response, ServiceResponse) and response.status_code == 200
    return cast(list[dict[str, str]], response.body["entries"])


# ---------------------------------------------------------------- the default check


@pytest.mark.parametrize(
    ("status", "spec_yaml", "expected"),
    [
        ("queued", SCANNED_SPEC_YAML, "pending"),
        ("running", SCANNED_SPEC_YAML, "pending"),
        ("cancelling", SCANNED_SPEC_YAML, "pending"),
        ("failed", SCANNED_SPEC_YAML, "failed"),
        ("cancelled", SCANNED_SPEC_YAML, "failed"),
        ("succeeded", None, "missing"),
        ("succeeded", LICENSED_SPEC_YAML, "missing"),
        ("succeeded", WARN_SPEC_YAML, "unverified"),
        ("succeeded", ACCEPTED_COLUMNS_SPEC_YAML, "unverified"),
        ("succeeded", SCANNED_SPEC_YAML, "passed"),
    ],
)
def test_the_default_check_reads_each_state(
    status: str, spec_yaml: str | None, expected: PiiScanState
) -> None:
    spec = _spec(spec_yaml) if spec_yaml is not None else None
    assert default_pii_scan_check(status, spec) == expected


def test_allow_mode_is_unverified() -> None:
    """``pii.mode: allow`` cannot be stored with publish=true, but a check never trusts it."""
    spec = _spec(LICENSED_SPEC_YAML + "pii:\n  mode: allow\n")
    assert default_pii_scan_check("succeeded", spec) == "unverified"


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("missing", "pii_scan_missing"),
        ("pending", "pii_scan_pending"),
        ("failed", "pii_scan_failed"),
        ("unverified", "pii_scan_unverified"),
    ],
)
def test_each_refusing_state_has_its_own_stable_code(state: PiiScanState, code: str) -> None:
    issue = publish_service.pii_scan_blocker("succeeded", None, lambda *_: state)
    assert issue is not None and issue.code == code
    assert code in PII_SCAN_ISSUE_CODES
    assert code in publish_service.PUBLISH_ISSUE_CODES


def test_a_passed_scan_is_no_blocker() -> None:
    assert publish_service.pii_scan_blocker("succeeded", None, lambda *_: "passed") is None


def test_a_check_that_raises_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    def broken(_status: str, _spec: BuildSpec | None) -> PiiScanState:
        raise RuntimeError(f"detector crashed on {_SYNTHETIC_RRN}")

    assert pii_scan_state(broken, "succeeded", None) == "failed"
    assert _SYNTHETIC_RRN not in caplog.text


def test_a_check_with_an_unknown_answer_fails_closed() -> None:
    assert pii_scan_state(lambda *_: cast(PiiScanState, "clean"), "succeeded", None) == "failed"


# ---------------------------------------------------------------- when the gate is on


def test_single_user_leaves_the_gate_off() -> None:
    assert pii_gate_required() is False


@pytest.mark.parametrize("value", ["true", "1", "TRUE"])
def test_single_user_can_turn_the_gate_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(PII_GATE_ENV, value)
    assert pii_gate_required() is True


@pytest.mark.parametrize("value", ["", "false", "0"])
def test_multi_user_has_the_gate_on_whatever_the_setting_says(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    monkeypatch.setenv(PII_GATE_ENV, value)
    assert pii_gate_required() is True


def test_oidc_has_the_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER", "https://id.example.com/realms/x")
    monkeypatch.setenv(PII_GATE_ENV, "false")
    assert pii_gate_required() is True


def test_readiness_asks_the_check_only_when_the_gate_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def check(status: str, _spec: BuildSpec | None) -> PiiScanState:
        asked.append(status)
        return "missing"

    def readiness() -> publish_service.ReadinessResult:
        return publish_service.build_readiness(
            run_id="r-pending",
            target="huggingface",
            destination="",
            status="running",
            manifest=None,
            spec=None,
            output_root=tmp_path,
            pii_scan_check=check,
        )

    assert "pii_scan_missing" not in [b.code for b in readiness().blockers]
    assert asked == []

    monkeypatch.setenv(PII_GATE_ENV, "true")
    assert "pii_scan_missing" in [b.code for b in readiness().blockers]
    assert asked == ["running"]


def test_a_running_run_reports_the_scan_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    result = publish_service.build_readiness(
        run_id="r-running",
        target="huggingface",
        destination="",
        status="running",
        manifest=None,
        spec=_spec(SCANNED_SPEC_YAML),
        output_root=tmp_path,
    )
    codes = [b.code for b in result.blockers]
    assert "pii_scan_pending" in codes and "run_not_terminal" in codes
    assert result.ready is False


# ---------------------------------------------------------------- single-user over HTTP


class TestSingleUser:
    def test_an_unscanned_run_publishes_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: _SpyPublisher
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "hf_server_token_1113")
        service = _service(tmp_path)
        assert _build(service, "r1", LICENSED_SPEC_YAML).status_code == 200

        readiness = _readiness(service, "r1")
        response = _publish(service, "r1")

        assert not set(_codes(readiness)) & set(PII_SCAN_ISSUE_CODES)
        assert response.status_code == 200, response.body
        assert len(spy.calls) == 1
        assert _audit(service, "r1") == []

    def test_the_setting_turns_the_gate_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: _SpyPublisher
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "hf_server_token_1113")
        monkeypatch.setenv(PII_GATE_ENV, "true")
        service = _service(tmp_path)
        assert _build(service, "r1", LICENSED_SPEC_YAML).status_code == 200

        response = _publish(service, "r1")

        assert response.status_code == 409
        assert "pii_scan_missing" in _codes(response)
        assert spy.calls == []


# ---------------------------------------------------------------- multi-user over HTTP


@pytest.mark.usefixtures("multi_user")
class TestMultiUser:
    @pytest.mark.parametrize(
        ("spec_yaml", "code"),
        [
            (LICENSED_SPEC_YAML, "pii_scan_missing"),
            (WARN_SPEC_YAML, "pii_scan_unverified"),
            (ACCEPTED_COLUMNS_SPEC_YAML, "pii_scan_unverified"),
        ],
    )
    def test_a_run_not_known_clean_is_refused_and_audited(
        self, tmp_path: Path, spy: _SpyPublisher, spec_yaml: str, code: str
    ) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", spec_yaml).status_code == 200

        readiness = _readiness(service, "r1")
        response = _publish(service, "r1")

        assert readiness.body["ready"] is False and code in _codes(readiness)
        assert response.status_code == 409 and code in _codes(response)
        assert spy.calls == []
        (entry,) = _audit(service, "r1")
        assert entry["action"] == f"refused:{code}"
        assert entry["actor"] == "gate"

    def test_a_failed_run_is_refused_as_scan_failed(
        self, tmp_path: Path, spy: _SpyPublisher
    ) -> None:
        service = _service(tmp_path)
        _build(service, "r-failed", FAILING_SPEC_YAML + "pii:\n  mode: block\n")

        response = _publish(service, "r-failed")

        assert response.status_code == 409
        assert {"run_failed", "pii_scan_failed"} <= set(_codes(response))
        assert spy.calls == []
        assert [e["action"] for e in _audit(service, "r-failed")] == ["refused:pii_scan_failed"]

    def test_a_scan_that_finds_personal_data_fails_the_run_and_the_publish(
        self, tmp_path: Path, spy: _SpyPublisher
    ) -> None:
        """Real scan, synthetic value: ``block`` fails the build, and the gate reads that."""
        service = _service(tmp_path, synthetic_pii=True)
        built = _build(service, "r-found", SCANNED_SPEC_YAML)
        assert built.body["status"] == "failed"
        assert "PII" in cast(str, built.body["error"])
        assert _SYNTHETIC_RRN not in json.dumps(built.body)

        response = _publish(service, "r-found")

        assert response.status_code == 409
        assert "pii_scan_failed" in _codes(response)
        assert spy.calls == []

    def test_a_scanned_run_publishes(self, tmp_path: Path, spy: _SpyPublisher) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", SCANNED_SPEC_YAML).status_code == 200

        assert not set(_codes(_readiness(service, "r1"))) & set(PII_SCAN_ISSUE_CODES)
        response = _publish(service, "r1")

        assert response.status_code == 200, response.body
        assert len(spy.calls) == 1
        assert not any(e["action"].startswith("refused:") for e in _audit(service, "r1"))

    def test_readiness_alone_records_nothing(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", LICENSED_SPEC_YAML).status_code == 200

        _readiness(service, "r1")

        assert _audit(service, "r1") == []

    def test_the_refusal_record_holds_no_request_or_data_value(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, spy: _SpyPublisher
    ) -> None:
        """Leakage: the audit row, the response and the logs name the code, not the data."""
        caplog.set_level(logging.DEBUG)
        service = _service(tmp_path, synthetic_pii=True)
        assert _build(service, "r1", LICENSED_SPEC_YAML).status_code == 200

        response = _publish(service, "r1")

        assert response.status_code == 409
        entries = json.dumps(_audit(service, "r1"))
        rows = sqlite3.connect(tmp_path / "_publish_receipts.sqlite").execute(
            "SELECT * FROM publish_receipt_audit"
        )
        stored = json.dumps([list(row) for row in rows.fetchall()])
        for text in (entries, stored, json.dumps(response.body), caplog.text):
            assert _DESTINATION not in text
            assert _REQUEST_TOKEN not in text
            assert _SYNTHETIC_RRN not in text

    def test_a_failed_audit_write_still_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        spy: _SpyPublisher,
    ) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", LICENSED_SPEC_YAML).status_code == 200

        def broken(**_kwargs: object) -> None:
            raise sqlite3.OperationalError(f"disk full writing {_DESTINATION}")

        monkeypatch.setattr(service._publish_api._publish_receipts, "record_refusal", broken)
        response = _publish(service, "r1")

        assert response.status_code == 409 and "pii_scan_missing" in _codes(response)
        assert spy.calls == []
        assert "OperationalError" in caplog.text
        assert _DESTINATION not in caplog.text

    def test_the_check_can_be_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: _SpyPublisher
    ) -> None:
        """A deployment that decides its input scope plugs in its own check."""
        service = _service(tmp_path)
        assert _build(service, "r1", LICENSED_SPEC_YAML).status_code == 200
        monkeypatch.setattr(service._publish_api, "_pii_scan_check", lambda *_: "passed")

        response = _publish(service, "r1")

        assert response.status_code == 200, response.body
        assert len(spy.calls) == 1
