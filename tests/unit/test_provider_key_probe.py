"""``POST /providers/{provider}/probe`` uses the request's key only and keeps nothing (#802).

The service is driven through ``dispatch`` with the key in the ``X-Provider-Key`` header.
Most tests replace the probe itself, to choose its outcomes; the last ones run the real
kpubdata ``Client`` with its HTTP layer replaced, so the URL, the error text and the log
records are the ones production produces.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from kpubdata import PROBE_STATUSES

from kpubdata_builder import cli
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch, provider_probe
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_CONTRACT = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)
_PATH = "/providers/{provider}/probe"
_KEY = "canary-value-for-probe-test"
_OPERATOR = "operator-value-that-must-not-be-used"
_IDS = ["datago.apt_rent", "datago.apt_trade", "datago.village_fcst"]


@dataclass(frozen=True)
class _Outcome:
    service_id: str
    status: str
    detail: str = ""
    http_status: int | None = 200


@dataclass
class _Probe:
    """Stands in for kpubdata: records what it was opened with and asked."""

    outcomes: dict[str, _Outcome | None] = field(default_factory=dict)
    opened: list[tuple[str, str]] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    closed: int = 0
    error: Exception | None = None

    @contextmanager
    def open(self, provider: str, key: str) -> Iterator[provider_probe.ProbeOne]:
        self.opened.append((provider, key))
        try:
            yield self._one
        finally:
            self.closed += 1

    def _one(self, dataset_id: str) -> _Outcome | None:
        self.asked.append(dataset_id)
        if self.error is not None:
            raise self.error
        return self.outcomes.get(dataset_id, _Outcome("svc", "available"))


def _service(tmp_path: Path, probe: _Probe, ids: list[str] | None = None) -> BuilderService:
    return BuilderService(
        output_root=tmp_path,
        client_factory=cli._create_client,
        open_probe=probe.open,
        probe_datasets=lambda provider: [i for i in (ids or _IDS) if i.startswith(provider)],
    )


def _post(
    service: BuilderService,
    body: dict[str, JsonValue] | None = None,
    *,
    provider: str = "datago",
    headers: tuple[str, ...] = (f"datago={_KEY}",),
) -> ServiceResponse:
    response = dispatch(
        service, "POST", f"/providers/{provider}/probe", body, provider_key_headers=headers
    )
    assert isinstance(response, ServiceResponse)
    return response


def _only_dataset(response: ServiceResponse) -> dict[str, JsonValue]:
    datasets = response.body["datasets"]
    assert isinstance(datasets, list) and len(datasets) == 1
    (dataset,) = datasets
    assert isinstance(dataset, dict)
    return dataset


def test_each_dataset_gets_its_status_and_service(tmp_path: Path) -> None:
    probe = _Probe(
        {
            "datago.apt_trade": _Outcome("RTMSDataSvcAptTradeDev", "available"),
            "datago.apt_rent": _Outcome(
                "RTMSDataSvcAptRent", "application_required", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
            ),
            "datago.village_fcst": _Outcome("VilageFcstInfoService_2.0", "params_invalid", "", 400),
        }
    )

    response = _post(_service(tmp_path, probe))

    assert response.status_code == 200
    schema = response_schema(_CONTRACT, _PATH, "post", 200)
    assert schema is not None
    assert validate(response.body, schema, _CONTRACT) == []
    assert response.body["complete"] is True
    assert response.body["not_probed"] == []
    assert response.body["datasets"] == [
        {
            "dataset": "apt_rent",
            "service_id": "RTMSDataSvcAptRent",
            "status": "application_required",
            "detail": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
            "http_status": 200,
        },
        {
            "dataset": "apt_trade",
            "service_id": "RTMSDataSvcAptTradeDev",
            "status": "available",
            "detail": "",
            "http_status": 200,
        },
        {
            "dataset": "village_fcst",
            "service_id": "VilageFcstInfoService_2.0",
            "status": "params_invalid",
            "detail": "",
            "http_status": 400,
        },
    ]
    # Opened once with the header's key, and closed when the request ended.
    assert probe.opened == [("datago", _KEY)]
    assert probe.closed == 1


def test_without_the_header_nothing_is_probed_whatever_else_holds_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: the operator's environment key and a key for another provider are not
    used in its place."""
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _OPERATOR)
    probe = _Probe()
    service = _service(tmp_path, probe)

    for headers in ((), (f"localdata={_KEY}",)):
        response = _post(service, headers=headers)

        assert response.status_code == 400
        assert response.body["code"] == "provider_key_required"
    assert probe.opened == []


def test_the_response_holds_nothing_of_the_key(tmp_path: Path) -> None:
    probe = _Probe({"datago.apt_trade": _Outcome("svc", "network_error", f"GET /x/{_KEY}/y")})

    response = _post(_service(tmp_path, probe), {"datasets": ["apt_trade"]})

    assert response.status_code == 200
    text = json.dumps(response.body, ensure_ascii=False)
    assert _KEY not in text
    assert "GET /x/" in text
    assert probe.asked == ["datago.apt_trade"]


def test_a_probe_that_raises_answers_502_without_its_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    probe = _Probe(error=RuntimeError(f"https://apis.example/get?serviceKey={_KEY}"))

    with caplog.at_level(logging.DEBUG):
        response = _post(_service(tmp_path, probe))

    assert (response.status_code, response.body) == (502, {"error": "probe unavailable"})
    assert probe.closed == 1


@pytest.mark.parametrize(
    "body",
    [
        {"datasets": []},
        {"datasets": "apt_trade"},
        {"datasets": ["apt_trade", 3]},
        {"datasets": ["apt_trade"], "key": "x"},
        {"datasets": ["no_such_dataset"]},
        {"datasets": [f"d{index}" for index in range(51)]},
    ],
)
def test_a_body_that_is_not_a_list_of_known_datasets_is_refused(
    tmp_path: Path, body: dict[str, JsonValue]
) -> None:
    probe = _Probe()

    response = _post(_service(tmp_path, probe), body)

    assert response.status_code == 400
    assert response.body["code"] == "invalid_request"
    assert probe.asked == []


def test_an_unknown_provider_is_not_found(tmp_path: Path) -> None:
    probe = _Probe()

    response = _post(_service(tmp_path, probe), provider="nosuch", headers=(f"nosuch={_KEY}",))

    assert response.status_code == 404
    assert probe.opened == []


def test_past_the_budget_no_call_is_started_and_the_rest_is_named() -> None:
    probe = _Probe()
    ticks = iter([0.0, 0.0, 10.0, 50.0])
    clock: Callable[[], float] = lambda: next(ticks)  # noqa: E731

    body = provider_probe.run_probe(
        "datago", _KEY, _IDS, open_probe=probe.open, budget_seconds=45.0, clock=clock
    )

    assert probe.asked == ["datago.apt_rent", "datago.apt_trade"]
    assert body["complete"] is False
    assert body["not_probed"] == ["village_fcst"]
    datasets = body["datasets"]
    assert isinstance(datasets, list) and len(datasets) == 2


def test_a_dataset_without_a_spec_is_named_not_given_a_status() -> None:
    probe = _Probe({"datago.apt_trade": None})

    body = provider_probe.run_probe("datago", _KEY, ["datago.apt_trade"], open_probe=probe.open)

    assert (body["datasets"], body["not_probed"], body["complete"]) == ([], ["apt_trade"], False)


def test_the_datasets_offered_are_the_ones_kpubdata_has_a_spec_for() -> None:
    ids = provider_probe.spec_dataset_ids("datago")

    assert "datago.apt_trade" in ids
    assert ids == sorted(ids)
    assert all(item.startswith("datago.") for item in ids)
    assert provider_probe.spec_dataset_ids("nosuch") == []


# --- The real kpubdata client, with its HTTP layer replaced ---


@dataclass
class _Upstream:
    """Answers every call and keeps the requests it saw."""

    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            # An upstream that echoes the request, key included.
            return httpx.Response(self.status, text=f"rejected: {request.url}")
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00", "resultMsg": "OK"},
                    "body": {"items": {"item": [{"aptNm": "a"}]}, "totalCount": 1},
                }
            },
        )


@pytest.fixture()
def upstream(monkeypatch: pytest.MonkeyPatch) -> _Upstream:
    # The cross-repo job replays recorded fixtures instead of calling HTTP; these tests
    # need the HTTP path they observe.
    monkeypatch.delenv("KPUBDATA_REPLAY_DIR", raising=False)
    monkeypatch.delenv("KPUBDATA_MODE", raising=False)
    seen = _Upstream()
    real_client = httpx.Client

    def mocked_client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(seen.handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", mocked_client)
    return seen


def _real_service(tmp_path: Path) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=cli._create_client)


def test_the_real_client_calls_with_the_header_key_and_not_the_operators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: _Upstream
) -> None:
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _OPERATOR)

    response = _post(_real_service(tmp_path), {"datasets": ["apt_trade"]})

    assert response.status_code == 200
    dataset = _only_dataset(response)
    assert dataset["status"] in PROBE_STATUSES
    assert dataset["status"] == "available"
    assert len(upstream.requests) == 1
    sent = str(upstream.requests[0].url) + repr(upstream.requests[0].headers.raw)
    assert _KEY in sent
    assert _OPERATOR not in sent


@pytest.mark.parametrize("status", [401, 403, 500])
def test_the_key_reaches_no_response_and_no_log_when_the_upstream_echoes_it(
    tmp_path: Path, upstream: _Upstream, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    upstream.status = status

    with caplog.at_level(logging.DEBUG):
        response = _post(_real_service(tmp_path), {"datasets": ["apt_trade"]})

    assert response.status_code == 200
    dataset = _only_dataset(response)
    assert dataset["status"] != "available"
    assert len(upstream.requests) == 1 and _KEY in str(upstream.requests[0].url)
    assert _KEY not in json.dumps(response.body, ensure_ascii=False)
    logged = "\n".join(
        logging.Formatter("%(message)s %(exc_text)s").format(record) for record in caplog.records
    )
    assert _KEY not in logged


def test_the_real_client_does_not_fall_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch, upstream: _Upstream
) -> None:
    """Negative: opened for another provider, it has no datago key — and takes none from
    the environment, so no call is made."""
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _OPERATOR)

    with provider_probe.open_kpubdata_probe("localdata", _KEY) as probe_one:
        outcome = probe_one("datago.apt_trade")

    assert outcome is not None
    assert outcome.status == "auth_unknown"
    assert upstream.requests == []
