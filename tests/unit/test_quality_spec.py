"""quality BuildSpec extension (range/compare_columns/severity) parsing/validation/serialization tests (#486)."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
import yaml

from kpubdata_builder.errors import SpecLoadError, ValidationError
from kpubdata_builder.spec import parse_spec
from kpubdata_builder.spec.loader import parse_spec as loader_parse_spec
from kpubdata_builder.spec.models import CompareColumnsRule, QualityPolicy, RangeRule
from kpubdata_builder.spec.serializer import serialize_spec
from kpubdata_builder.spec.validator import validate_spec

_BASE = {
    "dataset_id": "d",
    "title": "t",
    "description": "d",
    "sources": [{"provider": "p", "dataset": "s"}],
    "exports": [{"kind": "jsonl", "output_path": "o.jsonl"}],
}


def _spec_with_quality(quality: dict[str, object]) -> dict[str, object]:
    return {**_BASE, "quality": quality}


class TestSeverityParsing:
    def test_invalid_severity_value_rejected(self) -> None:
        with pytest.raises(SpecLoadError):
            loader_parse_spec(
                _spec_with_quality(
                    {"max_duplicate_rate": 0.1, "max_duplicate_rate_severity": "bad"}
                )
            )

    def test_min_rows_severity_invalid_rejected(self) -> None:
        with pytest.raises(SpecLoadError):
            loader_parse_spec(_spec_with_quality({"min_rows": 1, "min_rows_severity": "bad"}))

    def test_max_null_ratio_severity_invalid_rejected(self) -> None:
        with pytest.raises(SpecLoadError):
            loader_parse_spec(
                _spec_with_quality(
                    {"max_null_ratio": {"a": 0.1}, "max_null_ratio_severity": {"a": "bad"}}
                )
            )


class TestRangeRuleParsing:
    def test_parses_range_rule(self) -> None:
        spec = loader_parse_spec(
            _spec_with_quality({"range": [{"column": "price", "min": 0, "max": 100}]})
        )
        assert spec.quality is not None
        assert spec.quality.range == (
            RangeRule(column="price", min=0.0, max=100.0, severity="warn"),
        )

    def test_range_rule_missing_both_bounds_rejected_by_validator(self) -> None:
        spec = loader_parse_spec(_spec_with_quality({"range": [{"column": "price"}]}))
        with pytest.raises(ValidationError, match="min.*max|range"):
            validate_spec(spec)

    def test_range_rule_inverted_bounds_rejected_by_validator(self) -> None:
        spec = loader_parse_spec(
            _spec_with_quality({"range": [{"column": "price", "min": 100, "max": 0}]})
        )
        with pytest.raises(ValidationError):
            validate_spec(spec)

    def test_range_rule_only_min_is_valid(self) -> None:
        spec = loader_parse_spec(_spec_with_quality({"range": [{"column": "price", "min": 0}]}))
        validate_spec(spec)  # no raise

    def test_range_rule_non_number_min_rejected(self) -> None:
        with pytest.raises(SpecLoadError):
            loader_parse_spec(_spec_with_quality({"range": [{"column": "price", "min": "x"}]}))


class TestCompareColumnsRuleParsing:
    def test_parses_compare_columns_rule(self) -> None:
        spec = loader_parse_spec(
            _spec_with_quality(
                {"compare_columns": [{"left": "a", "operator": "gte", "right": "b"}]}
            )
        )
        assert spec.quality is not None
        assert spec.quality.compare_columns == (
            CompareColumnsRule(left="a", operator="gte", right="b", severity="warn"),
        )

    @pytest.mark.parametrize("operator", ["eq", "ne", "gt", "gte", "lt", "lte"])
    def test_all_valid_operators_accepted(self, operator: str) -> None:
        spec = loader_parse_spec(
            _spec_with_quality(
                {"compare_columns": [{"left": "a", "operator": operator, "right": "b"}]}
            )
        )
        assert spec.quality is not None
        assert spec.quality.compare_columns[0].operator == operator

    def test_invalid_operator_rejected(self) -> None:
        with pytest.raises(SpecLoadError):
            loader_parse_spec(
                _spec_with_quality(
                    {"compare_columns": [{"left": "a", "operator": "eval", "right": "b"}]}
                )
            )

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(SpecLoadError):
            loader_parse_spec(_spec_with_quality({"compare_columns": [{"left": "a"}]}))


class TestQualityValidatorSemantics:
    def test_unknown_severity_column_rejected(self) -> None:
        spec = loader_parse_spec(
            _spec_with_quality(
                {"max_null_ratio": {"a": 0.1}, "max_null_ratio_severity": {"b": "fail"}}
            )
        )
        with pytest.raises(ValidationError, match="max_null_ratio_severity"):
            validate_spec(spec)

    def test_known_severity_column_accepted(self) -> None:
        spec = loader_parse_spec(
            _spec_with_quality(
                {"max_null_ratio": {"a": 0.1}, "max_null_ratio_severity": {"a": "fail"}}
            )
        )
        validate_spec(spec)  # no raise


class TestQualityRoundTrip:
    def test_full_quality_policy_round_trips_through_yaml(self) -> None:
        quality = QualityPolicy(
            max_duplicate_rate=0.02,
            max_duplicate_rate_severity="fail",
            max_null_ratio={"col": 0.1},
            max_null_ratio_severity={"col": "warn"},
            min_rows=10,
            min_rows_severity="fail",
            range=(RangeRule(column="price", min=0.0, max=100.0, severity="fail"),),
            compare_columns=(
                CompareColumnsRule(left="a", operator="gte", right="b", severity="warn"),
            ),
        )
        spec = parse_spec(cast(dict[str, object], _BASE))
        spec = replace(spec, quality=quality)

        text = serialize_spec(spec)
        reparsed = parse_spec(cast(dict[str, object], yaml.safe_load(text)))

        assert reparsed.quality == quality
