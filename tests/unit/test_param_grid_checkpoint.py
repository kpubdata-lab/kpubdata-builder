"""A param_grid fetch resumes from its checkpoint; the run is then not reproducible (#648)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.manifest.reproducibility import is_reproducible, reproducible_runs
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.build import build_bronze_artifact
from kpubdata_builder.stages.bronze.checkpoint import INDEX_NAME, CombinationCheckpoint
from kpubdata_builder.stages.bronze.models import CallTotal
from kpubdata_builder.stages.bronze.writer import read_records

_COMBOS: list[dict[str, JsonValue]] = [{"sido": s} for s in ("a", "b", "c", "d")]
_SPEC = """\
dataset_id: grid.table
title: Grid
description: d
sources:
  - provider: datago
    dataset: air_quality
    param_grid:
      sido: [a, b, c, d]
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


class _Result:
    def __init__(self, sido: str) -> None:
        self.items = [{"sido": sido, "n": 1}, {"sido": sido, "n": 2}]


class _Dataset:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def list(self, **params: object) -> _Result:
        sido = cast(str, params["sido"])
        self._client.calls.append(sido)
        if sido == self._client.fail_on:
            raise RuntimeError("provider went away")
        return _Result(sido)


class _Client:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[str] = []

    def dataset(self, _key: str) -> _Dataset:
        return _Dataset(self)


def _total(index: int) -> CallTotal:
    return CallTotal(index=index, value=None, status="unknown", fetched_row_count=2, pages=1)


# -------------------------------------------------------------------- checkpoint


def test_the_checkpoint_keeps_a_fragment_and_an_index_line_per_combination(
    tmp_path: Path,
) -> None:
    checkpoint = CombinationCheckpoint(tmp_path / "c")
    for index in range(3):
        checkpoint.append(index, _COMBOS[index], [{"i": index}], _total(index))

    index_lines = (tmp_path / "c" / INDEX_NAME).read_text(encoding="utf-8").splitlines()
    assert len(index_lines) == 3
    assert sorted(p.name for p in (tmp_path / "c").glob("0*.jsonl")) == [
        "000000.jsonl",
        "000001.jsonl",
        "000002.jsonl",
    ]
    loaded = checkpoint.load(_COMBOS)
    assert sorted(loaded) == [0, 1, 2]
    fragment, total = loaded[1]
    assert list(read_records(fragment)) == [{"i": 1}]
    assert total == _total(1)


def test_records_are_not_packed_into_the_index(tmp_path: Path) -> None:
    """#622: a large combination is never one JSON line holding every record."""
    checkpoint = CombinationCheckpoint(tmp_path / "c")
    checkpoint.append(0, _COMBOS[0], [{"i": n} for n in range(1000)], _total(0))

    (entry,) = [json.loads(line) for line in (tmp_path / "c" / INDEX_NAME).open()]
    assert "records" not in entry
    assert entry["record_count"] == 1000
    assert entry["fragment"] == "000000.jsonl"


def test_a_changed_spec_discards_the_checkpoint(tmp_path: Path) -> None:
    """Negative: records from another expansion are never mixed in."""
    checkpoint = CombinationCheckpoint(tmp_path / "c")
    checkpoint.append(0, {"sido": "old"}, [{"i": 0}], _total(0))

    assert checkpoint.load(_COMBOS) == {}
    assert not (tmp_path / "c").exists()


def test_a_cut_short_last_line_is_ignored(tmp_path: Path) -> None:
    checkpoint = CombinationCheckpoint(tmp_path / "c")
    checkpoint.append(0, _COMBOS[0], [{"i": 0}], _total(0))
    with (tmp_path / "c" / INDEX_NAME).open("a", encoding="utf-8") as handle:
        handle.write('{"index": 1, "params": ')

    assert sorted(checkpoint.load(_COMBOS)) == [0]


def test_a_fragment_without_its_index_line_is_not_a_finished_combination(
    tmp_path: Path,
) -> None:
    """A crash after the fragment and before the index line: fetched again."""
    checkpoint = CombinationCheckpoint(tmp_path / "c")
    checkpoint.append(0, _COMBOS[0], [{"i": 0}], _total(0))
    with checkpoint.fragment(1) as fragment:
        fragment.write_batch([{"i": 1}])
        fragment.close()

    assert sorted(checkpoint.load(_COMBOS)) == [0]


def test_a_fragment_that_lost_lines_discards_the_checkpoint(tmp_path: Path) -> None:
    """Negative: a fragment shorter than its index line says is never resumed from."""
    checkpoint = CombinationCheckpoint(tmp_path / "c")
    checkpoint.append(0, _COMBOS[0], [{"i": 0}, {"i": 1}], _total(0))
    (tmp_path / "c" / "000000.jsonl").write_text('{"i": 0}\n', encoding="utf-8")

    assert checkpoint.load(_COMBOS) == {}
    assert not (tmp_path / "c").exists()


def test_no_key_is_written(tmp_path: Path) -> None:
    """Negative: a provider that echoes the key does not put it on disk (#686)."""
    from kpubdata_builder.stages.bronze.resolve import scrub_secret_values

    checkpoint = CombinationCheckpoint(
        tmp_path / "c", scrub=lambda v: scrub_secret_values(v, ("canary-648",))
    )
    checkpoint.append(0, _COMBOS[0], [{"echo": "serviceKey=canary-648"}], _total(0))

    for path in (tmp_path / "c").iterdir():
        assert "canary-648" not in path.read_text(encoding="utf-8")


# ------------------------------------------------------------------------ bronze


def test_a_rerun_fetches_only_what_is_missing(tmp_path: Path) -> None:
    checkpoint = CombinationCheckpoint(tmp_path / "c")
    failing = _Client(fail_on="c")
    with pytest.raises(RuntimeError):
        build_bronze_artifact(
            failing,
            source_key="datago.air_quality",
            param_combinations=_COMBOS,
            checkpoint=checkpoint,
        )
    assert failing.calls == ["a", "b", "c"]

    working = _Client()
    resumed = build_bronze_artifact(
        working, source_key="datago.air_quality", param_combinations=_COMBOS, checkpoint=checkpoint
    )
    fresh = build_bronze_artifact(
        _Client(), source_key="datago.air_quality", param_combinations=_COMBOS
    )

    assert working.calls == ["c", "d"]
    assert resumed.resumed_combinations == 2
    assert tuple(resumed.iter_records()) == tuple(fresh.iter_records())
    assert [t.index for t in resumed.call_totals] == [0, 1, 2, 3]
    assert fresh.resumed_combinations == 0


# -------------------------------------------------------------------- end to end


def test_a_resumed_build_is_marked_not_reproducible(tmp_path: Path) -> None:
    first = _Client(fail_on="c")
    failing = BuilderService(output_root=tmp_path, client_factory=lambda **_: first)
    assert failing.build(_SPEC, run_id="r1").status_code != 200
    checkpoint = tmp_path / "r1" / "_checkpoints"
    assert list(checkpoint.rglob("*.jsonl"))

    second = _Client()
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: second)
    assert service.build(_SPEC, run_id="r1").status_code == 200

    assert second.calls == ["c", "d"]
    manifest = json.loads((tmp_path / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reproducibility"]["reproducible"] is False
    assert manifest["reproducibility"]["reason"] == "resumed_from_checkpoint"
    (entry,) = manifest["reproducibility"]["resumed_sources"].values()
    assert entry == {"resumed_combinations": 2, "total_combinations": 4}
    assert not list(checkpoint.rglob("*.jsonl"))


def test_a_build_run_start_to_finish_has_no_mark(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())

    assert service.build(_SPEC, run_id="r1").status_code == 200

    manifest = json.loads((tmp_path / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert "reproducibility" not in manifest
    assert is_reproducible(manifest)


def test_the_r1_comparison_leaves_resumed_runs_out() -> None:
    runs: list[tuple[str, dict[str, object]]] = [
        ("whole", {"status": "ok"}),
        ("resumed", {"reproducibility": {"reproducible": False}}),
    ]

    assert reproducible_runs(runs) == ["whole"]
