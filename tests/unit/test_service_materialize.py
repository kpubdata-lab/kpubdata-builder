"""The HTTP service reaches the materialise-only end state (#703).

`BuilderService` has accepted a catalog root since #737, but `kpubdata-builder serve`
never passed one, so a deployed service could not commit a snapshot at all. These
tests hold both ends: `serve` wires the root through, and `POST /build` then reports
what it committed — with no export target and no publish credential anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

import kpubdata_builder.service.http as http_module
from kpubdata_builder.cli import main
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import TableCatalog

# No `exports` — the build is meant to end at a table, not at a package (#716).
_SPEC = """\
dataset_id: warehouse-only
title: Warehouse only
description: Ends at a committed table snapshot
sources:
  - provider: datago
    dataset: air_quality
"""

_PUBLISH_ENV = (
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "KAGGLE_USERNAME",
    "KAGGLE_KEY",
    "KPUBDATA_BUILDER_WAREHOUSE",
)


class _Result:
    def __init__(self) -> None:
        self.items: Iterable[dict[str, JsonValue]] = ({"id": "1", "value": 1},)


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _dataset_id: str) -> _Dataset:
        return _Dataset()

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_publish_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PUBLISH_ENV:
        monkeypatch.delenv(name, raising=False)


def _service(tmp_path: Path, warehouse: Path | None) -> BuilderService:
    runs = tmp_path / "runs"
    runs.mkdir()
    return BuilderService(
        output_root=runs,
        client_factory=lambda **_: _Client(),
        warehouse_root=warehouse,
    )


def test_post_build_commits_a_snapshot_without_exports_or_publish_credentials(
    tmp_path: Path,
) -> None:
    warehouse = tmp_path / "warehouse"

    response = _service(tmp_path, warehouse).build(_SPEC, run_id="run1")

    assert response.status_code == 200, response.body
    assert response.body["status"] == "ok"
    materialized = response.body["materialized"]
    assert isinstance(materialized, dict) and set(materialized) == {"datago.air_quality"}
    committed = materialized["datago.air_quality"]
    assert isinstance(committed, dict)

    # The snapshot is readable from the catalog alone: no provider key, no publish
    # credential, nothing but the directory the service was given.
    catalog = TableCatalog(warehouse)
    with catalog.pinned(str(committed["table_id"])) as pin:
        assert pin.snapshot_id == committed["snapshot_id"]
        assert catalog.get_snapshot(pin.snapshot_id).state == "committed"


def test_without_a_warehouse_the_key_is_absent_not_empty(tmp_path: Path) -> None:
    """Absent means "not configured"; `{}` would claim "configured, nothing committed"."""
    response = _service(tmp_path, None).build(_SPEC, run_id="run1")

    assert response.status_code == 200, response.body
    assert "materialized" not in response.body


def _capture_serve(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake_serve(service: object, *, host: str, port: int, max_workers: int) -> None:
        assert isinstance(service, BuilderService)
        seen["warehouse_root"] = service._warehouse_root

    monkeypatch.setattr(http_module, "serve", fake_serve)
    return seen


def test_serve_passes_the_warehouse_flag_to_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_serve(monkeypatch)

    assert main(["serve", "--output-dir", str(tmp_path), "--warehouse", str(tmp_path / "wh")]) == 0

    assert seen["warehouse_root"] == tmp_path / "wh"


def test_serve_reads_the_warehouse_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container entrypoint configures by environment, so the variable must work."""
    monkeypatch.setenv("KPUBDATA_BUILDER_WAREHOUSE", str(tmp_path / "from-env"))
    seen = _capture_serve(monkeypatch)

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0

    assert seen["warehouse_root"] == tmp_path / "from-env"


def test_the_flag_overrides_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_WAREHOUSE", str(tmp_path / "from-env"))
    seen = _capture_serve(monkeypatch)

    assert main(["serve", "--output-dir", str(tmp_path), "--warehouse", str(tmp_path / "wh")]) == 0

    assert seen["warehouse_root"] == tmp_path / "wh"


def test_serve_without_a_warehouse_writes_no_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_serve(monkeypatch)

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0

    assert seen["warehouse_root"] is None
