"""Live-orchestration tests for verify.runner (#578).

The CLI tests patch ``verify_dataset`` itself and ``test_verify.py`` covers only
the models and schema helpers, so nothing exercised the endpoint → auth →
response → parser → pagination → schema orchestration. A regression there could
let a failed check still return HEALTHY and exit 0, which is exactly the outcome
this command exists to prevent. These tests drive the runner with fakes.
"""

from __future__ import annotations

import dataclasses
import urllib.error
from typing import Any

import pytest
from kpubdata.core.spec import (
    AuthSpec,
    EndpointSpec,
    ErrorSpec,
    ExampleSpec,
    PaginationSpec,
    ResponseSpec,
    SpecDefinition,
)
from kpubdata.exceptions import AuthError, PublicDataError, RateLimitError, TransportError

from kpubdata_builder.verify import runner as runner_module
from kpubdata_builder.verify.models import CheckName, DatasetStatus


def _spec(*, pagination: str = "page") -> SpecDefinition:
    return SpecDefinition(
        id="datago.fake",
        provider="datago",
        title="fake dataset",
        endpoint=EndpointSpec(base_url="https://example.invalid/api", operation="getList"),
        auth=AuthSpec(type="query_key", param_name="serviceKey", provider_key="datago"),
        response=ResponseSpec(
            format="json",
            envelope="datago",
            items_path="body.items",
            total_count_path="body.totalCount",
            error=ErrorSpec(style="http_status"),
        ),
        pagination=PaginationSpec(type=pagination, page_param="pageNo", size_param="numOfRows"),
    )


class _FakeTransport:
    """Records whether the runner released the HTTP client it created."""

    instances: list[_FakeTransport] = []

    def __init__(self) -> None:
        self.closed = False
        _FakeTransport.instances.append(self)

    def close(self) -> None:
        self.closed = True


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetch: Any,
    items: Any = None,
    total_count: Any = None,
    payload_error: Exception | None = None,
) -> None:
    """Patch the kpubdata entry points the runner imports at call time."""
    import kpubdata.core.executor as executor_module
    import kpubdata.transport.http as http_module

    _FakeTransport.instances = []

    class _FakeExecutor:
        def __init__(self, transport: object, config: object) -> None:
            self.transport = transport

        def fetch(self, spec: object, query: object, format_hint: object = None) -> Any:
            return fetch()

    def _check_payload_error(spec: object, payload: object) -> None:
        if payload_error is not None:
            raise payload_error

    monkeypatch.setattr(http_module, "HttpTransport", _FakeTransport)
    monkeypatch.setattr(executor_module, "SpecExecutor", _FakeExecutor)
    monkeypatch.setattr(executor_module, "check_payload_error", _check_payload_error)
    monkeypatch.setattr(executor_module, "extract_items", lambda spec, payload: items or [])
    monkeypatch.setattr(executor_module, "extract_total_count", lambda spec, payload: total_count)
    monkeypatch.setattr(
        runner_module,
        "_check_endpoint",
        lambda spec: runner_module.CheckResult(CheckName.ENDPOINT, passed=True),
    )


def _check(result: Any, name: CheckName) -> Any:
    return next(c for c in result.checks if c.name == name)


class TestEndpointCheck:
    def test_404_is_not_treated_as_a_live_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "route not found" is the symptom of a removed or mistyped base URL.
        # Passing it printed "Endpoint pass" for a dead endpoint.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(
                urllib.error.HTTPError("u", 404, "Not Found", {}, None)  # type: ignore[arg-type]
            ),
        )

        check = runner_module._check_endpoint(_spec())

        assert not check.passed

    @pytest.mark.parametrize("code", [400, 401, 403, 405])
    def test_method_and_auth_rejections_prove_the_endpoint_is_live(
        self, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        # 401 in particular: the probe sends no credentials, so this reply means
        # the route exists. Failing early here never reached the authenticated
        # fetch, so INVALID_KEY and NEEDS_APPLICATION were unreachable.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(
                urllib.error.HTTPError("u", code, "rejected", {}, None)  # type: ignore[arg-type]
            ),
        )

        assert runner_module._check_endpoint(_spec()).passed

    def test_a_connection_error_fails_the_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no route to host")),
        )

        check = runner_module._check_endpoint(_spec())

        assert not check.passed
        assert "unreachable" in check.detail


class TestAuthClassification:
    def test_a_plain_403_is_an_application_requirement(self) -> None:
        # Classifying on free-form text alone made every AuthError(403) an
        # INVALID_KEY unless the provider happened to use one of the phrases.
        status = runner_module._classify_auth_error(AuthError("denied", status_code=403))

        assert status == DatasetStatus.NEEDS_APPLICATION

    def test_a_401_is_an_invalid_key(self) -> None:
        status = runner_module._classify_auth_error(AuthError("denied", status_code=401))

        assert status == DatasetStatus.INVALID_KEY

    def test_message_text_still_wins_over_the_status_code(self) -> None:
        status = runner_module._classify_auth_error(AuthError("승인 waiting", status_code=403))

        assert status == DatasetStatus.WAITING_APPROVAL

    def test_rate_limit_is_its_own_status(self) -> None:
        assert (
            runner_module._classify_auth_error(RateLimitError("slow down"))
            == DatasetStatus.RATE_LIMITED
        )


class TestOrchestration:
    def test_a_healthy_run_passes_every_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fakes(
            monkeypatch,
            fetch=lambda: ({}, {"body": {}}),
            items=[{"a": 1}, {"a": 2}],
            total_count=57,
        )

        result = runner_module.verify_dataset(_spec())

        assert result.status == DatasetStatus.HEALTHY
        assert result.passed
        assert all(c.passed for c in result.checks)

    def test_the_transport_is_closed_after_a_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # HttpTransport owns a persistent client; --all would otherwise pile up
        # sockets until the garbage collector got to them.
        _install_fakes(monkeypatch, fetch=lambda: ({}, {}), items=[{"a": 1}], total_count=1)

        runner_module.verify_dataset(_spec())

        assert _FakeTransport.instances and all(t.closed for t in _FakeTransport.instances)

    def test_the_transport_is_closed_when_the_fetch_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fakes(
            monkeypatch,
            fetch=lambda: (_ for _ in ()).throw(AuthError("denied", status_code=401)),
        )

        runner_module.verify_dataset(_spec())

        assert _FakeTransport.instances and all(t.closed for t in _FakeTransport.instances)

    def test_a_total_count_below_the_page_is_not_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 10 items alongside total_count=5 is an inconsistent response. Only
        # total_count < 0 used to fail, and even that left status HEALTHY.
        _install_fakes(
            monkeypatch,
            fetch=lambda: ({}, {}),
            items=[{"a": i} for i in range(10)],
            total_count=5,
        )

        result = runner_module.verify_dataset(_spec())

        assert not _check(result, CheckName.PAGINATION).passed
        assert result.status != DatasetStatus.HEALTHY
        assert not result.passed

    def test_a_negative_total_count_fails_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The check was recorded as failed but status stayed HEALTHY, so the CLI
        # reported a malformed response as healthy and exited 0.
        _install_fakes(monkeypatch, fetch=lambda: ({}, {}), items=[{"a": 1}], total_count=-1)

        result = runner_module.verify_dataset(_spec())

        assert not result.passed

    def test_schema_drift_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fakes(monkeypatch, fetch=lambda: ({}, {}), items=[{"a": 1}], total_count=1)

        result = runner_module.verify_dataset(_spec(), previous_hash="0" * 64)

        assert result.status == DatasetStatus.SCHEMA_CHANGED
        assert not _check(result, CheckName.SCHEMA).passed

    def test_an_envelope_auth_error_also_fails_the_auth_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A 200 response can still carry an auth failure in its envelope. The
        # report used to read "Auth pass" next to NEEDS_APPLICATION.
        _install_fakes(
            monkeypatch,
            fetch=lambda: ({}, {}),
            payload_error=AuthError("활용신청이 필요합니다", status_code=403),
        )

        result = runner_module.verify_dataset(_spec())

        assert result.status == DatasetStatus.NEEDS_APPLICATION
        assert not _check(result, CheckName.AUTH).passed

    def test_stages_after_a_failure_are_skipped_not_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Collapsing skipped into failed made one auth problem look like every
        # later stage was broken.
        _install_fakes(
            monkeypatch,
            fetch=lambda: (_ for _ in ()).throw(AuthError("denied", status_code=401)),
        )

        result = runner_module.verify_dataset(_spec())

        later = [c for c in result.checks if c.name in (CheckName.PARSER, CheckName.SCHEMA)]
        assert later and all(c.skipped for c in later)
        assert all(c.state == "skipped" for c in later)
        assert not _check(result, CheckName.AUTH).skipped

    def test_a_transport_error_is_a_broken_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fakes(
            monkeypatch, fetch=lambda: (_ for _ in ()).throw(TransportError("connection reset"))
        )

        result = runner_module.verify_dataset(_spec())

        assert result.status == DatasetStatus.BROKEN_ENDPOINT

    def test_a_parser_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kpubdata.core.executor as executor_module

        _install_fakes(monkeypatch, fetch=lambda: ({}, {}))
        monkeypatch.setattr(
            executor_module,
            "extract_items",
            lambda spec, payload: (_ for _ in ()).throw(PublicDataError("items_path missing")),
        )

        result = runner_module.verify_dataset(_spec())

        assert not _check(result, CheckName.PARSER).passed
        assert result.status == DatasetStatus.BROKEN_ENDPOINT


class TestTheRequestVerifySends:
    """Query and format hint the runner hands the executor (#1111).

    The default for a spec with no examples used to be built from kpubdata's private
    ``ExampleSpec``. These pin that dropping the import kept the request unchanged.
    """

    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
        import kpubdata.core.executor as executor_module

        _install_fakes(monkeypatch, fetch=lambda: ({}, {}))
        sent: list[tuple[Any, Any]] = []

        class _RecordingExecutor:
            def __init__(self, transport: object, config: object) -> None:
                pass

            def fetch(self, spec: object, query: object, format_hint: object = None) -> Any:
                sent.append((query, format_hint))
                return {}, {}

        monkeypatch.setattr(executor_module, "SpecExecutor", _RecordingExecutor)
        return sent

    def test_a_spec_without_examples_sends_no_filters_on_page_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._capture(monkeypatch)

        runner_module.verify_dataset(_spec(), page_size=7)

        assert len(sent) == 1
        query, format_hint = sent[0]
        assert (query.filters, query.page, query.page_size) == ({}, 1, 7)
        assert format_hint is None

    def test_the_first_example_supplies_params_page_and_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._capture(monkeypatch)
        spec = dataclasses.replace(
            _spec(),
            examples=(
                ExampleSpec(name="first", params={"LAWD_CD": "11680"}, page=3, format="xml"),
                ExampleSpec(name="second", params={"LAWD_CD": "00000"}, page=9),
            ),
        )

        runner_module.verify_dataset(spec)

        assert len(sent) == 1
        query, format_hint = sent[0]
        assert (query.filters, query.page, query.page_size) == ({"LAWD_CD": "11680"}, 3, 10)
        assert format_hint == "xml"

    def test_an_example_without_a_page_starts_on_page_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._capture(monkeypatch)
        spec = dataclasses.replace(_spec(), examples=(ExampleSpec(name="bare"),))

        runner_module.verify_dataset(spec)

        query, format_hint = sent[0]
        assert (query.filters, query.page) == ({}, 1)
        assert format_hint is None
