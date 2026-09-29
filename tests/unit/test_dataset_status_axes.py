"""A table's state is reported per axis, and an axis with no evidence says unknown (#781).

kpubdata's TERMINOLOGY keeps five axes apart — refresh, completeness, health, access,
maturity — because one merged badge makes a user ask why. `GET /datasets` reported
only the last finished run's result.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.datasets import RunRecord, status_axes
from kpubdata_builder.spec import JsonValue

_SPEC = """\
dataset_id: axes.table
title: Axes
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""

_ALICE = Principal("oidc", "alice", "oidc:alice")
_BOB = Principal("oidc", "bob", "oidc:bob")


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items: Iterable[dict[str, JsonValue]] = items


class _Dataset:
    def __init__(self, gate: threading.Event | None) -> None:
        self._gate = gate

    def list(self, **_params: object) -> _Result:
        if self._gate is not None:
            self._gate.wait(timeout=10)
        return _Result([{"id": "1"}])


class _Client:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self._gate = gate

    def dataset(self, _key: str) -> _Dataset:
        return _Dataset(self._gate)


def _record(status: str) -> RunRecord:
    return RunRecord("r1", "axes.table", status, None, None, None, None)


def _axes(response: ServiceResponse) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], response.body["status_axes"])


@pytest.mark.parametrize(
    ("manifest", "status", "expected"),
    [
        ({"row_counts": {"a": 3}}, "ok", "complete"),
        ({"partial": True, "row_counts": {"a": 3}}, "cancelled", "partial"),
        ({"row_counts": {"a": 3}}, "failed", "partial"),
        ({"row_counts": {}}, "failed", "unknown"),
        ({}, "ok", "unknown"),
    ],
    ids=["ok", "cancelled-partial", "failed-with-rows", "failed-empty", "no-manifest"],
)
def test_completeness_follows_the_manifest(
    manifest: dict[str, object], status: str, expected: str
) -> None:
    assert status_axes(manifest, _record(status))["completeness"] == expected


def test_axes_without_evidence_are_unknown_not_guessed() -> None:
    """Negative: nothing Builder has decides health, access or maturity."""
    axes = status_axes({"row_counts": {"a": 1}}, _record("ok"))

    assert axes["health"] == "unknown"
    assert axes["access"] == "unknown"
    assert axes["maturity"] == "unknown"


@pytest.mark.parametrize(
    ("active", "expected"),
    [
        ((), "succeeded"),
        (("queued",), "queued"),
        (("queued", "running"), "running"),
        (("cancelling",), "running"),
    ],
)
def test_an_in_progress_refresh_wins_over_the_last_result(
    active: tuple[str, ...], expected: str
) -> None:
    assert status_axes({"row_counts": {}}, _record("ok"), active)["refresh"] == expected


def test_the_endpoint_reports_every_axis(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())
    assert service.build(_SPEC, run_id="r1").status_code == 200

    listed = service.list_datasets(principal=Principal("dev"))
    detail = service.get_dataset("axes.table", principal=Principal("dev"))

    expected = {
        "refresh": "succeeded",
        "completeness": "complete",
        "health": "unknown",
        "access": "unknown",
        "maturity": "unknown",
    }
    assert cast(list[dict[str, JsonValue]], listed.body["datasets"])[0]["status_axes"] == expected
    assert _axes(detail) == expected


def test_a_running_refresh_shows_only_to_those_who_may_see_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: Bob's queued refresh must not show in Alice's view of the table."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    gate = threading.Event()
    client = _Client()
    service = BuilderService(
        output_root=tmp_path, client_factory=lambda **_: client, async_max_workers=1
    )
    assert (
        service.build(
            _SPEC, run_id="alice-1", owner_id=_ALICE.owner_id, manifest_owner_id=_ALICE.owner_id
        ).status_code
        == 200
    )
    assert service.build(_SPEC, run_id="bob-1", manifest_owner_id=_BOB.owner_id).status_code == 200

    client._gate = gate
    try:
        submitted = service.submit_build(_SPEC, run_id="bob-2", owner_id=_BOB.owner_id)
        assert submitted.status_code == 202

        assert _axes(service.get_dataset("axes.table", principal=_BOB))["refresh"] in {
            "queued",
            "running",
        }
        assert _axes(service.get_dataset("axes.table", principal=_ALICE))["refresh"] == "succeeded"
    finally:
        gate.set()


def test_every_job_transition_keeps_the_table_it_belongs_to() -> None:
    """The running snapshot must still name its table, or the refresh never shows."""
    from kpubdata_builder.service.jobs import AsyncBuildJobRegistry

    registry = AsyncBuildJobRegistry()
    registry.create(run_id="r", created_by=None, dataset_id="axes.table")
    registry.begin_run("r")

    (running,) = registry.active_snapshots()
    assert (running.status, running.dataset_id) == ("running", "axes.table")
