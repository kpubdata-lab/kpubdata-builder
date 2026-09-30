"""Multi-source Join/Composition BuildSpec and Gold assembly verification (#506)."""

from __future__ import annotations

import json
import tempfile
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
from kpubdata_builder.tabular.polars_bridge import handle_from_frame, to_polars

_SALES = SourceRef(provider="datago", dataset="sales", alias="sales")
_REGION = SourceRef(provider="datago", dataset="region", alias="region")
_EXPORTS = (ExportTarget(kind="jsonl", output_path="data.jsonl"),)


def _make_silver(rows: list[dict[str, JsonValue]]) -> SilverDataset:
    table = handle_from_frame(pl.DataFrame(rows), workdir=Path(tempfile.mkdtemp()))
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
# #698: cardinality judged on the keys that intersect
# --------------------------------------------------------------------------


def _keyed(values: list[JsonValue]) -> SilverDataset:
    return _make_silver([{"k": v, "row": str(i)} for i, v in enumerate(values)])


def test_non_intersecting_duplicates_on_both_sides_are_not_a_violation() -> None:
    # Acceptance: left A,A / right B,B are non-unique on both sides but never meet.
    # The pre-#698 structural hint warned here and on_duplicate_key="fail" refused it.
    join = JoinSpec(
        left="l",
        right="r",
        left_key="k",
        right_key="k",
        on_duplicate_key="fail",
        cardinality="one_to_one",
    )

    package, stats = build_composed_gold_package(
        left_silver=_keyed(["A", "A"]),
        right_silver=_keyed(["B", "B"]),
        join=join,
        dataset_name="combined",
    )

    assert stats.duplicate_key_warning is False
    assert stats.observed_cardinality == "one_to_one"
    assert stats.output_row_count == 0
    assert package.table.height == 0
    assert stats.left_unmatched_ratio == 1.0
    assert stats.right_unmatched_ratio == 1.0


def test_key_repeated_on_both_sides_violates_many_to_one() -> None:
    # Acceptance: one key repeated on both sides -> violation for many_to_one.
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k", cardinality="many_to_one")

    with pytest.raises(CompositionError, match="declared cardinality 'many_to_one'") as exc_info:
        build_composed_gold_package(
            left_silver=_keyed(["A", "A", "C"]),
            right_silver=_keyed(["A", "A"]),
            join=join,
            dataset_name="combined",
        )
    message = str(exc_info.value)
    assert "intersecting keys are 'many_to_many'" in message
    assert "k='A'" in message  # the offending key, with its counts
    assert "2 left rows x 2 right rows" in message


def test_key_repeated_on_both_sides_is_allowed_for_many_to_many() -> None:
    # Acceptance: the same input is allowed when many_to_many is declared.
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k", cardinality="many_to_many")

    package, stats = build_composed_gold_package(
        left_silver=_keyed(["A", "A", "C"]),
        right_silver=_keyed(["A", "A"]),
        join=join,
        dataset_name="combined",
    )

    assert stats.observed_cardinality == "many_to_many"
    assert stats.duplicate_key_warning is True  # a real amplification, still reported
    assert stats.output_row_count == 4
    assert package.table.height == 4


def test_many_to_one_fails_when_right_side_repeats_an_intersecting_key() -> None:
    # Acceptance: declared many_to_one, right side not unique on an intersecting key.
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k", cardinality="many_to_one")

    with pytest.raises(CompositionError, match="intersecting keys are 'one_to_many'"):
        build_composed_gold_package(
            left_silver=_keyed(["A", "B"]),
            right_silver=_keyed(["A", "A", "B"]),
            join=join,
            dataset_name="combined",
        )


def test_many_to_one_passes_when_only_the_left_repeats() -> None:
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k", cardinality="many_to_one")

    _, stats = build_composed_gold_package(
        left_silver=_keyed(["A", "A", "B"]),
        # "Z" repeats on the right but has no partner on the left, so it is not a violation.
        right_silver=_keyed(["A", "B", "Z", "Z"]),
        join=join,
        dataset_name="combined",
    )

    assert stats.observed_cardinality == "many_to_one"
    assert stats.output_row_count == 3


@pytest.mark.parametrize(
    ("left_values", "right_values", "observed"),
    [
        (["A", "A"], ["A"], "many_to_one"),
        (["A"], ["A", "A"], "one_to_many"),
    ],
)
def test_one_to_one_fails_when_either_side_is_not_unique(
    left_values: list[JsonValue], right_values: list[JsonValue], observed: str
) -> None:
    # Acceptance: declared one_to_one with either side non-unique -> fail.
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k", cardinality="one_to_one")

    with pytest.raises(CompositionError, match=f"intersecting keys are '{observed}'"):
        build_composed_gold_package(
            left_silver=_keyed(left_values),
            right_silver=_keyed(right_values),
            join=join,
            dataset_name="combined",
        )


def test_composite_key_unique_as_a_pair_is_not_a_violation() -> None:
    # Acceptance: each column alone repeats, but every (region_id, month) pair is unique.
    pairs = [("r1", "m1"), ("r1", "m2"), ("r2", "m1")]
    left = _make_silver(
        [{"region_id": r, "month": m, "trade": str(i)} for i, (r, m) in enumerate(pairs)]
    )
    right = _make_silver([{"rid": r, "mon": m, "pop": str(i)} for i, (r, m) in enumerate(pairs)])
    join = JoinSpec(
        left="l",
        right="r",
        keys=(("region_id", "rid"), ("month", "mon")),
        cardinality="one_to_one",
        on_duplicate_key="fail",
    )

    package, stats = build_composed_gold_package(
        left_silver=left, right_silver=right, join=join, dataset_name="combined"
    )

    assert stats.observed_cardinality == "one_to_one"
    assert stats.duplicate_key_warning is False
    assert stats.keys == (("region_id", "rid"), ("month", "mon"))
    assert stats.left_distinct_key_count == 3
    assert stats.output_row_count == 3
    assert package.table.height == 3


def test_composite_key_checks_dtype_per_pair() -> None:
    left = _make_silver([{"a": "1", "b": 1}])
    right = _make_silver([{"a": "1", "b": "1"}])
    join = JoinSpec(left="l", right="r", keys=(("a", "a"), ("b", "b")))

    with pytest.raises(CompositionError, match="dtype mismatch"):
        build_composed_gold_package(
            left_silver=left, right_silver=right, join=join, dataset_name="combined"
        )


def test_composite_key_reports_missing_column_by_index() -> None:
    left = _make_silver([{"a": "1", "b": "1"}])
    right = _make_silver([{"a": "1"}])
    join = JoinSpec(left="l", right="r", keys=(("a", "a"), ("b", "b")))

    with pytest.raises(CompositionError, match=r"composition\.join\.keys\[1\]\.right"):
        build_composed_gold_package(
            left_silver=left, right_silver=right, join=join, dataset_name="combined"
        )


def test_null_key_rows_are_counted_in_stats() -> None:
    # Acceptance: a null key that drops rows from an inner join is reported, not hidden.
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k")

    package, stats = build_composed_gold_package(
        left_silver=_keyed(["A", None, None]),
        right_silver=_keyed(["A", None]),
        join=join,
        dataset_name="combined",
    )

    assert stats.left_null_key_rows == 2
    assert stats.right_null_key_rows == 1
    assert stats.output_row_count == 1  # null never matches null
    assert package.table.height == 1
    assert stats.left_unmatched_ratio == pytest.approx(2 / 3)
    assert stats.right_unmatched_ratio == pytest.approx(1 / 2)


@pytest.mark.parametrize(
    ("left_values", "right_values", "side"),
    [
        (["A", None], ["A"], "left"),
        (["A"], ["A", None], "right"),
    ],
)
def test_null_key_fails_when_on_null_key_is_fail(
    left_values: list[JsonValue], right_values: list[JsonValue], side: str
) -> None:
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k", on_null_key="fail")

    with pytest.raises(CompositionError, match=f"1 {side} row"):
        build_composed_gold_package(
            left_silver=_keyed(left_values),
            right_silver=_keyed(right_values),
            join=join,
            dataset_name="combined",
        )


def test_null_in_any_composite_key_column_counts_as_a_null_key() -> None:
    left = _make_silver([{"a": "1", "b": "x"}, {"a": "1", "b": None}, {"a": None, "b": "x"}])
    right = _make_silver([{"a": "1", "b": "x"}])
    join = JoinSpec(left="l", right="r", keys=(("a", "a"), ("b", "b")))

    _, stats = build_composed_gold_package(
        left_silver=left, right_silver=right, join=join, dataset_name="combined"
    )

    assert stats.left_null_key_rows == 2
    assert stats.output_row_count == 1


def test_expansion_and_unmatched_ratios() -> None:
    # left  A,A,B,C,null  / right A,B,B,D
    # intersecting: A (2 left x 1 right), B (1 left x 2 right) -> 2 + 2 = 4 output rows.
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k")

    _, stats = build_composed_gold_package(
        left_silver=_keyed(["A", "A", "B", "C", None]),
        right_silver=_keyed(["A", "B", "B", "D"]),
        join=join,
        dataset_name="combined",
    )

    assert stats.output_row_count == 4
    assert stats.expansion_ratio == pytest.approx(4 / 5)
    assert stats.left_unmatched_ratio == pytest.approx(2 / 5)  # C and the null row
    assert stats.right_unmatched_ratio == pytest.approx(1 / 4)  # D
    assert stats.left_null_key_rows == 1
    assert stats.right_null_key_rows == 0
    assert stats.left_distinct_key_count == 3
    assert stats.right_distinct_key_count == 3
    # Each side repeats a key, but no key repeats on both: no many-to-many amplification.
    assert stats.observed_cardinality == "many_to_many"
    assert stats.duplicate_key_warning is False


def test_expansion_ratio_is_none_for_an_empty_left_side() -> None:
    template = _make_silver([{"k": "A"}])
    empty_left = SilverDataset(
        table=handle_from_frame(
            to_polars(template.table).clear(), workdir=Path(tempfile.mkdtemp())
        ),
        schema=template.schema,
        statistics=template.statistics,
        preview=template.preview,
        validation=template.validation,
        source_bronze="x",
    )
    join = JoinSpec(left="l", right="r", left_key="k", right_key="k")

    _, stats = build_composed_gold_package(
        left_silver=empty_left,
        right_silver=_make_silver([{"k": "A"}]),
        join=join,
        dataset_name="combined",
    )

    assert stats.expansion_ratio is None
    assert stats.left_unmatched_ratio == 0.0
    assert stats.right_unmatched_ratio == 1.0


def test_join_spec_normalises_the_shorthand_and_the_keys_form() -> None:
    shorthand = JoinSpec(left="l", right="r", left_key="a", right_key="b")
    assert shorthand.keys == (("a", "b"),)

    composite = JoinSpec(left="l", right="r", keys=(("a", "b"), ("c", "d")))
    assert (composite.left_key, composite.right_key) == ("a", "b")

    with pytest.raises(ValueError, match="not both"):
        JoinSpec(left="l", right="r", left_key="x", right_key="y", keys=(("a", "b"),))


def _spec_with_join(join: object) -> dict[str, object]:
    return {
        "dataset_id": "d",
        "title": "t",
        "description": "desc",
        "sources": [{"provider": "datago", "dataset": "sales", "alias": "sales"}],
        "exports": [{"kind": "jsonl", "output_path": "data.jsonl"}],
        "composition": {"name": "combined", "join": join},
    }


def test_parse_spec_reads_composite_keys_and_cardinality() -> None:
    spec = parse_spec(
        _spec_with_join(
            {
                "left": "trade",
                "right": "population",
                "keys": [
                    {"left": "region_id", "right": "region_id"},
                    {"left": "month", "right": "ym"},
                ],
                "cardinality": "many_to_one",
                "on_null_key": "fail",
            }
        )
    )

    assert spec.composition is not None
    join = spec.composition.join
    assert join.keys == (("region_id", "region_id"), ("month", "ym"))
    assert join.cardinality == "many_to_one"
    assert join.on_null_key == "fail"
    assert (join.left_key, join.right_key) == ("region_id", "region_id")


def test_parse_spec_join_defaults_for_new_fields() -> None:
    spec = parse_spec(
        _spec_with_join({"left": "a", "right": "b", "left_key": "k", "right_key": "k"})
    )

    assert spec.composition is not None
    assert spec.composition.join.cardinality is None
    assert spec.composition.join.on_null_key == "warn"
    assert spec.composition.join.keys == (("k", "k"),)


def test_parse_spec_rejects_both_key_forms() -> None:
    join: dict[str, object] = {
        "left": "a",
        "right": "b",
        "left_key": "k",
        "right_key": "k",
        "keys": [{"left": "k", "right": "k"}],
    }
    with pytest.raises(Exception, match="either keys or left_key/right_key, not both"):
        parse_spec(_spec_with_join(join))


def test_parse_spec_rejects_neither_key_form() -> None:
    with pytest.raises(Exception, match="requires keys"):
        parse_spec(_spec_with_join({"left": "a", "right": "b"}))


@pytest.mark.parametrize(
    "keys",
    [
        [],
        "k",
        [{"left": "k"}],
        [{"left": "k", "right": "k", "extra": "x"}],
    ],
)
def test_parse_spec_rejects_malformed_keys(keys: object) -> None:
    with pytest.raises(Exception, match=r"composition\.join\.keys"):
        parse_spec(_spec_with_join({"left": "a", "right": "b", "keys": keys}))


@pytest.mark.parametrize("field", ["cardinality", "on_null_key"])
def test_parse_spec_rejects_unknown_698_vocabulary(field: str) -> None:
    join: dict[str, object] = {"left": "a", "right": "b", "left_key": "k", "right_key": "k"}
    join[field] = "bogus"
    with pytest.raises(Exception, match=f"composition.join.{field}"):
        parse_spec(_spec_with_join(join))


def test_canonical_mapping_round_trips_composite_keys() -> None:
    join = JoinSpec(
        left="sales",
        right="region",
        keys=(("region_id", "id"), ("month", "ym")),
        cardinality="many_to_one",
        on_null_key="fail",
    )
    spec = _spec(CompositionSpec(name="combined", join=join))

    mapping = canonical_spec_mapping(spec)
    composition = cast(dict[str, JsonValue], mapping["composition"])
    assert composition["join"] == {
        "left": "sales",
        "right": "region",
        "keys": [{"left": "region_id", "right": "id"}, {"left": "month", "right": "ym"}],
        "type": "inner",
        "on_duplicate_key": "warn",
        "cardinality": "many_to_one",
        "on_null_key": "fail",
    }
    reparsed = parse_spec(_spec_with_join(composition["join"]))
    assert reparsed.composition is not None
    assert reparsed.composition.join == join


def test_validate_spec_rejects_blank_composite_key_column() -> None:
    spec = _spec(
        CompositionSpec(
            name="combined",
            join=JoinSpec(left="sales", right="region", keys=(("region_id", "id"), ("", "ym"))),
        )
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    problems = [str(p) for p in (exc_info.value.structured_problems or [])]
    assert any("composition.join.keys[1].left" in p for p in problems)


def test_validate_spec_rejects_repeated_key_column() -> None:
    spec = _spec(
        CompositionSpec(
            name="combined",
            join=JoinSpec(left="sales", right="region", keys=(("a", "x"), ("a", "y"))),
        )
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = [p.code for p in (exc_info.value.structured_problems or [])]
    assert "duplicate_join_key_column" in codes


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
    # #698 fields reach the manifest.
    assert composition_manifest["keys"] == [{"left": "region_id", "right": "id"}]
    assert composition_manifest["cardinality"] is None
    assert composition_manifest["observed_cardinality"] == "one_to_one"
    assert composition_manifest["left_unmatched_ratio"] == pytest.approx(1 / 3)
    assert composition_manifest["right_unmatched_ratio"] == 0.0
    assert composition_manifest["expansion_ratio"] == pytest.approx(2 / 3)
    assert composition_manifest["left_null_key_rows"] == 0
    assert composition_manifest["right_null_key_rows"] == 0

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


class TestNaNJoinKeys:
    """#793: a NaN key is a missing key — counted as null, never matched."""

    def _compose(self, **join: object) -> tuple[object, object]:
        nan = float("nan")
        left = _make_silver([{"k": nan, "a": 1}, {"k": nan, "a": 2}, {"k": 1.0, "a": 3}])
        right = _make_silver([{"k": nan, "b": 4}, {"k": nan, "b": 5}, {"k": 1.0, "b": 6}])
        return build_composed_gold_package(
            left_silver=left,
            right_silver=right,
            join=JoinSpec(left="l", right="r", left_key="k", right_key="k", **join),  # type: ignore[arg-type]
            dataset_name="nan_keys",
        )

    def test_statistics_and_the_join_agree(self) -> None:
        package, stats = self._compose()

        assert stats.left_null_key_rows == 2  # type: ignore[attr-defined]
        assert stats.right_null_key_rows == 2  # type: ignore[attr-defined]
        # Only the real key 1.0 matches: NaN keys no longer pair up into four rows.
        assert stats.output_row_count == 1  # type: ignore[attr-defined]
        assert stats.observed_cardinality == "one_to_one"  # type: ignore[attr-defined]

    def test_on_null_key_fail_catches_nan(self) -> None:
        with pytest.raises(CompositionError, match="null join key"):
            self._compose(on_null_key="fail")
