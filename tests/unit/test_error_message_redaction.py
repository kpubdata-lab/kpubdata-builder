"""Verify that internal exception strings do not leak into HTTP responses.

Returning exceptions directly in responses exposes information callers don't need —
OSError carries server absolute paths, upstream client exceptions carry request URLs.
data.go.kr variants send API keys as query parameters, so those URLs contain
others' credentials.

The issue is not eliminating diagnostic information but **where to send it** —
tracebacks go to logs, responses contain only stable messages.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.jobs import AsyncBuildExecutor
from kpubdata_builder.spec import BUILDSPEC_SNAPSHOT_FILENAME

_SECRET_PATH = "/srv/kpubdata/secrets/production.key"
_SECRET_URL = "https://apis.data.go.kr/service?serviceKey=SUPERSECRETKEY"


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _service(tmp_path: Path) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: _FakeClient())


class TestSnapshotReadFailure:
    def test_an_unreadable_snapshot_does_not_return_the_server_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / BUILDSPEC_SNAPSHOT_FILENAME).write_text("dataset_id: x\n", encoding="utf-8")

        def _boom(self: Path) -> bytes:
            raise OSError(f"[Errno 13] Permission denied: {_SECRET_PATH}")

        monkeypatch.setattr(Path, "read_bytes", _boom)

        with caplog.at_level(logging.ERROR):
            response = _service(tmp_path).spec("run1")

        assert response.status_code == 500
        assert response.body == {"error": "failed to read BuildSpec snapshot"}
        assert _SECRET_PATH not in str(response.body)
        # It doesn't disappear; it goes to the log.
        assert _SECRET_PATH in caplog.text


class TestCatalogFailure:
    def test_an_upstream_failure_does_not_return_the_request_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import kpubdata_builder.service.spec_api as spec_api_module

        def _boom(_client: object) -> Any:
            raise RuntimeError(f"connection failed: {_SECRET_URL}")

        # The catalog lives in the spec authoring service (#596).
        monkeypatch.setattr(spec_api_module, "runtime_provider_catalog", _boom)

        with caplog.at_level(logging.ERROR):
            response = _service(tmp_path).catalog()

        assert response.status_code == 502
        assert response.body == {"error": "catalog unavailable", "code": "catalog_unavailable"}
        assert "serviceKey" not in str(response.body)
        # The operator still sees which request failed — but not the key (#686). This
        # line used to require the key in the log, pinning the leak it should catch.
        assert "https://apis.data.go.kr/service?serviceKey=[REDACTED]" in caplog.text
        assert "SUPERSECRETKEY" not in caplog.text


class TestUnhandledJobFailure:
    def test_an_unexpected_exception_reports_its_type_not_its_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``GET /builds/{run_id}`` carries this error directly.

        Unexpected exception content is unpredictable, so only type name is exposed —
        reveals which layer failed while avoiding arbitrary internal strings.
        """
        executor = AsyncBuildExecutor(max_workers=1)

        def _boom(*_args: object) -> ServiceResponse:
            raise ConnectionRefusedError(f"cannot reach {_SECRET_URL}")

        try:
            with caplog.at_level(logging.ERROR):
                executor.submit(
                    spec_yaml="dataset_id: x\n",
                    run_id="run-boom",
                    created_by="tester",
                    runner=_boom,
                )
                snapshot = _await_terminal(executor, "run-boom")
        finally:
            executor.shutdown()

        assert snapshot.status == "failed"
        assert snapshot.error == "internal error: ConnectionRefusedError"
        assert "serviceKey" not in (snapshot.error or "")
        # The operator still sees which request failed — but not the key (#686). This
        # line used to require the key in the log, pinning the leak it should catch.
        assert "https://apis.data.go.kr/service?serviceKey=[REDACTED]" in caplog.text
        assert "SUPERSECRETKEY" not in caplog.text


def _await_terminal(executor: AsyncBuildExecutor, run_id: str) -> Any:
    import time

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        snapshot = executor.get(run_id)
        if snapshot is not None and snapshot.status in ("succeeded", "failed", "cancelled"):
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"job {run_id} never reached a terminal state")
