"""Source schema contract verification tests (#437, VAL-1).

Verify BuildSpec ``sources[].schema`` declaration (1) parses to SchemaContract in loader,
(2) validator rejects unknown dtype/cast, (3) Silver validation gate catches required/dtype violations.
Previously gate existed but had no pass condition (orchestrator didn't pass arg, always ok).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.errors import ValidationError
from kpubdata_builder.spec import BuildSpec, parse_spec
from kpubdata_builder.spec.validator import validate_spec
from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
from kpubdata_builder.stages.silver.build import build_silver_dataset

_BASE_SOURCE = {"provider": "datago", "dataset": "air_quality"}
_BASE_EXPORTS = [{"kind": "jsonl", "output_path": "o.jsonl"}]


def _spec(sources: list[dict[str, object]]) -> object:
    return parse_spec(
        {
            "dataset_id": "ds",
            "title": "t",
            "description": "d",
            "sources": sources,
            "exports": _BASE_EXPORTS,
        }
    )


class TestSchemaContractParsing:
    """loader._parse_schema → SchemaContract (#437)."""

    def test_parse_required_dtypes_casts(self) -> None:
        spec = _spec(
            [
                {
                    **_BASE_SOURCE,
                    "schema": {
                        "required": ["base_date", "nx"],
                        "dtypes": {"nx": "int64", "base_date": "string"},
                        "casts": {"nx": "int64"},
                    },
                }
            ]
        )
        schema = spec.sources[0].schema
        assert schema is not None
        assert schema.required == ("base_date", "nx")
        assert schema.dtypes == {"nx": "int64", "base_date": "string"}
        assert schema.casts == {"nx": "int64"}

    def test_schema_none_when_not_declared(self) -> None:
        """Undeclared schema is None — backward compatible (#437 argument standard)."""
        spec = _spec([{**_BASE_SOURCE}])
        assert spec.sources[0].schema is None

    def test_schema_partial_fields(self) -> None:
        """Parsing succeeds even with only some fields declared (default empty collection)."""
        spec = _spec([{**_BASE_SOURCE, "schema": {"required": ["x"]}}])
        schema = spec.sources[0].schema
        assert schema is not None
        assert schema.required == ("x",)
        assert schema.dtypes == {}
        assert schema.casts == {}


class TestSchemaContractValidation:
    """validator._schema_problems — reject unknown dtype/cast (#437)."""

    def test_rejects_unknown_dtype(self) -> None:
        spec = _spec([{**_BASE_SOURCE, "schema": {"dtypes": {"nx": "NotARealDtype"}}}])
        with pytest.raises(ValidationError) as exc:
            validate_spec(spec)
        codes = [p.code for p in (exc.value.structured_problems or [])]
        assert "unknown_dtype" in codes

    def test_rejects_unknown_cast(self) -> None:
        spec = _spec([{**_BASE_SOURCE, "schema": {"casts": {"nx": "BogusType"}}}])
        with pytest.raises(ValidationError) as exc:
            validate_spec(spec)
        codes = [p.code for p in (exc.value.structured_problems or [])]
        assert "unknown_cast_dtype" in codes

    def test_accepts_known_dtypes(self) -> None:
        """_NAMED_DTYPES keys (int64/string/float64, etc.) pass."""
        spec = _spec(
            [
                {
                    **_BASE_SOURCE,
                    "schema": {
                        "required": ["x"],
                        "dtypes": {"x": "string"},
                        "casts": {"x": "int64"},
                    },
                }
            ]
        )
        validate_spec(spec)  # No exception.


class TestSchemaContractEnforcement:
    """build_silver_dataset argument passed → Silver validation gate enabled (#437)."""

    @staticmethod
    def _bronze(records: list[dict[str, object]]) -> BronzeArtifact:
        return BronzeArtifact(
            source_key="test",
            raw_records=records,
            fetch_params={},
            fetched_at=utc_now(),
            provenance=None,
        )

    def test_required_missing_fails_validation(self) -> None:
        """Validation fails if required column is absent from actual table (#437)."""
        bronze = self._bronze([{"a": 1}, {"a": 2}])
        silver = build_silver_dataset(bronze, required_columns=("missing_col",))
        assert not silver.validation.ok
        codes = [p.code for p in silver.validation.problems]
        assert "missing_column" in codes

    def test_dtype_mismatch_fails_validation(self) -> None:
        """Validation fails if declared dtype differs from actual (#437)."""
        bronze = self._bronze([{"a": 1}])
        silver = build_silver_dataset(bronze, column_dtypes={"a": "string"})
        assert not silver.validation.ok
        codes = [p.code for p in silver.validation.problems]
        assert "dtype_mismatch" in codes

    def test_matching_contract_passes(self) -> None:
        """ok=True if contract matches actual (positive)."""
        bronze = self._bronze([{"a": 1}, {"a": 2}])
        silver = build_silver_dataset(bronze, required_columns=("a",), column_dtypes={"a": "int64"})
        assert silver.validation.ok

    def test_no_contract_backward_compat(self) -> None:
        """Existing behavior when argument not passed (contract None) — always ok (backward compatible)."""
        bronze = self._bronze([{"a": 1}])
        silver = build_silver_dataset(bronze)
        assert silver.validation.ok


class _FakeResult:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items

    @property
    def items(self) -> list[dict[str, object]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items

    def list(self, **_params: object) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, object]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        return _FakeDataset(self._data[source_key])


class TestTransformRulesReachTheBuild:
    """schema.rename/derived declarations are reflected in Silver outputs via orchestrator (#611)."""

    def test_rename_and_derived_appear_in_the_silver_table(self, tmp_path: Path) -> None:
        import polars as pl

        from kpubdata_builder.pipeline import run_build

        spec = cast(
            BuildSpec,
            _spec(
                [
                    {
                        **_BASE_SOURCE,
                        "dataset": "apt_trade",
                        "schema": {
                            "rename": {"sggCd": "district_code", "dealAmount": "deal_amount"},
                            "casts": {"deal_amount": "int_comma"},
                            "derived": [
                                {
                                    "name": "deal_date",
                                    "kind": "date_parts",
                                    "columns": ["dealYear", "dealMonth", "dealDay"],
                                }
                            ],
                        },
                    }
                ]
            ),
        )
        client = _FakeClient(
            {
                "datago.apt_trade": [
                    {
                        "sggCd": "11110",
                        "dealAmount": "120,000",
                        "dealYear": "2026",
                        "dealMonth": "9",
                        "dealDay": "8",
                    }
                ]
            }
        )

        run_build(spec, client=client, output_root=tmp_path, run_id="run1")

        table = pl.read_parquet(tmp_path / "run1" / "silver" / "datago.apt_trade" / "table.parquet")
        assert "district_code" in table.columns
        assert table["deal_amount"].to_list() == [120000]
        assert table["deal_date"].to_list() == [date(2026, 9, 8)]
