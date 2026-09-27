"""Tests for scripts/pipeline/transform.py — schema/validation logic.

Pure logic tests; no network calls, no filesystem side-effects.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import polars as pl
import pytest

_TRANSFORM_PATH = Path(__file__).parents[2] / "scripts" / "pipeline" / "transform.py"


def _load_transform() -> Any:
    spec = importlib.util.spec_from_file_location("scripts.pipeline.transform", _TRANSFORM_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("scripts.pipeline.transform", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


transform_mod = _load_transform()
transform_records = transform_mod.transform_records
validate_schema = transform_mod.validate_schema
build_variant_dataframes = transform_mod.build_variant_dataframes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    column_mapping: dict[str, str],
    *,
    dtypes: dict[str, str] | None = None,
    filters: list[str] | None = None,
    derived: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "source": {"source_url": "https://example.com"},
        "transform": {
            "column_mapping": column_mapping,
            "dtypes": dtypes or {},
            "filters": filters or [],
            "derived": derived or [],
        },
    }


# ---------------------------------------------------------------------------
# transform_records
# ---------------------------------------------------------------------------


def test_transform_basic_column_mapping() -> None:
    records = [{"raw_id": "1", "raw_name": "Seoul"}, {"raw_id": "2", "raw_name": "Busan"}]
    cfg = _config({"raw_id": "id", "raw_name": "name"})

    df = transform_records(records, cfg)

    assert df.columns == ["id", "name"]
    assert df.height == 2
    assert df["id"][0] == "1"
    assert df["name"][1] == "Busan"


def test_transform_missing_raw_key_becomes_none() -> None:
    records = [{"raw_id": "1"}]  # raw_name absent
    cfg = _config({"raw_id": "id", "raw_name": "name"})

    df = transform_records(records, cfg)

    assert df["name"][0] is None


def test_transform_dtype_int_cast() -> None:
    records = [{"amount": "1234"}, {"amount": None}, {"amount": "-"}]
    cfg = _config({"amount": "amount"}, dtypes={"amount": "int"})

    df = transform_records(records, cfg)

    assert df["amount"].dtype == pl.Int64
    assert df["amount"][0] == 1234
    assert df["amount"][1] is None
    assert df["amount"][2] is None  # null token


def test_transform_dtype_int_comma() -> None:
    records = [{"val": "1,234,567"}]
    cfg = _config({"val": "val"}, dtypes={"val": "int_comma"})

    df = transform_records(records, cfg)

    assert df["val"][0] == 1234567


def test_transform_dtype_float_cast() -> None:
    records = [{"price": "3.14"}, {"price": "N/A"}]
    cfg = _config({"price": "price"}, dtypes={"price": "float"})

    df = transform_records(records, cfg)

    assert df["price"].dtype == pl.Float64
    assert abs(df["price"][0] - 3.14) < 1e-9
    assert df["price"][1] is None


def test_transform_filter_greater_than() -> None:
    records = [{"amount": "100"}, {"amount": "200"}, {"amount": "50"}]
    cfg = _config({"amount": "amount"}, dtypes={"amount": "int"}, filters=["amount > 100"])

    df = transform_records(records, cfg)

    assert df.height == 1
    assert df["amount"][0] == 200


def test_transform_filter_equal_string() -> None:
    records = [{"city": "Seoul"}, {"city": "Busan"}, {"city": "Seoul"}]
    cfg = _config({"city": "city"}, filters=["city == Seoul"])

    df = transform_records(records, cfg)

    assert df.height == 2


def test_transform_filter_unknown_column_is_skipped() -> None:
    """A filter referencing a nonexistent column is silently skipped."""
    records = [{"x": "1"}]
    cfg = _config({"x": "x"}, filters=["missing_col > 0"])

    df = transform_records(records, cfg)
    assert df.height == 1


def test_transform_derived_concat_date() -> None:
    records = [{"y": "2023", "m": "3", "d": "5"}]
    cfg = _config(
        {"y": "year", "m": "month", "d": "day"},
        derived=[{"name": "date", "expr": "concat_date(year, month, day)", "dtype": "str"}],
    )

    df = transform_records(records, cfg)

    assert "date" in df.columns
    assert df["date"][0] == "2023-03-05"


def test_transform_derived_format_expr() -> None:
    records = [{"first": "Kim", "last": "Choe"}]
    cfg = _config(
        {"first": "first", "last": "last"},
        derived=[{"name": "full", "expr": "format({} {}, first, last)", "dtype": "str"}],
    )

    df = transform_records(records, cfg)

    assert df["full"][0] == "Kim Choe"


# ---------------------------------------------------------------------------
# validate_schema
# ---------------------------------------------------------------------------


def test_validate_schema_passes_for_exact_match() -> None:
    df = pl.DataFrame({"id": ["1"], "name": ["Seoul"]})
    cfg = _config({"raw_id": "id", "raw_name": "name"})

    # Should not raise or call sys.exit
    validate_schema(df, cfg)


def test_validate_schema_fails_for_undeclared_column(monkeypatch: pytest.MonkeyPatch) -> None:
    df = pl.DataFrame({"id": ["1"], "extra": ["oops"]})
    cfg = _config({"raw_id": "id"})

    with pytest.raises(SystemExit):
        validate_schema(df, cfg)


def test_validate_schema_fails_for_missing_column(monkeypatch: pytest.MonkeyPatch) -> None:
    df = pl.DataFrame({"id": ["1"]})
    cfg = _config({"raw_id": "id", "raw_name": "name"})

    with pytest.raises(SystemExit):
        validate_schema(df, cfg)


def test_validate_schema_warns_for_all_null_column(caplog: pytest.LogCaptureFixture) -> None:
    df = pl.DataFrame({"id": [None, None], "name": ["Seoul", "Busan"]})
    cfg = _config({"raw_id": "id", "raw_name": "name"})

    import logging

    with caplog.at_level(logging.WARNING, logger="publish_to_hf.transform"):
        validate_schema(df, cfg)

    assert any("100%" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# build_variant_dataframes
# ---------------------------------------------------------------------------


def test_build_variant_dataframes_no_variants_returns_default() -> None:
    df = pl.DataFrame({"id": ["1"]})
    cfg: dict[str, Any] = {}

    result = build_variant_dataframes(df, cfg)

    assert list(result.keys()) == ["default"]
    assert result["default"].equals(df)


def test_build_variant_dataframes_column_rename() -> None:
    df = pl.DataFrame({"id": ["1"], "name": ["Seoul"]})
    cfg = {
        "variants": {
            "en": {"column_rename": {"name": "city_name"}},
        }
    }

    result = build_variant_dataframes(df, cfg)

    assert "en" in result
    assert "city_name" in result["en"].columns
    assert "name" not in result["en"].columns


def test_build_variant_dataframes_multiple_variants_independent() -> None:
    df = pl.DataFrame({"id": ["1"], "name": ["Seoul"]})
    cfg = {
        "variants": {
            "ko": {},
            "en": {"column_rename": {"name": "city"}},
        }
    }

    result = build_variant_dataframes(df, cfg)

    assert "ko" in result and "en" in result
    # ko variant should keep original column name
    assert "name" in result["ko"].columns
    # en variant renamed
    assert "city" in result["en"].columns


class TestBuildSpecPathMatchesTheLegacyScript:
    """Both transform paths produce identical output from same Bronze (#611).

    If deploy script path (this module) and BuildSpec path (stages/silver/
    normalize.py) diverge, the pipeline described in papers and the one that
    produced HF datasets differ. We lock three promoted rules (rename /
    int_comma / date_parts) to ensure they actually produce identical results.

    ``deal_date`` representation intentionally differs — script produces string,
    BuildSpec path produces Date. Comparison checks they reference same date.
    """

    def _records(self) -> list[dict[str, Any]]:
        return [
            {
                "sggCd": "11110",
                "dealAmount": "120,000",
                "dealYear": "2026",
                "dealMonth": "9",
                "dealDay": "8",
            },
            {
                "sggCd": "11140",
                "dealAmount": "82,500",
                "dealYear": "2026",
                "dealMonth": "10",
                "dealDay": "15",
            },
        ]

    def _legacy(self) -> pl.DataFrame:
        config = {
            "transform": {
                "column_mapping": {
                    "sggCd": "district_code",
                    "dealAmount": "deal_amount",
                    "dealYear": "deal_year",
                    "dealMonth": "deal_month",
                    "dealDay": "deal_day",
                },
                "dtypes": {"deal_amount": "int_comma"},
                "derived": [
                    {
                        "name": "deal_date",
                        "expr": "concat_date(deal_year, deal_month, deal_day)",
                        "dtype": "str",
                    }
                ],
            }
        }
        return transform_records(self._records(), config)

    def _buildspec(self) -> pl.DataFrame:
        from kpubdata_builder.spec.models import DerivedColumn
        from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
        from kpubdata_builder.stages.silver.normalize import normalize_table

        bronze = BronzeArtifact(
            source_key="datago.apt_trade",
            raw_records=tuple(self._records()),
            fetched_at=utc_now(),
        )
        return normalize_table(
            bronze,
            rename={
                "sggCd": "district_code",
                "dealAmount": "deal_amount",
                "dealYear": "deal_year",
                "dealMonth": "deal_month",
                "dealDay": "deal_day",
            },
            casts={"deal_amount": "int_comma"},
            derived=(
                DerivedColumn(
                    name="deal_date",
                    kind="date_parts",
                    columns=("deal_year", "deal_month", "deal_day"),
                ),
            ),
        )

    def test_rename_and_formatted_cast_agree(self) -> None:
        legacy, buildspec = self._legacy(), self._buildspec()

        assert legacy["district_code"].to_list() == buildspec["district_code"].to_list()
        assert legacy["deal_amount"].to_list() == buildspec["deal_amount"].to_list()

    def test_derived_date_agrees(self) -> None:
        legacy, buildspec = self._legacy(), self._buildspec()

        assert legacy["deal_date"].to_list() == [
            value.isoformat() for value in buildspec["deal_date"].to_list()
        ]
