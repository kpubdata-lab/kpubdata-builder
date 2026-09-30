"""In a multi-user deployment another owner's run looks like no run at all (#796).

ADR 0012's 2026-09-30 amendment. Run routes answered 403 for another owner's run and 404
for a missing one, so a run id could be probed for existence; publish receipts already
answered 404 for both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.routes import _guards

_SPEC = """\
dataset_id: probe.table
title: Probe
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


class _Result:
    items = [{"id": "1"}]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


@pytest.mark.parametrize(
    "path",
    [
        "/builds/{run}",
        "/builds/{run}/manifest",
        "/builds/{run}/spec",
        "/builds/{run}/stages",
        "/builds/{run}/quality",
        "/builds/{run}/events",
        "/builds/{run}/publish/readiness?target=huggingface",
    ],
)
def test_another_owners_run_answers_like_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())
    built = service.build(
        _SPEC, run_id="alice-run", owner_id="oidc:alice", manifest_owner_id="oidc:alice"
    )
    assert built.status_code == 200
    monkeypatch.setattr(
        app_module, "authenticate", lambda **_: Principal("oidc", "bob", "oidc:bob")
    )

    route, _, query = path.partition("?")
    foreign = dispatch(service, "GET", route.format(run="alice-run"), None, query)
    missing = dispatch(service, "GET", route.format(run="no-such-run"), None, query)

    assert isinstance(foreign, ServiceResponse) and isinstance(missing, ServiceResponse)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.body == {"error": "run not found: alice-run"}


def test_a_single_user_deployment_keeps_403(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_guards.ownership_module, "hides_foreign_runs", lambda: False)

    assert _guards.not_owner("r").status_code == 403
