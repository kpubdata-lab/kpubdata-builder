"""A build whose staging copy fails leaves a reclaimable warehouse (#1333).

The unit tests inject each failure into ``materialize`` directly. This one runs the
whole ``build --warehouse`` command: a first build commits, a second one hits a full
disk while copying Gold into staging, and a third one commits again. The command has to
report the failure, the first table has to stay current, and the third build's own
reclamation pass has to remove what the second left — with no staging directory and no
uncommitted row surviving it.
"""

from __future__ import annotations

import errno
import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from kpubdata_builder import cli
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import SnapshotLayout, TableCatalog

_SPEC = (
    "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
    "  - provider: datago\n    dataset: air_station\n    alias: m\n"
    "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
)
_ROWS: list[dict[str, JsonValue]] = [{"stationName": "A", "pm10": 30 + i} for i in range(22)]


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    def list(self, **_params: object) -> _Result:
        return _Result(list(_ROWS))

    def dataset(self, _key: str) -> _Source:
        return self


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str) -> int:
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(_SPEC, encoding="utf-8")
    monkeypatch.setattr(cli, "_create_client", _Source)
    return cli.main(
        [
            "build",
            str(spec_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--run-id",
            run_id,
            "--warehouse",
            str(tmp_path / "wh"),
        ]
    )


def test_a_full_disk_during_staging_is_reported_and_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _build(tmp_path, monkeypatch, "first") == 0
    (table,) = TableCatalog(tmp_path / "wh").list_tables()
    first = table.current_snapshot_id
    assert first is not None

    original = shutil.copy2

    def copy2(src: Any, dst: Any, **kwargs: Any) -> Any:
        if "_staging" in Path(str(dst)).parts:
            Path(dst).write_bytes(b"PA")
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(src, dst, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copy2", copy2)
        assert _build(tmp_path, monkeypatch, "second") == 1

    manifest = cast(
        dict[str, Any],
        json.loads((tmp_path / "out" / "second" / "manifest.json").read_text("utf-8")),
    )
    assert manifest["warehouse_failures"]["m"]["reason"] == "commit_failed"
    assert "OSError" in manifest["warehouse_failures"]["m"]["detail"]

    catalog = TableCatalog(tmp_path / "wh")
    assert catalog.get_table(table.id).current_snapshot_id == first
    states = {s.run_id: s.state for s in catalog.list_snapshots(table.id)}
    assert states == {"first": "committed", "second": "abandoned"}
    layout = SnapshotLayout(catalog.root, table.id)
    assert not layout.staging_root.exists() or not any(layout.staging_root.iterdir())
    catalog.close()

    assert _build(tmp_path, monkeypatch, "third") == 0
    catalog = TableCatalog(tmp_path / "wh")
    snapshots = catalog.list_snapshots(table.id)
    assert "second" not in {s.run_id for s in snapshots}
    assert {s.state for s in snapshots} == {"committed"}
    current = catalog.get_table(table.id).current_snapshot_id
    assert current is not None
    assert layout.snapshot_dir(current).is_dir()
    assert not layout.staging_root.exists() or not any(layout.staging_root.iterdir())
    catalog.close()
