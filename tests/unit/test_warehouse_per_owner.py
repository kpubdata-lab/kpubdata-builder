"""One owner's build cannot replace another owner's warehouse table (#789).

Tables are unique per (workspace, logical name), and every service build committed
into one workspace. Two owners building the same spec shared a table, and each
refresh replaced the other's current snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE, warehouse_workspace
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import TableCatalog

_SPEC = """\
dataset_id: shared.spec
title: Shared
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


class _Result:
    def __init__(self) -> None:
        self.items: Iterable[dict[str, JsonValue]] = ({"id": "1"},)


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


def _service(tmp_path: Path) -> BuilderService:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    return BuilderService(
        output_root=runs, client_factory=lambda **_: _Client(), warehouse_root=tmp_path / "wh"
    )


def _current_runs(tmp_path: Path) -> dict[str, str]:
    catalog = TableCatalog(tmp_path / "wh")
    return {
        table.workspace_id: catalog.get_snapshot(table.current_snapshot_id).run_id
        for table in catalog.list_tables()
        if table.current_snapshot_id is not None
    }


def test_two_owners_get_two_tables_when_ownership_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (negative): Bob's refresh leaves Alice's table as it was."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service = _service(tmp_path)

    assert service.build(_SPEC, run_id="alice-1", manifest_owner_id="oidc:alice").status_code == 200
    assert service.build(_SPEC, run_id="bob-1", manifest_owner_id="oidc:bob").status_code == 200

    current = _current_runs(tmp_path)
    assert sorted(current.values()) == ["alice-1", "bob-1"]
    assert current[warehouse_workspace("oidc:alice")] == "alice-1"
    assert current[warehouse_workspace("oidc:bob")] == "bob-1"


def test_a_single_user_deployment_keeps_its_one_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    service = _service(tmp_path)

    service.build(_SPEC, run_id="first", manifest_owner_id="oidc:alice")
    service.build(_SPEC, run_id="second", manifest_owner_id="oidc:bob")

    assert _current_runs(tmp_path) == {PERSONAL_WORKSPACE: "second"}


def test_the_workspace_name_does_not_carry_the_owner_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")

    workspace = warehouse_workspace("oidc:alice@example.com")

    assert "alice" not in workspace and workspace.startswith("ws_")
    assert workspace == warehouse_workspace("oidc:alice@example.com")
    assert warehouse_workspace(None) == PERSONAL_WORKSPACE
