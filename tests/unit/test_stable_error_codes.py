"""Failures a client had to tell apart by their sentence carry a stable code (#1000)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from kpubdata_builder.service.http import _overloaded_response
from tests.unit.test_service_jobs import VALID_SPEC_YAML, _BlockingBuildService


def test_a_full_build_queue_says_so_with_a_code(tmp_path: Path) -> None:
    """429 was shared with ``auth_throttled`` and had no code of its own."""
    entered = threading.Event()
    release = threading.Event()
    service = _BlockingBuildService(
        output_root=tmp_path, entered=entered, release=release, async_max_queue_size=1
    )
    try:
        service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert entered.wait(timeout=5)
        service.submit_build(VALID_SPEC_YAML, run_id="run2", created_by="tester")

        saturated = service.submit_build(VALID_SPEC_YAML, run_id="run3", created_by="tester")
    finally:
        release.set()

    assert saturated.status_code == 429
    assert saturated.body == {"error": "async build queue is full", "code": "build_queue_full"}


def test_the_overload_response_carries_its_code() -> None:
    _head, _, body = _overloaded_response().partition(b"\r\n\r\n")

    assert json.loads(body)["code"] == "server_overloaded"
