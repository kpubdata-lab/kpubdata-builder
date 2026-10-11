"""How many async builds may wait is a setting (#1108).

``BuilderService`` held ten queued jobs because ``10`` was written in its signature;
nothing an operator could set changed it. ``KPUBDATA_BUILDER_MAX_QUEUED_BUILDS`` does,
within a range, and a value outside it stops the start.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import pytest

from kpubdata_builder import settings_catalog as catalog
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.build_limits import (
    DEFAULT_MAX_QUEUED_BUILDS,
    MAX_QUEUED_BUILDS_CEILING,
    MAX_QUEUED_BUILDS_ENV,
    resolve_max_queued_builds,
)
from kpubdata_builder.service.startup_settings import check_settings

_SPEC = (
    "dataset_id: d\ntitle: t\ndescription: d\nsources:\n"
    "  - provider: datago\n    dataset: air_quality\n"
    "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
)


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for setting in catalog.SETTINGS:
        monkeypatch.delenv(setting.name, raising=False)


class _HeldBuilds(BuilderService):
    """One worker, and every build waits for ``release``: the rest stay queued."""

    def __init__(self, tmp_path: Path, *, queue: int | None = None) -> None:
        super().__init__(
            output_root=tmp_path,
            client_factory=lambda **_: None,
            async_max_workers=1,
            async_max_queue_size=queue,
        )
        self.release = threading.Event()
        self.started = threading.Semaphore(0)

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        self.started.release()
        self.release.wait(timeout=10)
        return ServiceResponse(200, {"status": "ok", "run_id": run_id})


def _queued_before_refusal(service: _HeldBuilds) -> int:
    """How many builds the service queues behind a running one before it refuses."""
    try:
        assert service.submit_build(_SPEC, run_id="running").status_code == 202
        assert service.started.acquire(timeout=5)
        for count in range(MAX_QUEUED_BUILDS_CEILING + 1):
            response = service.submit_build(_SPEC, run_id=f"queued-{count}")
            if response.status_code != 202:
                assert response.status_code == 429, response.body
                assert cast(dict[str, Any], response.body)["code"] == "build_queue_full"
                return count
        raise AssertionError("the queue never filled")
    finally:
        service.release.set()


def test_the_queue_holds_what_the_setting_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_QUEUED_BUILDS_ENV, "3")

    assert _queued_before_refusal(_HeldBuilds(tmp_path)) == 3


def test_the_queue_holds_ten_when_nothing_is_set(tmp_path: Path) -> None:
    """What it held when the number was written in the code."""
    assert DEFAULT_MAX_QUEUED_BUILDS == 10
    assert _queued_before_refusal(_HeldBuilds(tmp_path)) == 10


def test_a_size_the_caller_gives_takes_the_place_of_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_QUEUED_BUILDS_ENV, "3")

    assert _queued_before_refusal(_HeldBuilds(tmp_path, queue=1)) == 1


@pytest.mark.parametrize(("value", "expected"), [("1", 1), (" 25 ", 25), ("1000", 1000), ("", 10)])
def test_a_size_in_range_is_read(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    monkeypatch.setenv(MAX_QUEUED_BUILDS_ENV, value)

    assert resolve_max_queued_builds() == expected
    assert check_settings().problems == []


@pytest.mark.parametrize("value", ["0", "-1", "1001", "ten", "2.5", "inf"])
def test_a_size_out_of_range_stops_the_start(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(MAX_QUEUED_BUILDS_ENV, value)

    with pytest.raises(ValueError, match="must be an integer from 1 to 1000"):
        resolve_max_queued_builds()
    (problem,) = check_settings().problems
    assert MAX_QUEUED_BUILDS_ENV in problem


def test_the_setting_is_in_the_catalog_and_the_guide() -> None:
    (setting,) = [s for s in catalog.SETTINGS if s.name == MAX_QUEUED_BUILDS_ENV]
    guide = (Path(__file__).resolve().parents[2] / "docs" / "deployment.md").read_text("utf-8")

    assert setting.kind == "integer"
    assert f"| `{MAX_QUEUED_BUILDS_ENV}` |" in guide
