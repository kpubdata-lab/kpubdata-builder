"""Gold splits in DuckDB: ``hash-sort-v2`` ratio splits and key splits (#871)."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder.spec import SplitSpec
from kpubdata_builder.stages.gold.split import (
    LEGACY_SPLIT_ALGORITHM,
    SPLIT_ALGORITHM,
    apply_splits,
    apply_splits_to_table,
    split_algorithm_of,
)
from kpubdata_builder.tabular.duckdb_load import TableHandle
from tests.support.polars_bridge import handle_from_frame, to_polars

_SPLIT_PY = Path(__file__).parents[2] / "src" / "kpubdata_builder" / "stages" / "gold" / "split.py"
_RATIOS = {"train": 0.7, "val": 0.2, "test": 0.1}


def _table(frame: pl.DataFrame, tmp_path: Path) -> TableHandle:
    return handle_from_frame(frame, workdir=tmp_path)


def _ids(splits: dict[str, TableHandle]) -> dict[str, list[int]]:
    return {name: to_polars(t)["id"].to_list() for name, t in splits.items()}


@pytest.mark.parametrize("total", [0, 1, 2, 3, 7, 10, 101, 1000])
def test_ratio_counts_are_exact(total: int, tmp_path: Path) -> None:
    table = _table(pl.DataFrame({"id": list(range(total))}), tmp_path)

    splits = apply_splits_to_table(table, SplitSpec(mode="ratio", ratios=_RATIOS, seed=7))

    from kpubdata_builder.stages.gold.split import _allocate_counts

    expected = _allocate_counts(total, _RATIOS, sorted(_RATIOS))
    assert {name: t.height for name, t in splits.items()} == expected
    members = sorted(i for ids in _ids(splits).values() for i in ids)
    assert members == list(range(total))


def test_the_same_seed_gives_the_same_membership(tmp_path: Path) -> None:
    spec = SplitSpec(mode="ratio", ratios=_RATIOS, seed=42)
    frame = pl.DataFrame({"id": list(range(200))})

    first = _ids(apply_splits_to_table(_table(frame, tmp_path), spec))
    second = _ids(apply_splits_to_table(_table(frame, tmp_path), spec))

    assert first == second


def test_a_different_seed_changes_membership(tmp_path: Path) -> None:
    frame = pl.DataFrame({"id": list(range(200))})
    one = _ids(
        apply_splits_to_table(
            _table(frame, tmp_path), SplitSpec(mode="ratio", ratios=_RATIOS, seed=1)
        )
    )
    two = _ids(
        apply_splits_to_table(
            _table(frame, tmp_path), SplitSpec(mode="ratio", ratios=_RATIOS, seed=2)
        )
    )

    assert one != two
    assert {k: len(v) for k, v in one.items()} == {k: len(v) for k, v in two.items()}


def test_duplicate_rows_are_told_apart_by_their_ordinal(tmp_path: Path) -> None:
    """Identical rows still land in exact counts: the ordinal, not the values, is hashed."""
    frame = pl.DataFrame({"v": ["same"] * 10})

    splits = apply_splits_to_table(
        _table(frame, tmp_path), SplitSpec(mode="ratio", ratios={"a": 0.5, "b": 0.5}, seed=3)
    )

    assert {name: t.height for name, t in splits.items()} == {"a": 5, "b": 5}


def test_each_split_keeps_the_table_order(tmp_path: Path) -> None:
    splits = apply_splits_to_table(
        _table(pl.DataFrame({"id": list(range(50))}), tmp_path),
        SplitSpec(mode="ratio", ratios=_RATIOS, seed=9),
    )

    for ids in _ids(splits).values():
        assert ids == sorted(ids)


def test_records_and_tables_split_alike(tmp_path: Path) -> None:
    """The records path hashes the same text DuckDB does."""
    records = [{"id": i} for i in range(64)]
    spec = SplitSpec(mode="ratio", ratios=_RATIOS, seed=11)

    by_records = {k: [r["id"] for r in v] for k, v in apply_splits(records, spec).items()}
    by_table = _ids(apply_splits_to_table(_table(pl.DataFrame(records), tmp_path), spec))

    assert by_records == by_table


def test_key_split_sentinels(tmp_path: Path) -> None:
    """``__null__`` holds the nulls and the literal ``"__null__"``, as before (#225)."""
    frame = pl.DataFrame(
        {"id": [0, 1, 2, 3, 4, 5], "k": ["b", None, "__null__", "a", "__missing__", "b"]}
    )

    splits = apply_splits_to_table(_table(frame, tmp_path), SplitSpec(mode="key", key="k"))

    assert _ids(splits) == {
        "b": [0, 5],
        "__null__": [1, 2],
        "a": [3],
        "__missing__": [4],
    }
    assert list(splits) == ["b", "__null__", "a", "__missing__"]


def test_a_missing_key_column_is_all_missing(tmp_path: Path) -> None:
    table = _table(pl.DataFrame({"id": [1, 2]}), tmp_path)

    splits = apply_splits_to_table(table, SplitSpec(mode="key", key="nope"))

    assert _ids(splits) == {"__missing__": [1, 2]}


@pytest.mark.parametrize(
    ("values", "names"),
    [
        ([1, 2, 1], ["1", "2"]),
        ([1.0, 2.5, None], ["1.0", "2.5", "__null__"]),
        ([True, False, True], ["true", "false"]),
        (
            [dt.date(2020, 1, 1), dt.date(2021, 2, 3), None],
            ["2020-01-01", "2021-02-03", "__null__"],
        ),
        (
            [dt.datetime(2020, 1, 1), dt.datetime(2020, 1, 1, 1, 2, 3, 4)],
            ["2020-01-01 00:00:00.000000", "2020-01-01 01:02:03.000004"],
        ),
    ],
)
def test_key_partition_names_are_unchanged(
    values: list[object], names: list[str], tmp_path: Path
) -> None:
    """Named as the Polars splits named them, so a run's split files keep their names."""
    frame = pl.DataFrame({"k": values})
    expected = list(
        dict.fromkeys(frame["k"].cast(pl.Utf8, strict=False).fill_null("__null__").to_list())
    )

    splits = apply_splits_to_table(_table(frame, tmp_path), SplitSpec(mode="key", key="k"))

    assert list(splits) == names == expected


def test_split_algorithm_of_a_manifest() -> None:
    assert split_algorithm_of({"split_algorithm": SPLIT_ALGORITHM}) == "hash-sort-v2"
    # A manifest from before #871 used the shuffle.
    assert split_algorithm_of({}) == LEGACY_SPLIT_ALGORITHM == "shuffle-v1"


def test_no_python_index_list_or_shuffle() -> None:
    source = _SPLIT_PY.read_text(encoding="utf-8")

    assert not re.search(r"^\s*(import random|from random)", source, flags=re.M)
    assert "shuffle(" not in source
    # Polars only names key partitions from their distinct values (until #876).
    assert not re.search(r"^(import polars|from polars)", source, flags=re.M)


def test_the_manifest_records_the_algorithm(tmp_path: Path) -> None:
    from kpubdata_builder.service import BuilderService

    from .test_service import _FakeClient
    from .test_service_publish import LICENSED_SPEC_YAML

    client = _FakeClient({"datago.air_quality": [{"id": str(i), "v": i} for i in range(10)]})
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: client)
    ratio = (
        LICENSED_SPEC_YAML
        + "splits:\n  mode: ratio\n  ratios: {train: 0.8, test: 0.2}\n  seed: 5\n"
    )

    assert service.build(ratio, run_id="r1").status_code == 200
    assert service.build(LICENSED_SPEC_YAML, run_id="r2").status_code == 200

    manifest = json.loads((tmp_path / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["split_algorithm"] == "hash-sort-v2"
    plain = json.loads((tmp_path / "r2" / "manifest.json").read_text(encoding="utf-8"))
    assert "split_algorithm" not in plain
