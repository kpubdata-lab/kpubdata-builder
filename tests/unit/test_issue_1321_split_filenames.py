"""Key splits persist under safe generated file names, labels kept in package.json (#1321).

A key split names each partition after its key value as text: nulls, Korean, negative
numbers, datetimes, ``..``, ``/``, NUL, Windows device names and very long values are all
ordinary values. Gold persistence used that text as the file name and refused anything
outside ``[A-Za-z0-9._-]``, so the build failed at the Gold stage. Names that are safe
keep their file (``train.parquet``, ``2024.parquet``); the others, and names that would
share a file on a case-insensitive filesystem, are written under a name derived from a
digest of the label. ``package.json`` maps every label to its file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.spec import JsonValue, SplitSpec
from kpubdata_builder.stages._path_safety import path_collision_key
from kpubdata_builder.stages._stage_reader import read_gold_summary
from kpubdata_builder.stages.bronze.build import SourceClient
from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
from kpubdata_builder.stages.gold import build_gold_package, persist_gold_package
from kpubdata_builder.stages.gold.persist import split_file_name
from kpubdata_builder.stages.silver import SilverDataset, build_silver_dataset

_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.parquet$")
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{port}{n}" for port in ("COM", "LPT") for n in range(10)
}


def _silver(records: tuple[Mapping[str, JsonValue], ...]) -> SilverDataset:
    bronze = BronzeArtifact.from_records(
        source_key="datago.apt_trade",
        records=tuple(dict(record) for record in records),
        fetched_at=utc_now(),
    )
    return build_silver_dataset(bronze)


def _package_json(path: Path) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], json.loads(path.read_text(encoding="utf-8")))


_AWKWARD_VALUES: tuple[str, ...] = (
    "서울",
    "-5",
    "2024-01-01 00:00:00",
    " padded ",
    "..",
    ".",
    "a/b",
    "../escape",
    "a\\b",
    "nul\x00byte",
    "CON",
    "nul",
    "com1.txt",
    "Lpt9",
    "x" * 300,
    "",
)


class TestAwkwardKeysPersist:
    @pytest.mark.parametrize("value", _AWKWARD_VALUES)
    def test_key_value_persists_under_a_safe_file(self, tmp_path: Path, value: str) -> None:
        silver = _silver(({"id": "1", "k": value}, {"id": "2", "k": "plain"}))
        package = build_gold_package(
            silver, dataset_name="ds", splits_spec=SplitSpec(mode="key", key="k")
        )

        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")

        splits_dir = result.gold_dir / "splits"
        assert set(result.splits_paths) == {value, "plain"}
        for label, path in result.splits_paths.items():
            assert path.parent == splits_dir
            assert _SAFE_FILE.match(path.name), path.name
            assert len(path.name) <= 120
            assert path.name.split(".")[0].upper() not in _WINDOWS_DEVICES
            assert path.is_file()
            rows = pl.read_parquet(path)["k"].to_list()
            assert rows == [label]
        assert result.splits_paths["plain"].name == "plain.parquet"

    def test_null_and_missing_partitions_persist(self, tmp_path: Path) -> None:
        silver = _silver(({"id": "1", "k": None}, {"id": "2", "k": "a"}))
        package = build_gold_package(
            silver, dataset_name="ds", splits_spec=SplitSpec(mode="key", key="k")
        )
        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")
        assert set(result.splits_paths) == {"__null__", "a"}
        assert all(path.is_file() for path in result.splits_paths.values())

        missing = build_gold_package(
            _silver(({"id": "1"},)),
            dataset_name="ds2",
            splits_spec=SplitSpec(mode="key", key="absent"),
        )
        result = persist_gold_package(missing, output_root=tmp_path, run_id="run1")
        assert set(result.splits_paths) == {"__missing__"}
        assert result.splits_paths["__missing__"].is_file()

    def test_negative_integer_keys_persist(self, tmp_path: Path) -> None:
        silver = _silver(({"id": "1", "k": -1}, {"id": "2", "k": 2}))
        package = build_gold_package(
            silver, dataset_name="ds", splits_spec=SplitSpec(mode="key", key="k")
        )
        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")
        assert set(result.splits_paths) == {"-1", "2"}
        assert result.splits_paths["2"].name == "2.parquet"


class TestNoTwoGroupsShareAFile:
    def test_case_colliding_labels_get_distinct_files(self, tmp_path: Path) -> None:
        silver = _silver(
            ({"id": "1", "k": "Seoul"}, {"id": "2", "k": "seoul"}, {"id": "3", "k": "SEOUL"})
        )
        package = build_gold_package(
            silver, dataset_name="ds", splits_spec=SplitSpec(mode="key", key="k")
        )

        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")

        names = [path.name for path in result.splits_paths.values()]
        assert len({path_collision_key(name) for name in names}) == 3
        for label, path in result.splits_paths.items():
            assert pl.read_parquet(path)["k"].to_list() == [label]

    def test_generated_names_never_hit_a_kept_name(self) -> None:
        # A kept label that looks exactly like a generated name still gets its own file.
        generated = split_file_name("서울", ["서울"]).removesuffix(".parquet")
        labels = ["서울", "-5", "x" * 300, "CON", "a", "b", generated, generated.upper()]
        files = {label: split_file_name(label, labels) for label in labels}
        folded = [path_collision_key(name) for name in files.values()]
        assert len(set(folded)) == len(labels)

    def test_names_are_deterministic_and_order_free(self) -> None:
        labels = ["서울", "Seoul", "seoul", "train"]
        forward = {label: split_file_name(label, labels) for label in labels}
        backward = {label: split_file_name(label, list(reversed(labels))) for label in labels}
        assert forward == backward
        assert forward["train"] == "train.parquet"


class TestPackageMetadataRoundTrip:
    def test_package_json_maps_labels_to_files(self, tmp_path: Path) -> None:
        silver = _silver(
            (
                {"id": "1", "k": "서울"},
                {"id": "2", "k": None},
                {"id": "3", "k": "2024"},
                {"id": "4", "k": "서울"},
            )
        )
        package = build_gold_package(
            silver, dataset_name="ds", splits_spec=SplitSpec(mode="key", key="k")
        )

        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")
        meta = _package_json(result.package_path)

        # The counts readers already use keep the labels as they were.
        assert meta["splits"] == {"서울": 2, "__null__": 1, "2024": 1}
        files = cast(dict[str, str], meta["split_files"])
        assert set(files) == {"서울", "__null__", "2024"}
        assert files["2024"] == "splits/2024.parquet"
        for label, relative in files.items():
            assert result.gold_dir / relative == result.splits_paths[label]
            frame = pl.read_parquet(result.gold_dir / relative)
            assert frame.height == cast(dict[str, int], meta["splits"])[label]

    def test_same_input_gives_same_files_and_rows(self, tmp_path: Path) -> None:
        records = tuple({"id": str(i), "k": ["서울", "부산", None][i % 3]} for i in range(9))
        spec = SplitSpec(mode="key", key="k")
        first = persist_gold_package(
            build_gold_package(_silver(records), dataset_name="ds", splits_spec=spec),
            output_root=tmp_path,
            run_id="run1",
        )
        second = persist_gold_package(
            build_gold_package(_silver(records), dataset_name="ds", splits_spec=spec),
            output_root=tmp_path,
            run_id="run2",
        )
        assert (
            _package_json(first.package_path)["split_files"]
            == _package_json(second.package_path)["split_files"]
        )
        for label, path in first.splits_paths.items():
            assert (
                pl.read_parquet(path)["id"].to_list()
                == pl.read_parquet(second.splits_paths[label])["id"].to_list()
            )

    def test_ratio_splits_keep_their_names(self, tmp_path: Path) -> None:
        records = tuple({"id": str(i)} for i in range(10))
        package = build_gold_package(
            _silver(records),
            dataset_name="ds",
            splits_spec=SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, seed=1),
        )
        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")
        meta = _package_json(result.package_path)
        assert meta["split_files"] == {
            "test": "splits/test.parquet",
            "train": "splits/train.parquet",
        }
        assert meta["splits"] == {"test": 2, "train": 8}
        ids = [
            *pl.read_parquet(result.splits_paths["train"])["id"].to_list(),
            *pl.read_parquet(result.splits_paths["test"])["id"].to_list(),
        ]
        assert sorted(ids) == sorted(str(i) for i in range(10))


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    def list(self, **_params: JsonValue) -> _Result:
        return _Result(
            [
                {"id": "1", "region": "서울"},
                {"id": "2", "region": "부산"},
                {"id": "3", "region": "서울"},
                {"id": "4", "region": None},
            ]
        )

    def dataset(self, _key: str) -> _Source:
        return self


def _factory(**_kwargs: object) -> SourceClient:
    return cast(SourceClient, _Source())


def test_a_build_with_korean_key_splits_succeeds_end_to_end(tmp_path: Path) -> None:
    """A whole build through the service: the key split no longer fails at Gold."""
    yaml_text = (
        "dataset_id: demo.table\ntitle: Demo\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: apt_trade\n    alias: m\n"
        "splits:\n  mode: key\n  key: region\n"
        "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )
    service = BuilderService(output_root=tmp_path, client_factory=_factory)
    try:
        built = service.build(yaml_text, run_id="r1")
    finally:
        service._async_builds._executor.shutdown(wait=True)

    assert built.status_code == 200, built.body
    gold = tmp_path / "r1" / "gold" / "m"
    meta = _package_json(gold / "package.json")
    assert meta["splits"] == {"서울": 2, "부산": 1, "__null__": 1}
    files = cast(dict[str, str], meta["split_files"])
    manifest = _package_json(tmp_path / "r1" / "manifest.json")
    outputs = cast(list[str], manifest["outputs"])
    for label, relative in files.items():
        path = gold / relative
        assert str(path) in outputs
        assert pl.read_parquet(path).height == cast(dict[str, int], meta["splits"])[label]
        # Every split file can be downloaded through the artifact route.
        served = service.serve_artifact_file("r1", f"gold/m/{relative}")
        assert not isinstance(served, ServiceResponse), served.body
    summary = read_gold_summary(tmp_path, "r1", "m")
    assert summary is not None
    assert summary.splits == {"서울": 2, "부산": 1, "__null__": 1}
