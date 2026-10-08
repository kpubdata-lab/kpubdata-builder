"""One user cannot hold the whole async build queue (#1189).

The queue counted queued jobs for the whole service, so one user's submissions filled
it and every other user got ``build_queue_full``. In a multi-user deployment each
owner may now have ``KPUBDATA_BUILDER_MAX_ACTIVE_BUILDS_PER_OWNER`` builds queued or
running.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.build_limits import (
    DEFAULT_MAX_ACTIVE_BUILDS_PER_OWNER,
    MAX_ACTIVE_BUILDS_PER_OWNER_ENV,
)
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
_SPEC = (
    "dataset_id: d\ntitle: t\ndescription: d\nsources:\n"
    "  - provider: datago\n    dataset: air_quality\n"
    "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
)


class _HeldBuilds(BuilderService):
    """Every build waits for ``release``; ``started`` counts the ones that began."""

    def __init__(self, tmp_path: Path, *, workers: int = 4, queue: int = 10) -> None:
        super().__init__(
            output_root=tmp_path,
            client_factory=lambda **_: None,
            async_max_workers=workers,
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


def _submit(service: BuilderService, run_id: str, owner: str) -> ServiceResponse:
    return service.submit_build(_SPEC, run_id=run_id, created_by=f"oidc:{owner}", owner_id=owner)


@pytest.fixture()
def multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    monkeypatch.delenv(MAX_ACTIVE_BUILDS_PER_OWNER_ENV, raising=False)


@pytest.mark.usefixtures("multi_user")
def test_one_owner_is_refused_past_the_limit_while_another_is_accepted(tmp_path: Path) -> None:
    service = _HeldBuilds(tmp_path)
    try:
        first = [_submit(service, f"alice-{i}", "alice") for i in range(2)]
        refused = _submit(service, "alice-2", "alice")
        bob = _submit(service, "bob-0", "bob")

        assert [r.status_code for r in first] == [202, 202]
        assert refused.status_code == 429, refused.body
        body = cast(dict[str, Any], refused.body)
        assert body["code"] == "build_owner_limit"
        assert body["limit"] == DEFAULT_MAX_ACTIVE_BUILDS_PER_OWNER == 2
        assert bob.status_code == 202
        # The refused run was not recorded: the same run id can be submitted later.
        assert service.build_status("alice-2").status_code == 404
        contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
        schema = response_schema(contract, "/builds", "post", 429)
        assert schema is not None
        assert validate(refused.body, schema, contract) == []
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_running_builds_count_as_well_as_queued_ones(tmp_path: Path) -> None:
    service = _HeldBuilds(tmp_path, workers=2)
    try:
        assert _submit(service, "a-0", "alice").status_code == 202
        assert _submit(service, "a-1", "alice").status_code == 202
        assert service.started.acquire(timeout=5)
        assert service.started.acquire(timeout=5)
        # Both are running now, none queued.

        assert _submit(service, "a-2", "alice").status_code == 429
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_a_finished_build_frees_its_place(tmp_path: Path) -> None:
    service = _HeldBuilds(tmp_path)
    assert _submit(service, "a-0", "alice").status_code == 202
    assert _submit(service, "a-1", "alice").status_code == 202
    service.release.set()
    for run_id in ("a-0", "a-1"):
        _wait_terminal(service, run_id)

    assert _submit(service, "a-2", "alice").status_code == 202


@pytest.mark.usefixtures("multi_user")
def test_two_submissions_at_once_cannot_both_take_the_last_place(tmp_path: Path) -> None:
    service = _HeldBuilds(tmp_path, workers=1)
    try:
        assert _submit(service, "a-0", "alice").status_code == 202
        barrier = threading.Barrier(8)
        codes: list[int] = []

        def submit(i: int) -> None:
            barrier.wait()
            codes.append(_submit(service, f"a-race-{i}", "alice").status_code)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(codes) == [202] + [429] * 7
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_the_limit_is_a_setting_and_zero_turns_it_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MAX_ACTIVE_BUILDS_PER_OWNER_ENV, "1")
    service = _HeldBuilds(tmp_path)
    try:
        assert _submit(service, "a-0", "alice").status_code == 202
        assert cast(dict[str, Any], _submit(service, "a-1", "alice").body)["limit"] == 1

        monkeypatch.setenv(MAX_ACTIVE_BUILDS_PER_OWNER_ENV, "0")
        assert [_submit(service, f"b-{i}", "alice").status_code for i in range(4)] == [202] * 4
    finally:
        service.release.set()


def test_a_single_user_deployment_has_no_owner_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_OWNERSHIP_ENV, raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    service = _HeldBuilds(tmp_path)
    try:
        codes = [_submit(service, f"a-{i}", "alice").status_code for i in range(5)]

        assert codes == [202] * 5
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_the_whole_queue_still_has_its_own_limit(tmp_path: Path) -> None:
    service = _HeldBuilds(tmp_path, workers=1, queue=2)
    try:
        assert _submit(service, "a-0", "alice").status_code == 202
        assert service.started.acquire(timeout=5)
        assert _submit(service, "b-0", "bob").status_code == 202
        assert _submit(service, "c-0", "carol").status_code == 202

        full = _submit(service, "d-0", "dave")

        assert full.status_code == 429
        assert cast(dict[str, Any], full.body)["code"] == "build_queue_full"
    finally:
        service.release.set()


def _wait_terminal(service: BuilderService, run_id: str) -> None:
    done = threading.Event()
    for _ in range(200):
        status = cast(dict[str, Any], service.build_status(run_id).body).get("status")
        if status in ("succeeded", "failed", "cancelled"):
            return
        done.wait(0.025)
    raise AssertionError(f"{run_id} did not end")
