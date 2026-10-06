"""A credential header is judged after authentication, and only where it is read (#1105).

``dispatch`` parsed ``X-Provider-Key`` and ``X-Publish-Credential`` before anything
else. A malformed ``X-Publish-Credential`` answered 400 on every route — the health
check, the version check, routes that read no publish credential — and answered it to a
caller with no credentials at all, who was told how a header was malformed before being
told to sign in. #1073 had narrowed the provider header to the routes that read it; the
400 still came before the authentication gate.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import _API_KEY_ENV, _DEV_MODE_ENV
from kpubdata_builder.service.publish_credentials import route_reads_publish_credentials
from kpubdata_builder.service.request_credentials import route_reads_provider_keys

_SERVER_KEY = "server-key-0123456789"
#: Stand-ins a malformed header might carry; none may come back in an answer or a log.
_HEADER_VALUE = "stand-in-header-value-not-a-credential"
_SECOND_VALUE = "stand-in-second-header-value"
_THIRD_VALUE = "stand-in-third-header-value"
_HEADER_VALUES = (_HEADER_VALUE, _SECOND_VALUE, _THIRD_VALUE)
_BAD_PROVIDER = [f"{_HEADER_VALUE}"]  # no '<provider>='
_BAD_PUBLISH = [f"TOKEN={_HEADER_VALUE}"]  # a variable no publish target uses
_TWO_VALUES = [f"HF_TOKEN={_HEADER_VALUE}", f"HF_TOKEN={_THIRD_VALUE}"]

_SPEC = "version: '1'\ndataset_id: d\nsources: []\n"

#: (method, path, body) of a route that reads provider keys.
_PROVIDER_ROUTES = [
    ("POST", "/preview", {"spec": _SPEC}),
    ("POST", "/build", {"spec": _SPEC}),
    ("POST", "/builds", {"spec": _SPEC}),
    ("GET", "/providers", None),
    ("GET", "/providers/datago/status", None),
    ("POST", "/providers/datago/test", {}),
    ("POST", "/providers/datago/probe", {}),
]
#: (method, path, body) of a route that reads publish credentials.
_PUBLISH_ROUTES = [
    ("POST", "/builds/r1/publish", {"target": "huggingface", "destination": "a/b"}),
    ("POST", "/builds/r1/publish/reconcile", {"target": "huggingface"}),
    ("GET", "/builds/r1/publish/readiness", None),
    ("GET", "/builds/r1/publish/receipt", None),
    ("DELETE", "/builds/r1/publish/receipt", None),
]
#: Routes that read neither header.
_OTHER_ROUTES = [
    ("GET", "/version", None),
    ("GET", "/builds", None),
    ("GET", "/builds/r1", None),
    ("GET", "/catalog", None),
    ("GET", "/uploads", None),
    ("POST", "/builds/r1/cancel", {}),
]


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
    monkeypatch.delenv(_DEV_MODE_ENV, raising=False)
    monkeypatch.setenv(_API_KEY_ENV, _SERVER_KEY)
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: object())


def _call(
    service: BuilderService,
    route: tuple[str, str, dict[str, object] | None],
    *,
    api_key: str | None = _SERVER_KEY,
    provider: list[str] | None = None,
    publish: list[str] | None = None,
) -> ServiceResponse:
    method, path, body = route
    response = dispatch(
        service,
        method,
        path,
        body,
        api_key=api_key,
        client_id="198.51.100.7",
        provider_key_headers=provider or [],
        publish_credential_headers=publish or [],
    )
    assert isinstance(response, ServiceResponse)
    return response


# ------------------------------------------------------------------ public route


@pytest.mark.parametrize("api_key", [None, "wrong", _SERVER_KEY])
def test_the_health_check_ignores_both_headers(
    service: BuilderService, api_key: str | None
) -> None:
    response = dispatch(
        service,
        "GET",
        "/healthz",
        None,
        api_key=api_key,
        provider_key_headers=_BAD_PROVIDER,
        publish_credential_headers=_BAD_PUBLISH,
    )

    assert isinstance(response, ServiceResponse)
    assert (response.status_code, response.body) == (200, {"status": "ok"})


# ---------------------------------------------------- routes that read neither header


@pytest.mark.parametrize("route", _OTHER_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_route_that_reads_no_credential_answers_as_without_the_headers(
    service: BuilderService, route: tuple[str, str, dict[str, object] | None]
) -> None:
    plain = _call(service, route)

    with_headers = _call(service, route, provider=_BAD_PROVIDER, publish=_BAD_PUBLISH)

    assert (with_headers.status_code, with_headers.body) == (plain.status_code, plain.body)
    assert with_headers.body.get("code") not in (
        "invalid_provider_key",
        "invalid_publish_credential",
    )


@pytest.mark.parametrize("route", _PROVIDER_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_provider_route_does_not_read_the_publish_header(
    service: BuilderService, route: tuple[str, str, dict[str, object] | None]
) -> None:
    plain = _call(service, route)

    response = _call(service, route, publish=_BAD_PUBLISH)

    assert response.status_code == plain.status_code
    assert response.body.get("code") != "invalid_publish_credential"


@pytest.mark.parametrize("route", _PUBLISH_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_publish_route_does_not_read_the_provider_header(
    service: BuilderService, route: tuple[str, str, dict[str, object] | None]
) -> None:
    plain = _call(service, route)

    response = _call(service, route, provider=_BAD_PROVIDER)

    assert (response.status_code, response.body) == (plain.status_code, plain.body)


# ------------------------------------------------------- authentication comes first


@pytest.mark.parametrize("api_key", [None, "wrong"])
@pytest.mark.parametrize(
    "route", _PROVIDER_ROUTES + _PUBLISH_ROUTES + _OTHER_ROUTES, ids=lambda r: f"{r[0]} {r[1]}"
)
def test_an_unauthenticated_request_is_told_to_authenticate(
    service: BuilderService,
    route: tuple[str, str, dict[str, object] | None],
    api_key: str | None,
) -> None:
    """Negative: whatever its headers, it gets the answer it would get without them."""
    plain = _call(service, route, api_key=api_key)

    response = _call(service, route, api_key=api_key, provider=_BAD_PROVIDER, publish=_BAD_PUBLISH)

    assert response.status_code == 401
    assert (response.status_code, response.body) == (plain.status_code, plain.body)


def test_failed_attempts_are_still_counted_when_the_headers_are_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 400 used to come before the gate, so a guess sent with a malformed header was
    never counted against the client."""
    monkeypatch.delenv(_DEV_MODE_ENV, raising=False)
    monkeypatch.setenv(_API_KEY_ENV, _SERVER_KEY)
    monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "5")
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: object())
    route = _PUBLISH_ROUTES[0]
    statuses = [
        _call(service, route, api_key="wrong", publish=_BAD_PUBLISH).status_code for _ in range(8)
    ]

    assert statuses[0] == 401
    assert 429 in statuses
    assert 400 not in statuses


# -------------------------------------------- the header is judged where it is read


@pytest.mark.parametrize("route", _PROVIDER_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_malformed_provider_header_is_refused_where_keys_are_read(
    service: BuilderService, route: tuple[str, str, dict[str, object] | None]
) -> None:
    response = _call(service, route, provider=_BAD_PROVIDER)

    assert response.status_code == 400
    assert response.body["code"] == "invalid_provider_key"


@pytest.mark.parametrize("publish", [_BAD_PUBLISH, _TWO_VALUES, ["HF_TOKEN"], ["=x"]])
@pytest.mark.parametrize("route", _PUBLISH_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_malformed_publish_header_is_refused_where_credentials_are_read(
    service: BuilderService,
    route: tuple[str, str, dict[str, object] | None],
    publish: list[str],
) -> None:
    response = _call(service, route, publish=publish)

    assert response.status_code == 400
    assert response.body["code"] == "invalid_publish_credential"


def test_a_well_formed_header_is_not_refused(service: BuilderService) -> None:
    for route in _PUBLISH_ROUTES:
        response = _call(service, route, publish=[f"HF_TOKEN={_HEADER_VALUE}"])
        assert response.body.get("code") != "invalid_publish_credential", route
    for route in _PROVIDER_ROUTES:
        response = _call(service, route, provider=[f"datago={_HEADER_VALUE}"])
        assert response.body.get("code") != "invalid_provider_key", route


def test_the_two_lists_of_routes_do_not_overlap() -> None:
    for method, path, _ in _PROVIDER_ROUTES + _PUBLISH_ROUTES + _OTHER_ROUTES:
        assert not (
            route_reads_provider_keys(method, path)
            and route_reads_publish_credentials(method, path)
        ), (method, path)
    for method, path, _ in _PUBLISH_ROUTES:
        assert route_reads_publish_credentials(method, path), (method, path)
    for method, path, _ in _PROVIDER_ROUTES + _OTHER_ROUTES:
        assert not route_reads_publish_credentials(method, path), (method, path)
    # Near misses are not publish routes.
    for method, path in (
        ("GET", "/builds/r1/publish"),
        ("PUT", "/builds/r1/publish"),
        ("POST", "/builds/r1/publish/receipt"),
        ("POST", "/builds/publish"),
        ("POST", "/datasets/d/publish"),
        ("POST", "/builds/r1/publish/reconcile/x"),
    ):
        assert not route_reads_publish_credentials(method, path), (method, path)


# -------------------------------------------------------------------- nothing leaks


@pytest.mark.parametrize(
    ("provider", "publish"),
    [
        (_BAD_PROVIDER, None),
        ([f"datago={_HEADER_VALUE}", f"datago={_SECOND_VALUE}"], None),
        (None, _BAD_PUBLISH),
        (None, _TWO_VALUES),
        (_BAD_PROVIDER, _BAD_PUBLISH),
    ],
)
@pytest.mark.parametrize("api_key", [None, "wrong", _SERVER_KEY])
def test_no_answer_and_no_log_line_holds_a_header_value(
    service: BuilderService,
    caplog: pytest.LogCaptureFixture,
    provider: list[str] | None,
    publish: list[str] | None,
    api_key: str | None,
) -> None:
    answers = []
    with caplog.at_level(logging.DEBUG):
        for route in _PROVIDER_ROUTES + _PUBLISH_ROUTES + _OTHER_ROUTES:
            answers.append(
                _call(service, route, api_key=api_key, provider=provider, publish=publish)
            )

    logged = caplog.text + "".join(str(record.__dict__) for record in caplog.records)
    for value in _HEADER_VALUES:
        assert value not in logged
        for response in answers:
            assert value not in json.dumps(response.body, ensure_ascii=False)
