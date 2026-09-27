"""Service dispatch route adapter    (#522)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import FileResponse, ServiceResponse
from kpubdata_builder.service.app import BuilderService, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.auth_throttle import AuthFailureThrottle
from kpubdata_builder.service.monitoring import LatencyRecorder
from kpubdata_builder.service.responses import (
    FileResponse as ResponseModuleFileResponse,
)
from kpubdata_builder.service.responses import (
    ServiceResponse as ResponseModuleServiceResponse,
)
from kpubdata_builder.spec import JsonValue


class _RoutingStub:
    """dispatch() latency recording wrapper _latency_recorder  
     _auth_throttle ,    dummy  ."""

    _latency_recorder = LatencyRecorder()
    _auth_throttle = AuthFailureThrottle()


def _unused_service() -> BuilderService:
    return cast(BuilderService, _RoutingStub())


def test_health_bypasses_authentication_and_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_auth(**_kwargs: str | None) -> Principal:
        raise AssertionError("health must not authenticate")

    def fail_adapter(
        service: BuilderService,
        method: str,
        path: str,
        body: Mapping[str, JsonValue] | None,
        query: str,
        principal: Principal,
    ) -> ServiceResponse | None:
        del service, method, path, body, query, principal
        raise AssertionError("health must not enter route adapters")

    monkeypatch.setattr(app_module, "authenticate", fail_auth)
    monkeypatch.setattr(app_module, "ROUTE_ADAPTERS", (fail_adapter,))

    response = dispatch(_unused_service(), "GET", "/healthz", None)

    assert response == ServiceResponse(200, {"status": "ok"})


def test_authenticates_once_before_ordered_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    def authenticate(**_kwargs: str | None) -> Principal:
        events.append("auth")
        return Principal("dev")

    def fallthrough(
        service: BuilderService,
        method: str,
        path: str,
        body: Mapping[str, JsonValue] | None,
        query: str,
        principal: Principal,
    ) -> None:
        del service, method, path, body, query, principal
        events.append("first")

    def match(
        service: BuilderService,
        method: str,
        path: str,
        body: Mapping[str, JsonValue] | None,
        query: str,
        principal: Principal,
    ) -> ServiceResponse:
        del service, method, path, body, query, principal
        events.append("second")
        return ServiceResponse(204, {})

    def must_not_run(
        service: BuilderService,
        method: str,
        path: str,
        body: Mapping[str, JsonValue] | None,
        query: str,
        principal: Principal,
    ) -> None:
        del service, method, path, body, query, principal
        events.append("third")

    monkeypatch.setattr(app_module, "authenticate", authenticate)
    monkeypatch.setattr(app_module, "ROUTE_ADAPTERS", (fallthrough, match, must_not_run))

    response = dispatch(_unused_service(), "GET", "/matched", None)

    assert response.status_code == 204
    assert events == ["auth", "first", "second"]


def test_adapter_fallthrough_returns_dispatch_404(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def adapter(
        service: BuilderService,
        method: str,
        path: str,
        body: Mapping[str, JsonValue] | None,
        query: str,
        principal: Principal,
    ) -> None:
        del service, method, body, query, principal
        calls.append(path)

    monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: Principal("dev"))
    monkeypatch.setattr(app_module, "ROUTE_ADAPTERS", (adapter, adapter))

    response = dispatch(_unused_service(), "PATCH", "/missing", None)

    assert response == ServiceResponse(404, {"error": "not found: PATCH /missing"})
    assert calls == ["/missing", "/missing"]


def test_response_imports_remain_compatible() -> None:
    assert app_module.ServiceResponse is ResponseModuleServiceResponse is ServiceResponse
    assert app_module.FileResponse is ResponseModuleFileResponse is FileResponse
