"""What the service sends on every route is declared in the contract (#994).

The ``X-Provider-Key`` request header, the 429 ``auth_throttled``, the overload 503 and
the ``X-Request-ID`` response header existed in the service and only in prose in the
contract. These compare the declarations with what the code actually sends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from kpubdata_builder.service.http import _overloaded_response
from kpubdata_builder.service.request_credentials import PROVIDER_KEY_HEADER

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


def test_it_is_declared_on_exactly_the_operations_that_call_a_provider(
    contract: dict[str, Any],
) -> None:
    reference = {"$ref": "#/components/parameters/ProviderKey"}
    declaring = {
        name
        for name, operation in _operations(contract).items()
        if reference in operation.get("parameters", [])
    }

    assert declaring == _PROVIDER_OPERATIONS


def test_the_overload_response_is_the_declared_one(contract: dict[str, Any]) -> None:
    declared = contract["components"]["responses"]["ServerOverloaded"]
    head, _, body = _overloaded_response().partition(b"\r\n\r\n")
    headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:])

    assert head.startswith(b"HTTP/1.1 %d " % declared["x-status"])
    example = declared["content"]["application/json"]["examples"]["ServerOverloaded"]["value"]
    assert json.loads(body) == example
    assert set(declared["headers"]) <= {name.decode() for name in headers}


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
