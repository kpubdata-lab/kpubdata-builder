"""What the service sends on every route is declared in the contract (#994).

The ``X-Provider-Key`` request header, the 429 ``auth_throttled``, the overload 503 and
the ``X-Request-ID`` response header existed in the service and only in prose in the
contract. These compare the declarations with what the code actually sends.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from kpubdata_builder.service.http import _overloaded_response
from kpubdata_builder.service.publish_credentials import route_reads_publish_credentials
from kpubdata_builder.service.request_credentials import (
    PROVIDER_KEY_HEADER,
    route_reads_provider_keys,
)

_CONTRACT = Path(__file__).resolve().parents[2] / "contract" / "builder-api.yaml"

#: The operations that call a provider with the requester's key.
_PROVIDER_OPERATIONS = {
    "previewBuild",
    "createBuild",
    "submitBuild",
    "getProviderStatus",
    "testProviderConnection",
    "probeProviderKey",
}

#: The operations that read the requester's key without calling a provider. In a
#: multi-user deployment the request is the only place a key lives, so the provider
#: list can only say what is covered when it sees the key.
_KEY_READING_OPERATIONS = {
    "listProviders",
}


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return loaded


def _operations(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        operation["operationId"]: operation
        for item in contract["paths"].values()
        for method, operation in item.items()
        if method in ("get", "post", "put", "delete") and "operationId" in operation
    }


def test_the_provider_key_header_is_a_declared_parameter(contract: dict[str, Any]) -> None:
    parameter = contract["components"]["parameters"]["ProviderKey"]

    assert (parameter["name"], parameter["in"], parameter["required"]) == (
        PROVIDER_KEY_HEADER,
        "header",
        False,
    )


def test_it_is_declared_on_exactly_the_operations_that_read_the_key(
    contract: dict[str, Any],
) -> None:
    reference = {"$ref": "#/components/parameters/ProviderKey"}
    declaring = {
        name
        for name, operation in _operations(contract).items()
        if reference in operation.get("parameters", [])
    }

    assert declaring == _PROVIDER_OPERATIONS | _KEY_READING_OPERATIONS


def test_a_malformed_header_is_refused_on_exactly_those_operations(
    contract: dict[str, Any],
) -> None:
    """``route_reads_provider_keys`` answers from the table generated from the contract
    (#1109); this reads the contract itself and holds the answer to it, in both
    directions (#1073)."""
    reference = {"$ref": "#/components/parameters/ProviderKey"}
    disagree: list[str] = []
    for template, item in contract["paths"].items():
        path = re.sub(r"\{[^}]+\}", "x", template)
        for method, operation in item.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            declared = reference in operation.get("parameters", [])
            if route_reads_provider_keys(method.upper(), path) != declared:
                disagree.append(f"{method.upper()} {template}")

    assert disagree == []


def test_a_malformed_publish_header_is_refused_on_exactly_the_declaring_operations(
    contract: dict[str, Any],
) -> None:
    """``route_reads_publish_credentials`` answers from the table generated from the
    contract (#1109); this reads the contract itself and holds the answer to it, in both
    directions (#1105)."""
    reference = {"$ref": "#/components/parameters/PublishCredential"}
    disagree: list[str] = []
    declaring = 0
    for template, item in contract["paths"].items():
        path = re.sub(r"\{[^}]+\}", "x", template)
        for method, operation in item.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            declared = reference in operation.get("parameters", [])
            declaring += declared
            if route_reads_publish_credentials(method.upper(), path) != declared:
                disagree.append(f"{method.upper()} {template}")

    assert disagree == []
    assert declaring == 5


def test_the_overload_response_is_the_declared_one(contract: dict[str, Any]) -> None:
    declared = contract["components"]["responses"]["ServerOverloaded"]
    head, _, body = _overloaded_response().partition(b"\r\n\r\n")
    headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:])

    assert head.startswith(b"HTTP/1.1 %d " % declared["x-status"])
    example = declared["content"]["application/json"]["examples"]["ServerOverloaded"]["value"]
    assert json.loads(body) == example
    assert set(declared["headers"]) <= {name.decode() for name in headers}


def test_the_auth_unavailable_response_is_the_declared_one(
    contract: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every authenticated operation can answer it, so it is declared once (#1109) — and
    what the service sends when the signing keys cannot be fetched is that declaration."""
    import kpubdata_builder.service.auth as auth_module
    from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch

    declared = contract["components"]["responses"]["AuthUnavailable"]
    example = declared["content"]["application/json"]["examples"]["AuthUnavailable"]["value"]

    class _Unreachable:
        def get_signing_key_from_jwt(self, token: str) -> object:
            raise ConnectionError("jwks endpoint unreachable")

    monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("OIDC_AUDIENCE", "builder")
    monkeypatch.setenv("OIDC_JWKS_URL", "http://localhost:0/jwks.json")
    monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "1")
    monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _Unreachable())
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_kw: None)

    answers = [
        dispatch(service, method, path, None, bearer_token="Bearer a.b.c", client_id="10.0.0.9")
        for method, path in (("GET", "/datasets"), ("POST", "/builds"), ("GET", "/version"))
    ]

    for answer in answers:
        assert isinstance(answer, ServiceResponse)
        assert answer.status_code == declared["x-status"] == 503
        assert answer.body == example
    # Three of them with a limit of one: it is not a failed attempt, so no 429 came.
    assert [a.status_code for a in answers if isinstance(a, ServiceResponse)] == [503, 503, 503]


def test_an_api_key_request_never_gets_auth_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: a key needs no signing keys, so their being unreachable is not its concern."""
    import kpubdata_builder.service.auth as auth_module
    from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch

    def unreachable() -> object:
        raise ConnectionError("jwks endpoint unreachable")

    monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("OIDC_AUDIENCE", "builder")
    monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "the-right-key")
    monkeypatch.setattr(auth_module, "_get_jwks_client", unreachable)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_kw: None)

    answer = dispatch(service, "GET", "/version", None, api_key="the-right-key")

    assert isinstance(answer, ServiceResponse) and answer.status_code == 200


def test_the_auth_throttle_response_is_the_declared_one(
    contract: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch

    declared = contract["components"]["responses"]["AuthThrottled"]
    # The suite runs in dev mode, which skips authentication; this needs it on.
    monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "the-right-key")
    monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "1")
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_kw: None)

    first = dispatch(service, "GET", "/datasets", None, api_key="wrong", client_id="10.0.0.9")
    second = dispatch(service, "GET", "/datasets", None, api_key="wrong", client_id="10.0.0.9")

    assert isinstance(first, ServiceResponse) and first.status_code == 401
    assert isinstance(second, ServiceResponse)
    assert second.status_code == declared["x-status"]
    example = declared["content"]["application/json"]["examples"]["AuthThrottled"]["value"]
    assert set(second.body) == set(example)
    assert (second.body["error"], second.body["code"]) == (example["error"], example["code"])


def test_the_request_id_header_is_declared_and_sent(
    contract: dict[str, Any], tmp_path: Path
) -> None:
    import threading
    import urllib.request
    from http.server import HTTPServer

    from kpubdata_builder.service import BuilderService
    from kpubdata_builder.service.http import make_handler

    assert "RequestId" in contract["components"]["headers"]
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_kw: None)
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/healthz"
        with urllib.request.urlopen(url, timeout=5.0) as response:
            assert response.headers["X-Request-ID"]
    finally:
        server.shutdown()
        server.server_close()
