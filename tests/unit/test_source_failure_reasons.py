"""A provider's refusal is stated by its reason, not as "pipeline failed" (#1187).

The end-to-end group runs the real kpubdata client against a fake data.go.kr endpoint
that answers each way a beta user's first build fails, and reads the reason and message
off ``POST /preview`` and ``POST /build``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx
import kpubdata
import pytest
import yaml

from kpubdata_builder.pipeline.failures import (
    SOURCE_FAILURE_REASONS,
    provider_failure_reason,
    source_failure,
)
from kpubdata_builder.service.app import BuilderService
from kpubdata_builder.spec import JsonValue

_KEY = "registered+key/=="
_CONTRACT = Path(__file__).resolve().parents[2] / "contract" / "builder-api.yaml"


def _datago(code: str, error: type[kpubdata.PublicDataError] = kpubdata.AuthError) -> Exception:
    return error(f"provider said {_KEY}", provider="datago", provider_code=code)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (_datago("20"), "application_required"),
        (_datago("30"), "application_required"),
        (_datago("31"), "application_required"),
        (_datago("32"), "auth_unknown"),
        (_datago("21"), "auth_unknown"),  # a suspended key
        (_datago("33"), "auth_unknown"),
        (_datago("11", kpubdata.InvalidRequestError), "params_invalid"),
        (_datago("04", kpubdata.ProviderResponseError), "temporarily_unavailable"),
        (_datago("05", kpubdata.ProviderResponseError), "temporarily_unavailable"),
        (_datago("12", kpubdata.DatasetNotFoundError), "retired"),
        (_datago("22", kpubdata.RateLimitError), "rate_limited"),
        (_datago("10", kpubdata.InvalidRequestError), "params_invalid"),
        (_datago("01", kpubdata.ServiceUnavailableError), "temporarily_unavailable"),
        # The code decides over the type: an unregistered IP is not a missing application.
        (_datago("32", kpubdata.TransportError), "auth_unknown"),
        (kpubdata.AuthError("refused", status_code=401), "auth_unknown"),
        (kpubdata.AuthError("refused", status_code=403), "application_required"),
        (kpubdata.RateLimitError("slow down", status_code=429), "rate_limited"),
        (kpubdata.TransportTimeoutError("timed out"), "network_error"),
        (kpubdata.TransportError("connection refused"), "network_error"),
        (kpubdata.TransportError("bad gateway", status_code=502), "temporarily_unavailable"),
        (kpubdata.TransportError("forbidden", status_code=403), "application_required"),
        (kpubdata.TransportError("bad request", status_code=400), "params_invalid"),
        (kpubdata.TransportError("unauthorized", status_code=401), "auth_unknown"),
        (kpubdata.TransportError("too many", status_code=429), "rate_limited"),
        (kpubdata.ServiceUnavailableError("down"), "temporarily_unavailable"),
        (kpubdata.InvalidRequestError("missing base_date"), "params_invalid"),
        # Another provider's code space is not data.go.kr's: its type decides.
        (
            kpubdata.InvalidRequestError("x", provider="bok", provider_code="30"),
            "params_invalid",
        ),
        (kpubdata.TransportError("not found", status_code=404), None),
        (kpubdata.ProviderResponseError("odd answer", provider="datago", provider_code="99"), None),
        (kpubdata.ParseError("not json"), None),
        (ValueError("a Builder bug"), None),
    ],
)
def test_each_refusal_has_its_reason(error: Exception, reason: str | None) -> None:
    assert provider_failure_reason(error) == reason


def test_a_missing_decoder_is_not_a_refusal() -> None:
    # kpubdata raises InvalidRequestError for an XML answer when xmltodict is missing;
    # that is this installation, not the provider or the parameters.
    error = kpubdata.InvalidRequestError("XML support needs xmltodict", provider="datago")
    error.__cause__ = ImportError("No module named 'xmltodict'")

    assert provider_failure_reason(error) is None


def test_the_message_says_what_to_do_and_never_repeats_the_provider() -> None:
    failure = source_failure(_datago("30"), "air", provider_keys={"datago": _KEY})

    assert failure.reason == "application_required"
    assert failure.message.startswith("source 'air': ")
    assert "활용신청" in failure.message
    assert "provider said" not in failure.message
    assert _KEY not in failure.message


def test_a_percent_encoded_key_gets_a_hint_on_a_refused_key() -> None:
    encoded = "abc%2Bdef%3D%3D"

    refused = source_failure(_datago("30"), "air", provider_keys={"datago": encoded})
    throttled = source_failure(_datago("22"), "air", provider_keys={"datago": encoded})
    plain = source_failure(_datago("30"), "air", provider_keys={"datago": _KEY})
    # Only the refusing provider's key counts: another provider's encoded key does not.
    elsewhere = source_failure(
        _datago("30"), "air", provider_keys={"datago": _KEY, "seoul": encoded}
    )
    unknown = source_failure(_datago("30"), "air")

    assert "Decoding" in refused.message
    assert encoded not in refused.message
    assert "Decoding" not in throttled.message
    assert "Decoding" not in plain.message
    assert "Decoding" not in elsewhere.message
    assert "Decoding" not in unknown.message


def test_an_error_no_provider_raised_keeps_the_old_rule() -> None:
    failure = source_failure(RuntimeError("/srv/workspace/x.parquet"), "air")

    assert failure.reason is None
    assert failure.message == "pipeline failed for source 'air'"


def test_the_contract_enum_is_the_builder_vocabulary() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = contract["components"]["schemas"]["SourceFailureReason"]

    assert tuple(schema["enum"]) == SOURCE_FAILURE_REASONS


# --- End to end: the real client, a fake data.go.kr -----------------------------------


def _envelope(code: str, message: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"response": {"header": {"resultCode": code, "resultMsg": message}, "body": {}}},
    )


def _connection_refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


_ANSWERS: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "application_required": lambda _r: _envelope("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"),
    "auth_unknown": lambda _r: _envelope("32", "UNREGISTERED_IP_ERROR"),
    "rate_limited": lambda _r: _envelope("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS"),
    "params_invalid": lambda _r: _envelope("10", "INVALID_REQUEST_PARAMETER_ERROR"),
    "temporarily_unavailable": lambda _r: httpx.Response(503, text="maintenance"),
    "network_error": _connection_refused,
    "retired": lambda _r: _envelope("12", "NO_OPENAPI_SERVICE_ERROR"),
}


class _Upstream:
    def __init__(self) -> None:
        self.answer: Callable[[httpx.Request], httpx.Response] = _ANSWERS["application_required"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        return self.answer(request)


@pytest.fixture()
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Upstream]:
    monkeypatch.delenv("KPUBDATA_REPLAY_DIR", raising=False)
    monkeypatch.delenv("KPUBDATA_MODE", raising=False)
    served = _Upstream()
    real_client = httpx.Client

    def mocked_client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(served.handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", mocked_client)
    yield served


_SPEC = yaml.safe_dump(
    {
        "dataset_id": "forecast",
        "title": "Forecast",
        "description": "village forecast",
        "sources": [
            {
                "provider": "datago",
                "dataset": "village_fcst",
                "params": {"base_date": "20260908", "base_time": "2300", "nx": 55, "ny": 127},
            }
        ],
        "exports": [{"kind": "jsonl", "output_path": "data.jsonl"}],
    },
    allow_unicode=True,
)


def _service(tmp_path: Path) -> BuilderService:
    client: Any = kpubdata.Client(provider_keys={"datago": _KEY}, env_keys=False, max_retries=0)
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: client)


@pytest.mark.parametrize("reason", list(_ANSWERS))
def test_preview_and_build_say_each_reason(
    upstream: _Upstream, tmp_path: Path, reason: str
) -> None:
    upstream.answer = _ANSWERS[reason]
    service = _service(tmp_path)

    preview = cast(dict[str, Any], service.preview(_SPEC, limit=5).body)["previews"][0]
    build = service.build(_SPEC, run_id="r1")
    outcome = cast(dict[str, Any], build.body)["outcomes"][0]

    assert preview["status"] == "failed"
    assert preview["reason"] == reason
    assert build.status_code == 502
    assert outcome["reason"] == reason
    assert outcome["error"] == preview["error"]
    assert "pipeline failed" not in outcome["error"]
    assert _KEY not in outcome["error"]
    timeline = service.get_build_events("r1", limit=100, tail=False)
    events = cast(dict[str, Any], timeline.body)["events"]
    (fetch_failed,) = [e for e in events if e["event"] == "source_fetch_failed"]
    assert cast(dict[str, JsonValue], fetch_failed["metrics"])["reason"] == reason


def _echoing(request: httpx.Request) -> httpx.Response:
    # A provider that refuses the key and repeats the request, key included.
    return httpx.Response(403, text=f"forbidden: {request.url} key={_KEY}")


def test_a_provider_that_echoes_the_key_leaks_it_nowhere(
    upstream: _Upstream, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    upstream.answer = _echoing
    service = _service(tmp_path)

    with caplog.at_level(logging.DEBUG):
        preview = service.preview(_SPEC, limit=5)
        build = service.build(_SPEC, run_id="r1")
        timeline = service.get_build_events("r1", limit=100, tail=False)

    for body in (preview.body, build.body, timeline.body):
        text = json.dumps(body, ensure_ascii=False, default=str)
        assert _KEY not in text
        assert quote(_KEY, safe="") not in text
    assert cast(dict[str, Any], build.body)["outcomes"][0]["reason"] == "application_required"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert _KEY not in logged
    assert quote(_KEY, safe="") not in logged


def test_the_reasons_read_differently(upstream: _Upstream, tmp_path: Path) -> None:
    messages = set()
    for reason in _ANSWERS:
        upstream.answer = _ANSWERS[reason]
        body = cast(dict[str, Any], _service(tmp_path).preview(_SPEC, limit=5).body)
        messages.add(body["previews"][0]["error"])

    assert len(messages) == len(_ANSWERS)
