"""Multi-source Join/Composition BuildSpec and Gold assembly verification (#506)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.pipeline.context import BuildContext
from kpubdata_builder.pipeline.orchestrator import _run_composition
from kpubdata_builder.spec import (
    BuildSpec,
    CompositionSpec,
    ExportTarget,
    JoinSpec,
    JsonValue,
    SourceRef,
    canonical_spec_mapping,
    parse_spec,
)
from kpubdata_builder.spec.validator import validate_spec
from kpubdata_builder.stages.gold.compose import CompositionError, build_composed_gold_package
from kpubdata_builder.stages.silver.models import SilverDataset, ValidationResult
from kpubdata_builder.stages.silver.preview import build_preview
from kpubdata_builder.stages.silver.summarize import build_schema, build_statistics

_SALES = SourceRef(provider="datago", dataset="sales", alias="sales")
_REGION = SourceRef(provider="datago", dataset="region", alias="region")
_EXPORTS = (ExportTarget(kind="jsonl", output_path="data.jsonl"),)


def _make_silver(rows: list[dict[str, JsonValue]]) -> SilverDataset:
    table = pl.DataFrame(rows)
    return SilverDataset(
        table=table,
        schema=build_schema(table),
        statistics=build_statistics(table),
        preview=build_preview(table, limit=5),
        validation=ValidationResult(ok=True),
        source_bronze="x",
    )


def _spec(composition: CompositionSpec | None, **kwargs: object) -> BuildSpec:
    return BuildSpec(
        dataset_id="combined_dataset",
        title="Combined",
        description="combined dataset",
        sources=(_SALES, _REGION),
        exports=_EXPORTS,
        composition=composition,
        **kwargs,  # type: ignore[arg-type]
    )


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **_params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        return _FakeDataset(self._data[source_key])


# --------------------------------------------------------------------------
# spec.validator: composition structure validation
# --------------------------------------------------------------------------


def test_validate_spec_accepts_valid_composition() -> None:
    spec = _spec(
        CompositionSpec(
            name="combined",
            join=JoinSpec(left="sales", right="region", left_key="region_id", right_key="id"),
        )
    )
    validate_spec(spec)  # no exception


def test_validate_spec_rejects_unknown_composition_alias() -> None:
    spec = _spec(
        CompositionSpec(
            name="combined",
            join=JoinSpec(left="sales", right="nope", left_key="region_id", right_key="id"),
        )
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = [p.code for p in (exc_info.value.structured_problems or [])]
    assert "unknown_composition_source" in codes


def test_validate_spec_rejects_self_join() -> None:
    spec = _spec(
        CompositionSpec(
            name="combined",
            join=JoinSpec(left="sales", right="sales", left_key="id", right_key="id"),
        )
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = [p.code for p in (exc_info.value.structured_problems or [])]
    assert "self_join" in codes


def test_validate_spec_rejects_duplicate_alias_when_composition_used() -> None:
    spec = BuildSpec(
        dataset_id="d",
        title="t",
        description="desc",
        sources=(
            SourceRef(provider="datago", dataset="sales", alias="dup"),
            SourceRef(provider="datago", dataset="region", alias="dup"),
        ),
        exports=_EXPORTS,
        composition=CompositionSpec(
            name="combined",
            join=JoinSpec(left="dup", right="dup", left_key="id", right_key="id"),
        ),
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = [p.code for p in (exc_info.value.structured_problems or [])]
    assert "duplicate_source_alias" in codes


def test_validate_spec_rejects_composition_name_collision_with_source_output_key() -> None:
    spec = _spec(
        CompositionSpec(
            name="sales",  # conflicts with sales alias
            join=JoinSpec(left="sales", right="region", left_key="region_id", right_key="id"),
        )
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = [p.code for p in (exc_info.value.structured_problems or [])]
    assert "composition_name_collision" in codes


def test_validate_spec_rejects_blank_join_keys() -> None:
    spec = _spec(
        CompositionSpec(
            name="combined",
            join=JoinSpec(left="sales", right="region", left_key="", right_key="id"),
        )
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    problems = [str(p) for p in (exc_info.value.structured_problems or [])]
    assert any("composition.join.left_key" in p for p in problems)


def test_validate_spec_without_composition_is_unaffected() -> None:
    # Existing multi-source BuildSpec without composition must have no regression
    # (completion condition).
    spec = _spec(None)
    validate_spec(spec)  # no exception


# --------------------------------------------------------------------------
# spec.loader / spec.serializer: parsing and canonical serialization
# --------------------------------------------------------------------------


def test_parse_spec_reads_composition_block() -> None:
    data: dict[str, object] = {
        "dataset_id": "d",
        "title": "t",
        "description": "desc",
        "sources": [
            {"provider": "datago", "dataset": "sales", "alias": "sales"},
            {"provider": "datago", "dataset": "region", "alias": "region"},
        ],
        "exports": [{"kind": "jsonl", "output_path": "data.jsonl"}],
        "composition": {
            "name": "combined",
            "join": {
                "left": "sales",
                "right": "region",
                "left_key": "region_id",
                "right_key": "id",
                "type": "left",
                "on_duplicate_key": "fail",
            },
        },
    }
    spec = parse_spec(data)
    assert spec.composition is not None
    assert spec.composition.name == "combined"
    assert spec.composition.join.type == "left"
    assert spec.composition.join.on_duplicate_key == "fail"


def test_parse_spec_composition_defaults() -> None:
    data: dict[str, object] = {
        "dataset_id": "d",
        "title": "t",
        "description": "desc",
        "sources": [{"provider": "datago", "dataset": "sales", "alias": "sales"}],
        "exports": [{"kind": "jsonl", "output_path": "data.jsonl"}],
        "composition": {
            "name": "combined",
            "join": {"left": "a", "right": "b", "left_key": "k1", "right_key": "k2"},
        },
    }
    spec = parse_spec(data)
    assert spec.composition is not None
    assert spec.composition.join.type == "inner"
    assert spec.composition.join.on_duplicate_key == "warn"


@pytest.mark.parametrize("field", ["type", "on_duplicate_key"])
def test_parse_spec_rejects_unknown_join_vocabulary(field: str) -> None:
    join: dict[str, object] = {"left": "a", "right": "b", "left_key": "k1", "right_key": "k2"}
    join[field] = "bogus"
    data: dict[str, object] = {
        "dataset_id": "d",
        "title": "t",
        "description": "desc",
        "sources": [{"provider": "datago", "dataset": "sales", "alias": "sales"}],
        "exports": [{"kind": "jsonl", "output_path": "data.jsonl"}],
        "composition": {"name": "combined", "join": join},
    }
    with pytest.raises(Exception, match="composition.join"):
        parse_spec(data)


def test_canonical_spec_mapping_includes_composition() -> None:
    spec = _spec(
        CompositionSpec(
            name="combined",
            join=JoinSpec(left="sales", right="region", left_key="region_id", right_key="id"),
        )
    )
    mapping = canonical_spec_mapping(spec)
    assert mapping["composition"] == {
        "name": "combined",
        "join": {
            "left": "sales",
            "right": "region",
            "left_key": "region_id",
            "right_key": "id",
            "type": "inner",
            "on_duplicate_key": "warn",
        },
    }


def test_canonical_spec_mapping_composition_none_by_default() -> None:
    spec = _spec(None)
    mapping = canonical_spec_mapping(spec)
    assert mapping["composition"] is None


# --------------------------------------------------------------------------
# stages.gold.compose: join execution gate (key existence/dtype/duplicate-key)
# --------------------------------------------------------------------------


def test_build_composed_gold_package_inner_join_happy_path() -> None:
    sales = _make_silver([{"id": "1", "region_id": "A"}, {"id": "2", "region_id": "B"}])
    region = _make_silver([{"id": "A", "name": "Seoul"}, {"id": "B", "name": "Busan"}])
    join = JoinSpec(left="sales", right="region", left_key="region_id", right_key="id")

    package, stats = build_composed_gold_package(
        left_silver=sales, right_silver=region, join=join, dataset_name="combined"
    )

    assert package.dataset_name == "combined"
    assert package.source_refs == ("sales", "region")
    assert package.source_silver == "sales+region"
    assert stats.output_row_count == 2
    assert stats.duplicate_key_warning is False


def test_build_composed_gold_package_left_join_keeps_unmatched_rows() -> None:
    sales = _make_silver([{"id": "1", "region_id": "A"}, {"id": "2", "region_id": "Z"}])
    region = _make_silver([{"id": "A", "name": "Seoul"}])
    join = JoinSpec(left="sales", right="region", left_key="region_id", right_key="id", type="left")

    package, stats = build_composed_gold_package(
        left_silver=sales, right_silver=region, join=join, dataset_name="combined"
    )

    assert stats.output_row_count == 2  # unmatched "Z" row survives as null-filled row
    assert package.table.height == 2


def test_build_composed_gold_package_rejects_missing_left_key() -> None:
    sales = _make_silver([{"id": "1"}])
    region = _make_silver([{"id": "A"}])
    join = JoinSpec(left="sales", right="region", left_key="nope", right_key="id")

    with pytest.raises(CompositionError, match="left_key"):
        build_composed_gold_package(
            left_silver=sales, right_silver=region, join=join, dataset_name="combined"
        )


def test_build_composed_gold_package_rejects_dtype_mismatch() -> None:
    sales = _make_silver([{"id": "1", "region_id": 1}])
    region = _make_silver([{"id": "A", "region_id": "1"}])
    join = JoinSpec(left="sales", right="region", left_key="region_id", right_key="region_id")

    with pytest.raises(CompositionError, match="dtype mismatch"):
        build_composed_gold_package(
            left_silver=sales, right_silver=region, join=join, dataset_name="combined"
        )


def test_build_composed_gold_package_warns_on_many_to_many_duplicate_keys() -> None:
    # If both sides have duplicate key "A", rows explode to 2x2=4.
    sales = _make_silver([{"id": "1", "region_id": "A"}, {"id": "2", "region_id": "A"}])
    region = _make_silver([{"id": "A", "name": "S1"}, {"id": "A", "name": "S2"}])
    join = JoinSpec(left="sales", right="region", left_key="region_id", right_key="id")

    package, stats = build_composed_gold_package(
        left_silver=sales, right_silver=region, join=join, dataset_name="combined"
    )

    assert stats.duplicate_key_warning is True
    assert stats.left_row_count == 2
    assert stats.left_distinct_key_count == 1
    assert stats.right_row_count == 2
    assert stats.right_distinct_key_count == 1
    assert stats.output_row_count == 4
    assert package.table.height == 4  # warns only, creates result (default warn)


def test_build_composed_gold_package_fails_closed_on_duplicate_key_when_severity_fail() -> None:
    sales = _make_silver([{"id": "1", "region_id": "A"}, {"id": "2", "region_id": "A"}])
    region = _make_silver([{"id": "A", "name": "S1"}, {"id": "A", "name": "S2"}])
    join = JoinSpec(
        left="sales",
        right="region",
        left_key="region_id",
        right_key="id",
        on_duplicate_key="fail",
    )

    with pytest.raises(CompositionError, match="on_duplicate_key='fail'"):
        build_composed_gold_package(
            left_silver=sales, right_silver=region, join=join, dataset_name="combined"
        )


# --------------------------------------------------------------------------
# pipeline.orchestrator._run_composition: skip/failed branches
# --------------------------------------------------------------------------


def test_run_composition_skips_when_referenced_source_missing(tmp_path: Path) -> None:
    composition = CompositionSpec(
        name="combined",
        join=JoinSpec(left="sales", right="region", left_key="region_id", right_key="id"),
    )
    spec = _spec(composition)
    context = BuildContext.create(spec, output_root=tmp_path, run_id="run1")
    sales = _make_silver([{"id": "1", "region_id": "A"}])

    # region's Silver doesn't exist — mimic failed or not-captured-from-thread state.
    result = _run_composition(composition, silver_by_key={"sales": sales}, context=context)

    assert result.outcome.status == "skipped"
    assert result.provenance is None
    assert "region" in (result.outcome.error or "")


# --------------------------------------------------------------------------
# pipeline.orchestrator.run_build: end-to-end
# --------------------------------------------------------------------------


def _combined_data() -> _FakeClient:
    return _FakeClient(
        {
            "datago.sales": [
                {"id": "1", "region_id": "A"},
                {"id": "2", "region_id": "B"},
                {"id": "3", "region_id": "Z"},
            ],
            "datago.region": [{"id": "A", "name": "Seoul"}, {"id": "B", "name": "Busan"}],
        }
    )


def test_run_build_produces_combined_gold_dataset(tmp_path: Path) -> None:
    composition = CompositionSpec(
        name="combined",
        join=JoinSpec(left="sales", right="region", left_key="region_id", right_key="id"),
    )
    spec = _spec(composition)

    result = run_build(spec, client=_combined_data(), output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    assert result.composition_outcome is not None
    assert result.composition_outcome.status == "ok"

    # Per-source independent Gold remains unchanged (no regression requirement).
    gold_dir = tmp_path / "run1" / "gold"
    assert {p.name for p in gold_dir.iterdir()} == {"sales", "region", "combined"}

    combined_table = pl.read_parquet(gold_dir / "combined" / "table.parquet")
    assert combined_table.height == 2  # region_id "Z" excluded from inner join

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    row_counts = cast(dict[str, int], manifest["row_counts"])
    assert row_counts["combined"] == 2
    assert row_counts["sales"] == 3  # per-source row_count unchanged regardless of assembly

    composition_manifest = cast(dict[str, JsonValue], manifest["composition"])
    assert composition_manifest["output_row_count"] == 2
    assert composition_manifest["left"] == "sales"
    assert composition_manifest["right"] == "region"

    # provenance individually exposed via source_refs (investigation result)
    # — appears two lines in card.
    package_json = json.loads((gold_dir / "combined" / "package.json").read_text(encoding="utf-8"))
    assert package_json["source_refs"] == ["sales", "region"]
    readme = (gold_dir / "combined" / "README.md").read_text(encoding="utf-8")
    assert "- sales" in readme
    assert "- region" in readme


def test_run_build_composition_failure_marks_build_failed_but_keeps_source_outputs(
    tmp_path: Path,
) -> None:
    composition = CompositionSpec(
        name="combined",
        join=JoinSpec(left="sales", right="region", left_key="nope", right_key="id"),
    )
    spec = _spec(composition)

    result = run_build(spec, client=_combined_data(), output_root=tmp_path, run_id="run1")

    assert result.status == "failed"
    assert result.composition_outcome is not None
    assert result.composition_outcome.status == "failed"
    # Per-source outcome remains success regardless of join failure.
    assert all(o.status == "ok" for o in result.outcomes)
    gold_dir = tmp_path / "run1" / "gold"
    assert (gold_dir / "sales").is_dir()
    assert (gold_dir / "region").is_dir()
    assert not (gold_dir / "combined").exists()


def test_run_build_without_composition_is_unaffected(tmp_path: Path) -> None:
    # Existing multi-source BuildSpec without composition has no regression (completion condition).
    spec = _spec(None)

    result = run_build(spec, client=_combined_data(), output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    assert result.composition_outcome is None
    gold_dir = tmp_path / "run1" / "gold"
    assert {p.name for p in gold_dir.iterdir()} == {"sales", "region"}
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["composition"] is None
