"""Silver stage (#46): tabularize → validate → summarize → preview → persist verification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest

from kpubdata_builder.errors import TabularError
from kpubdata_builder.spec import ColumnNullTokens as CNT
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
from kpubdata_builder.stages.silver import (
    SilverDataset,
    ValidationResult,
    build_silver_dataset,
    persist_silver_dataset,
)
from kpubdata_builder.stages.silver.normalize import normalize_table as _normalize_handle
from kpubdata_builder.tabular import (
    PreviewSlice,
    SchemaInfo,
    TableStatistics,
    compute_statistics,
    generate_preview,
    infer_schema,
)
from kpubdata_builder.tabular.polars_bridge import handle_from_frame, to_polars


def normalize_table(*args: Any, **kwargs: Any) -> pl.DataFrame:
    """Silver's normalization, read as the Polars frame these assertions were written
    against (#869: the table itself is on DuckDB)."""
    return to_polars(_normalize_handle(*args, **kwargs))


def _bronze(
    records: tuple[Mapping[str, JsonValue], ...], *, source_key: str = "datago.apt_trade"
) -> BronzeArtifact:
    normalized_records = tuple(dict(record) for record in records)
    return BronzeArtifact.from_records(
        source_key=source_key,
        records=normalized_records,
        fetched_at=utc_now(),
    )


class TestBuildSilverDataset:
    def test_produces_dataset_with_table_schema_stats_preview(self) -> None:
        bronze = _bronze(
            (
                {"id": "1", "amount": 1000, "district": "강남구"},
                {"id": "2", "amount": 2500, "district": "서초구"},
            )
        )

        dataset = build_silver_dataset(bronze)

        assert isinstance(dataset, SilverDataset)
        assert isinstance(to_polars(dataset.table), pl.DataFrame)
        assert to_polars(dataset.table).shape == (2, 3)
        assert isinstance(dataset.schema, SchemaInfo)
        assert [c.name for c in dataset.schema.columns] == ["id", "amount", "district"]
        assert isinstance(dataset.statistics, TableStatistics)
        assert dataset.statistics.row_count == 2
        assert isinstance(dataset.preview, PreviewSlice)
        assert dataset.source_bronze == "datago.apt_trade"

    def test_validation_passes_when_required_columns_present(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        dataset = build_silver_dataset(bronze, required_columns=("id", "amount"))

        assert isinstance(dataset.validation, ValidationResult)
        assert dataset.validation.ok is True
        assert dataset.validation.problems == ()

    def test_validation_fails_when_required_column_missing(self) -> None:
        bronze = _bronze(({"id": "1"},))

        dataset = build_silver_dataset(bronze, required_columns=("id", "amount"))

        assert dataset.validation.ok is False
        # Changed to ValidationProblem object, verify message field (#261)
        assert any("amount" in problem.message for problem in dataset.validation.problems)

    def test_validation_passes_when_dtype_matches(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        dataset = build_silver_dataset(
            bronze, casts={"amount": "int"}, column_dtypes={"amount": "int"}
        )

        assert dataset.validation.ok is True
        assert dataset.validation.problems == ()

    def test_validation_fails_when_dtype_mismatches(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        # amount reads as Int64; requesting Float64 must fail
        dataset = build_silver_dataset(bronze, column_dtypes={"amount": "float"})

        assert dataset.validation.ok is False
        assert any("amount" in p.message for p in dataset.validation.problems)

    def test_validation_reports_missing_column_for_dtype_spec(self) -> None:
        bronze = _bronze(({"id": "1"},))

        # Missing 'amount' column must include dtype validation error message
        dataset = build_silver_dataset(bronze, column_dtypes={"amount": "int"})

        assert dataset.validation.ok is False
        assert any("amount" in p.message for p in dataset.validation.problems)

    def test_preview_respects_limit(self) -> None:
        records = tuple({"n": i} for i in range(10))
        dataset = build_silver_dataset(_bronze(records), preview_limit=3)

        assert dataset.preview.total_rows == 10
        assert len(dataset.preview.rows) == 3

    def test_optional_casts_apply_declared_dtypes(self) -> None:
        bronze = _bronze(({"id": "1", "amount": "1000"}, {"id": "2", "amount": "2500"}))

        dataset = build_silver_dataset(bronze, casts={"amount": "int"})

        assert to_polars(dataset.table).schema["amount"] == pl.Int64

    def test_cast_data_loss_raises_instead_of_silently_nulling(self) -> None:
        # Declared cast dropping value to null must fail with TabularError, not silently (#188).
        bronze = _bronze(({"id": "1", "amount": "1000"}, {"id": "2", "amount": "oops"}))

        with pytest.raises(TabularError, match="data loss"):
            _ = build_silver_dataset(bronze, casts={"amount": "int"})

    def test_rejects_negative_preview_limit(self) -> None:
        # Negative preview_limit rejected early, doesn't leak via df.head(-1) (#190).
        bronze = _bronze(({"id": "1"},))

        with pytest.raises(ValueError, match="preview_limit"):
            _ = build_silver_dataset(bronze, preview_limit=-1)


class TestRowPreservingInvariant:
    """Fix invariant that Bronze-Silver doesn't filter/dedup/reorder rows (#497).

    Source-Silver diff in pipeline.preview compares ``tuple(bronze.iter_records())[i]`` with
    This invariant that ``silver.table`` row i is always same logical row
    depends on (actual basis for diff_available judgment). normalize_table()
    records_to_dataframe() calls only per-column ops; validate_table() never
    touches table so this invariant holds today, but future changes need care.
    Adding dedup/filter/reorder to Silver breaks test, signaling need to review
    alignment assumptions in pipeline/preview.py.
    """

    def test_coalesce_drops_columns_not_rows(self) -> None:
        # #620 - coalesce consumes candidate *columns*. Touching rows breaks preview
        # Source↔Silver alignment drifts silently.
        records = tuple(
            {"a": str(i) if i % 2 == 0 else None, "b": None if i % 2 == 0 else str(i)}
            for i in range(20)
        )
        bronze = _bronze(records)

        dataset = build_silver_dataset(bronze, coalesce={"merged": ("a", "b")})

        assert to_polars(dataset.table).height == len(records)
        assert to_polars(dataset.table)["merged"].to_list() == [str(i) for i in range(20)]

    def test_row_count_is_preserved(self) -> None:
        records = tuple({"id": str(i), "amount": i * 100} for i in range(50))
        bronze = _bronze(records)

        dataset = build_silver_dataset(bronze, casts={"amount": "float"})

        assert to_polars(dataset.table).height == len(records)
        assert dataset.statistics.row_count == len(records)

    def test_row_order_is_preserved_across_normalize_and_validate(self) -> None:
        # Using id as fingerprint of original order; order unchanged after cast/validation
        # verify - even if values change (#497 diff purpose), row *positions* same as original
        # must correspond 1:1.
        records = tuple({"id": str(i), "amount": str(i * 100)} for i in range(20))
        bronze = _bronze(records)

        dataset = build_silver_dataset(
            bronze,
            casts={"amount": "int"},
            required_columns=("id", "amount"),
            column_dtypes={"amount": "int"},
        )

        assert to_polars(dataset.table)["id"].to_list() == [r["id"] for r in records]
        assert dataset.validation.ok is True

    def test_row_order_is_preserved_without_declared_casts(self) -> None:
        # Even without casts (most common preview path), order preservation holds equally.
        records = tuple({"id": str(i), "label": chr(ord("a") + i)} for i in range(10))
        bronze = _bronze(records)

        dataset = build_silver_dataset(bronze)

        assert to_polars(dataset.table)["id"].to_list() == [r["id"] for r in records]


class TestPersistSilverDataset:
    def test_writes_parquet_and_json_sidecars(self, tmp_path: Path) -> None:
        bronze = _bronze(
            (
                {"id": "1", "amount": 1000},
                {"id": "2", "amount": 2500},
            )
        )
        dataset = build_silver_dataset(bronze, required_columns=("id",))

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        assert result.table_path.exists()
        assert result.schema_path.exists()
        assert result.stats_path.exists()
        assert result.preview_path.exists()
        assert result.validation_path.exists()

        # parquet round-trip
        assert pl.read_parquet(result.table_path).to_dicts() == to_polars(dataset.table).to_dicts()

        # json sidecars are well-formed and reflect the dataset
        stats = cast(
            dict[str, JsonValue], json.loads(result.stats_path.read_text(encoding="utf-8"))
        )
        assert stats["row_count"] == 2
        validation = cast(
            dict[str, JsonValue], json.loads(result.validation_path.read_text(encoding="utf-8"))
        )
        assert validation["ok"] is True

    def test_rejects_unsafe_run_id(self, tmp_path: Path) -> None:
        dataset = build_silver_dataset(_bronze(({"id": "1"},)))

        with pytest.raises(ValueError, match="run_id"):
            _ = persist_silver_dataset(dataset, output_root=tmp_path, run_id="../escape")

    def test_serializes_date_values_as_iso_strings(self, tmp_path: Path) -> None:
        # Columns cast to Date/Datetime in preview don't break persist,
        # verify serialization as ISO string (#93 review).
        bronze = _bronze(({"d": "2025-01-01"}, {"d": "2025-01-02"}))
        dataset = build_silver_dataset(bronze, casts={"d": "date"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["d"] == "2025-01-01"

    def test_stage_sample_keeps_precision_and_records_the_encoding(self, tmp_path: Path) -> None:
        # #735: preview.json is served as stage detail's sample. An out-of-range integer
        # and a Decimal are stored as exact decimal text, and schema.json says so. A
        # Decimal column used to make this persist step raise TypeError.
        from decimal import Decimal

        bronze = _bronze(
            (
                {"n": 9007199254740993, "amount": Decimal("12.50")},
                {"n": 1, "amount": Decimal("0.10")},
            )
        )
        dataset = build_silver_dataset(bronze)

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0] == {"n": "9007199254740993", "amount": "12.50"}
        schema = json.loads(result.schema_path.read_text(encoding="utf-8"))
        encodings = {c["name"]: c["wire_encoding"] for c in schema["columns"]}
        assert encodings == {"n": "decimal_string", "amount": "decimal_string"}

    def test_serializes_naive_datetime_values_as_iso_strings(self, tmp_path: Path) -> None:
        # Columns cast to naive datetime in preview appear as ISO string without offset
        # verify serialization (#97 datetime regression).
        bronze = _bronze(({"ts": "2025-01-01T12:30:00"}, {"ts": "2025-01-02T08:00:00"}))
        dataset = build_silver_dataset(bronze, casts={"ts": "datetime"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["ts"] == "2025-01-01T12:30:00"
        assert rows[1]["ts"] == "2025-01-02T08:00:00"

    def test_serializes_timezone_aware_datetime_values_as_iso_strings(self, tmp_path: Path) -> None:
        # Timezone-aware datetime columns normalized to UTC then ISO with +00:00 offset
        # Serialized as string (different input tz converge to same UTC instant). cast map
        # Only creates naive Datetime, construct aware table directly (#97 datetime regression).
        kst = timezone(timedelta(hours=9))
        table = pl.DataFrame(
            {
                "ts": [
                    datetime(2025, 1, 1, 12, 30, tzinfo=timezone.utc),
                    datetime(2025, 1, 2, 17, 0, tzinfo=kst),
                ]
            }
        )
        assert table.schema["ts"].time_zone is not None
        dataset = SilverDataset(
            table=handle_from_frame(table, workdir=tmp_path),
            schema=infer_schema(table),
            statistics=compute_statistics(table),
            preview=generate_preview(table),
            validation=ValidationResult(ok=True),
            source_bronze="datago.apt_trade",
        )

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        # Both inputs normalized to UTC with +00:00 offset (KST 17:00 == UTC 08:00).
        assert rows[0]["ts"] == "2025-01-01T12:30:00+00:00"
        assert rows[1]["ts"] == "2025-01-02T08:00:00+00:00"

    def test_serializes_datetime_with_microseconds(self, tmp_path: Path) -> None:
        # Datetime with microseconds not truncated, serializes to ISO fractional seconds.
        bronze = _bronze(({"ts": "2025-01-01T12:30:00.123456"},))
        dataset = build_silver_dataset(bronze, casts={"ts": "datetime"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["ts"] == "2025-01-01T12:30:00.123456"


class TestColumnRename:
    """Change original API field name to canonical column name (#611).

    Paper experiment Silver layer maps original field names like ``sggCd`` to ``district_code``
    Must be canonical dataset after transformation. BuildSpec path had no rename mechanism
    absent; only existed in deploy script (scripts/pipeline/transform.py).
    """

    def test_renames_declared_columns(self) -> None:
        bronze = _bronze(({"sggCd": "11110", "aptNm": "은마"},))

        table = normalize_table(bronze, rename={"sggCd": "district_code", "aptNm": "apt_name"})

        assert table.columns == ["district_code", "apt_name"]

    def test_missing_source_column_surfaces_as_tabular_error(self) -> None:
        # R2 (source evolution) must count upstream field disappearance as schema breakage
        # Polars ColumnNotFoundError leaking as-is, which column
        # caller can't tell if disappeared.

        bronze = _bronze(({"sggCd": "11110"},))

        with pytest.raises(TabularError) as exc:
            normalize_table(bronze, rename={"aptNm": "apt_name"})

        assert "aptNm" in str(exc.value)


class TestFormattedNumericCast:
    """Cast amount string with thousand separators to number (#611).

    Declaring ``dealAmount: int`` makes ``"120,000"`` entirely null, #188
    data-loss guard fails build. Source public data uses this format for amounts
    provided; without declaration means Silver build itself fails.
    """

    def test_comma_separated_decimal_casts_to_float(self) -> None:
        # Area/amount columns get separator only if ≥1000. Same column
        # notation differs inside, not handling delimiter makes only large values missing
        # average skewed downward.
        bronze = _bronze(({"area": "84.5"}, {"area": "2,436.26"}))

        table = normalize_table(bronze, casts={"area": "float_comma"})

        assert table["area"].to_list() == [84.5, 2436.26]

    def test_comma_separated_amount_casts_to_integer(self) -> None:
        bronze = _bronze(({"dealAmount": "120,000"}, {"dealAmount": "82,500"}))

        table = normalize_table(bronze, casts={"dealAmount": "int_comma"})

        assert table["dealAmount"].to_list() == [120000, 82500]


class TestDerivedColumns:
    """Create new column from existing column (#611).

    Source data splits transaction date into year/month/day columns for cross-dataset joins.
    Without both declared, Silver cannot be canonical dataset.
    """

    def test_date_parts_compose_a_date_column(self) -> None:
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"dealYear": "2026", "dealMonth": "9", "dealDay": "8"},))

        table = normalize_table(
            bronze,
            derived=(
                DerivedColumn(
                    name="deal_date",
                    kind="date_parts",
                    columns=("dealYear", "dealMonth", "dealDay"),
                ),
            ),
        )

        assert table["deal_date"].to_list() == [date(2026, 9, 8)]

    @pytest.mark.parametrize(
        ("month", "day"),
        [("13", "1"), ("2", "30"), ("0", "5")],
    )
    def test_date_parts_that_form_no_date_fail_instead_of_turning_null(
        self, month: str, day: str
    ) -> None:
        # If all 3 chunks present but not a date, value disappeared. cast same
        # loss makes #188 raise build, but derived rules silently null
        # Swapped month/day source loses half required dates but passes.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"dealYear": "2026", "dealMonth": month, "dealDay": day},))

        with pytest.raises(TabularError, match="data loss"):
            normalize_table(
                bronze,
                derived=(
                    DerivedColumn(
                        name="deal_date",
                        kind="date_parts",
                        columns=("dealYear", "dealMonth", "dealDay"),
                    ),
                ),
            )

    def test_date_parts_with_a_missing_part_stay_null_without_failing(self) -> None:
        # Rows where chunk already gone is not lost by rule - same standard as cast audit
        # (only null growth counted as loss).
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(
            (
                {"dealYear": "2026", "dealMonth": "9", "dealDay": "8"},
                {"dealYear": "2026", "dealMonth": None, "dealDay": "8"},
            )
        )

        table = normalize_table(
            bronze,
            derived=(
                DerivedColumn(
                    name="deal_date",
                    kind="date_parts",
                    columns=("dealYear", "dealMonth", "dealDay"),
                ),
            ),
        )

        assert table["deal_date"].to_list() == [date(2026, 9, 8), None]

    def test_date_parts_output_named_after_an_input_is_rejected(self) -> None:
        # If name is one of input columns, with_columns silently
        # overwrites. Moreover after overwriting, invalid date rows make chunk itself null
        # shows; loss audit misses what it caught. Reject as declaration error.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"dealYear": "2026", "dealMonth": "30", "dealDay": "2"},))

        with pytest.raises(TabularError, match="overwrite"):
            normalize_table(
                bronze,
                derived=(
                    DerivedColumn(
                        name="dealMonth",
                        kind="date_parts",
                        columns=("dealYear", "dealMonth", "dealDay"),
                    ),
                ),
            )

    def test_derived_name_must_not_overwrite_an_unrelated_column(self) -> None:
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"a": "x", "b": "y", "k": "ORIGINAL"},))

        with pytest.raises(TabularError, match="overwrite"):
            normalize_table(
                bronze, derived=(DerivedColumn(name="k", kind="join_key", columns=("a", "b")),)
            )

    def test_rename_target_must_not_collide_with_an_untouched_column(self) -> None:
        # Fails as TabularError (spec term), not Polars DuplicateError.
        bronze = _bronze(({"sggCd": "11110", "district_code": "already"},))

        with pytest.raises(TabularError, match="collide"):
            normalize_table(bronze, rename={"sggCd": "district_code"})

    def test_rename_swap_is_allowed(self) -> None:
        # Swapping renames not conflict - both source columns are rename targets.
        bronze = _bronze(({"a": 1, "b": 2},))

        table = normalize_table(bronze, rename={"a": "b", "b": "a"})

        assert table.columns == ["b", "a"]

    def test_join_key_concatenates_columns_into_one(self) -> None:
        # T3 (sales × lease) needs 4-key join but composition equi-join
        # Takes single column only. If composite key created in Silver, compose.py
        # can express same join without touching.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"district_code": "11110", "year_month": "202609"},))

        table = normalize_table(
            bronze,
            derived=(
                DerivedColumn(
                    name="join_key",
                    kind="join_key",
                    columns=("district_code", "year_month"),
                ),
            ),
        )

        assert table["join_key"].to_list() == ["11110|202609"]

    def test_join_key_separator_in_value_does_not_collide(self) -> None:
        # Concatenating delimiters directly makes ("a|b", "c") and ("a", "b|c") same key
        # unrelated rows join. Escape components to keep encoding injective.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(
            (
                {"left": "a|b", "right": "c"},
                {"left": "a", "right": "b|c"},
            )
        )

        table = normalize_table(
            bronze,
            derived=(DerivedColumn(name="join_key", kind="join_key", columns=("left", "right")),),
        )

        keys = table["join_key"].to_list()
        assert keys[0] != keys[1]
        assert keys == ["a\\|b|c", "a|b\\|c"]

    def test_join_key_escape_character_in_value_does_not_collide(self) -> None:
        # Escape char itself can appear in value. Without doubling
        # ("a\\", "b") and ("a", "\\b") collapse to same key again.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(
            (
                {"left": "a\\", "right": "b"},
                {"left": "a", "right": "\\b"},
            )
        )

        table = normalize_table(
            bronze,
            derived=(DerivedColumn(name="join_key", kind="join_key", columns=("left", "right")),),
        )

        keys = table["join_key"].to_list()
        assert keys[0] != keys[1]


class TestSchemaContractReachesNormalization:
    """BuildSpec rename/derived declarations reach actual Silver table (#611).

    Even if normalize_table has capability, if build_silver_dataset does not pass
    it, declaration has no effect. Fix generation boundary.
    """

    def test_build_silver_dataset_applies_rename_and_derived(self) -> None:
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(
            ({"sggCd": "11110", "dealYear": "2026", "dealMonth": "9", "dealDay": "8"},)
        )

        dataset = build_silver_dataset(
            bronze,
            rename={"sggCd": "district_code"},
            derived=(
                DerivedColumn(
                    name="deal_date",
                    kind="date_parts",
                    columns=("dealYear", "dealMonth", "dealDay"),
                ),
            ),
        )

        assert "district_code" in to_polars(dataset.table).columns
        assert to_polars(dataset.table)["deal_date"].to_list() == [date(2026, 9, 8)]


class TestSourceTypeDeclaration:
    """Declare what type to read source column as (#611 follow-up).

    Government real estate data gives same column different types per record - ``jibun``
    mostly string but integer in some records. records_to_dataframe() silent
    type coercion rejection raises TabularError (#187). Without declaration Silver build
    fails. Only allow declared columns, do not remove rejection.
    """

    def test_mixed_type_column_without_declaration_still_fails(self) -> None:
        from kpubdata_builder.errors import TabularError

        bronze = _bronze(({"jibun": "702"}, {"jibun": 69}))

        with pytest.raises(TabularError, match="heterogeneous"):
            normalize_table(bronze)

    def test_declared_column_is_read_as_text(self) -> None:
        bronze = _bronze(({"jibun": "702"}, {"jibun": 69}))

        table = normalize_table(bronze, read_as={"jibun": "str"})

        assert table["jibun"].to_list() == ["702", "69"]

    def test_declaration_preserves_nulls(self) -> None:
        # aptDong is 65% null. If declaration makes null "None" string
        # Missing rate measurement breaks entirely.
        bronze = _bronze(({"aptDong": "105"}, {"aptDong": 205}, {"aptDong": None}))

        table = normalize_table(bronze, read_as={"aptDong": "str"})

        assert table["aptDong"].to_list() == ["105", "205", None]


class TestNullTokenNormalization:
    """Convert source notation for missing to null (#611 follow-up).

    Some sources use empty string and None for missing. Keep empty string unchanged
    and declare numeric cast, #188 data-loss guard fails build - value
    became null because original was not data but "missing" notation.
    Consolidate missing into one notation, not erase it. Count preserves quality metrics.
    """

    def test_empty_string_blocks_a_numeric_cast_without_declaration(self) -> None:
        from kpubdata_builder.errors import TabularError

        bronze = _bronze(({"area": "84.5"}, {"area": ""}))

        with pytest.raises(TabularError, match="data loss"):
            normalize_table(bronze, casts={"area": "float"})

    def test_declared_null_token_becomes_null_before_casting(self) -> None:
        bronze = _bronze(({"area": "84.5"}, {"area": ""}))

        table = normalize_table(bronze, casts={"area": "float"}, null_tokens=("",))

        assert table["area"].to_list() == [84.5, None]

    def test_null_tokens_do_not_touch_undeclared_values(self) -> None:
        # If "-" not declared as missing, leave as-is. What to treat as missing
        # Differs per dataset; if builder decides arbitrarily, measurement contaminates.
        bronze = _bronze(({"grade": "-"}, {"grade": "A"}))

        table = normalize_table(bronze, null_tokens=("",))

        assert table["grade"].to_list() == ["-", "A"]

    def test_native_numeric_next_to_a_null_token_is_not_rejected(self) -> None:
        # JSON/public API records give 84.5 as native number, missing as "". Declaration
        # Applied *after* table creation, heterogeneous type guard (#187) catches first, correct
        # null_tokens declaration doesn't work without related read_as declaration.
        bronze = _bronze(({"area": 84.5}, {"area": ""}))

        table = normalize_table(bronze, null_tokens=("",))

        assert table["area"].to_list() == [84.5, None]

    def test_native_numeric_next_to_a_null_token_casts_cleanly(self) -> None:
        bronze = _bronze(({"area": 84.5}, {"area": ""}))

        table = normalize_table(bronze, casts={"area": "float"}, null_tokens=("",))

        assert table["area"].to_list() == [84.5, None]


class TestColumnNullTokens:
    """Same-meaning missing represented differently per column in source (#623).

    Global declaration alone cannot express it without changing other column meanings.
    """

    def test_column_tokens_add_to_the_global_ones(self) -> None:
        bronze = _bronze(({"gender": "", "station": ""}, {"gender": "TOKEN", "station": "x"}))

        table = normalize_table(
            bronze, null_tokens=("TOKEN",), column_null_tokens={"gender": CNT(tokens=("",))}
        )

        # gender both missing, station empty string stays as value.
        assert table["gender"].to_list() == [None, None]
        assert table["station"].to_list() == ["", "x"]

    def test_column_tokens_do_not_replace_the_global_ones(self) -> None:
        """Allowing overwrite silently loses global token while adding one."""
        bronze = _bronze(({"gender": "TOKEN"}, {"gender": ""}, {"gender": "F"}))

        table = normalize_table(
            bronze, null_tokens=("TOKEN",), column_null_tokens={"gender": CNT(tokens=("",))}
        )

        assert table["gender"].to_list() == [None, None, "F"]

    def test_works_without_any_global_tokens(self) -> None:
        bronze = _bronze(({"gender": ""}, {"station": ""}))

        table = normalize_table(bronze, column_null_tokens={"gender": CNT(tokens=("",))})

        assert table["gender"].to_list() == [None, None]
        assert table["station"].to_list() == [None, ""]

    def test_absent_column_fails(self) -> None:
        """Typo becomes silent no-op, missing stays as value, metric uncounted."""
        bronze = _bronze(({"gender": ""},))

        with pytest.raises(TabularError, match="absent from the source"):
            normalize_table(bronze, column_null_tokens={"gendr": CNT(tokens=("",))})

    def test_absent_column_is_ignored_when_declared_optional(self) -> None:
        """Documenting missing notation does not claim column must exist.

        Source evolves and columns can disappear; their absence does not break contract.
        """
        bronze = _bronze(({"other": "x"},))

        table = normalize_table(
            bronze, column_null_tokens={"gender": CNT(tokens=("",), on_absent="ignore")}
        )

        assert table.columns == ["other"]

    def test_optional_column_still_gets_its_tokens_when_present(self) -> None:
        bronze = _bronze(({"gender": ""}, {"gender": "F"}))

        table = normalize_table(
            bronze, column_null_tokens={"gender": CNT(tokens=("",), on_absent="ignore")}
        )

        assert table["gender"].to_list() == [None, "F"]

    def test_shorthand_default_is_error(self) -> None:
        bronze = _bronze(({"other": "x"},))

        with pytest.raises(TabularError, match="on_absent: ignore"):
            normalize_table(bronze, column_null_tokens={"gender": CNT(tokens=("",))})

    def test_non_string_column_fails(self) -> None:
        bronze = _bronze(({"use_count": 1},))

        with pytest.raises(TabularError, match="non-string"):
            normalize_table(bronze, column_null_tokens={"use_count": CNT(tokens=("0",))})

    def test_all_null_column_is_allowed(self) -> None:
        """In multi-generation snapshot, 'column present but all values missing' is normal."""
        bronze = _bronze(({"gender": None}, {"gender": None}))

        table = normalize_table(bronze, column_null_tokens={"gender": CNT(tokens=("",))})

        assert table["gender"].to_list() == [None, None]

    def test_runs_before_coalesce(self) -> None:
        """If missing stays as value, coalesce sees it as conflict."""
        bronze = _bronze(({"a": "", "b": "3"},))

        table = normalize_table(
            bronze, column_null_tokens={"a": CNT(tokens=("",))}, coalesce={"merged": ("a", "b")}
        )

        assert table["merged"].to_list() == ["3"]

    def test_keys_are_pre_rename_names(self) -> None:
        bronze = _bronze(({"성별": ""},))

        table = normalize_table(
            bronze, column_null_tokens={"성별": CNT(tokens=("",))}, rename={"성별": "gender"}
        )

        assert table["gender"].to_list() == [None]


class TestCoalesce:
    """Collect per-generation alias columns into one (#620).

    Fail condition is the point - silent first-wins only proof generation boundary wrong
    signal swallowed.
    """

    def test_merges_generation_aliases_into_one_column(self) -> None:
        bronze = _bronze(
            (
                {"이동거리": "1210.0", "이동거리(M)": None},
                {"이동거리": None, "이동거리(M)": "980.0"},
            )
        )

        table = normalize_table(bronze, coalesce={"move_meter": ("이동거리", "이동거리(M)")})

        assert table["move_meter"].to_list() == ["1210.0", "980.0"]

    def test_converged_candidates_are_absorbed_into_the_canonical_column(self) -> None:
        """Candidate columns absorbed into canonical column and vanish.

        Limited to declared alias group, not arbitrary column deletion - undeclared
        columns stay as-is.
        """
        bronze = _bronze(({"이동거리": "1210.0", "이동거리(M)": None, "keep": "x"},))

        table = normalize_table(bronze, coalesce={"move_meter": ("이동거리", "이동거리(M)")})

        assert "이동거리" not in table.columns
        assert "이동거리(M)" not in table.columns
        assert table.columns == ["keep", "move_meter"]

    def test_target_colliding_with_an_unrelated_column_fails(self) -> None:
        """Silently overwrite column outside alias group = deletion, not convergence."""
        bronze = _bronze(({"a": "1", "b": None, "merged": "keep me"},))

        with pytest.raises(TabularError, match="overwrite an existing column"):
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

    def test_target_may_reuse_a_candidate_name(self) -> None:
        bronze = _bronze(({"a": "1", "b": None}, {"a": None, "b": "2"}))

        table = normalize_table(bronze, coalesce={"a": ("a", "b")})

        assert table["a"].to_list() == ["1", "2"]
        assert "b" not in table.columns

    def test_all_null_row_stays_null(self) -> None:
        bronze = _bronze(({"a": None, "b": None}, {"a": "x", "b": None}))

        table = normalize_table(bronze, coalesce={"merged": ("a", "b")})

        assert table["merged"].to_list() == [None, "x"]

    def test_agreeing_candidates_are_allowed(self) -> None:
        bronze = _bronze(({"a": "3", "b": "3"},))

        table = normalize_table(bronze, coalesce={"merged": ("a", "b")})

        assert table["merged"].to_list() == ["3"]

    def test_disagreeing_candidates_fail(self) -> None:
        bronze = _bronze(({"a": "3", "b": "4"},))

        with pytest.raises(TabularError, match="disagree"):
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

    def test_no_candidate_present_fails(self) -> None:
        """Silently create all-null column, no one knows source disappeared entirely."""
        bronze = _bronze(({"other": "1"},))

        with pytest.raises(TabularError, match="none of its candidates"):
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

    def test_partial_presence_uses_what_exists(self) -> None:
        bronze = _bronze(({"a": "1"}, {"a": "2"}))

        table = normalize_table(bronze, coalesce={"merged": ("a", "b", "c")})

        assert table["merged"].to_list() == ["1", "2"]

    def test_differing_dtypes_fail(self) -> None:
        bronze = _bronze(({"a": "1", "b": None}, {"a": None, "b": 2}))

        with pytest.raises(TabularError, match="differing dtypes"):
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

    def test_runs_after_null_tokens(self) -> None:
        r"""If ``\N`` still string, coalesce sees it as value and conflicts."""
        bronze = _bronze(({"a": r"\N", "b": "3"},))

        table = normalize_table(bronze, null_tokens=(r"\N",), coalesce={"merged": ("a", "b")})

        assert table["merged"].to_list() == ["3"]

    def test_target_that_is_another_targets_candidate_is_rejected(self) -> None:
        # Each rule deletes converged candidates. So {"a": ["x"], "b": ["a"]} is a,b
        # succeeds in a,b order but fails in b,a order - yet
        # canonical_spec_mapping() sorts keys for snapshot, so same digest
        # Declaration might behave differently from original build.
        bronze = _bronze(({"x": "1"},))

        with pytest.raises(TabularError, match="overlapping coalesce groups"):
            normalize_table(bronze, coalesce={"a": ("x",), "b": ("a",)})

    def test_declaration_order_does_not_change_the_rejection(self) -> None:
        bronze = _bronze(({"x": "1"},))

        with pytest.raises(TabularError, match="overlapping coalesce groups"):
            normalize_table(bronze, coalesce={"b": ("a",), "a": ("x",)})

    def test_candidate_shared_by_two_targets_is_rejected(self) -> None:
        bronze = _bronze(({"x": "1", "y": "2"},))

        with pytest.raises(TabularError, match="overlapping coalesce groups"):
            normalize_table(bronze, coalesce={"a": ("x", "y"), "b": ("y",)})

    def test_independent_groups_are_applied_in_a_stable_order(self) -> None:
        # Non-overlapping groups must yield same result regardless of declaration order.
        records = ({"old_id": "1", "legacy_name": "seoul"},)

        first = normalize_table(
            _bronze(records), coalesce={"id": ("old_id",), "name": ("legacy_name",)}
        )
        second = normalize_table(
            _bronze(records), coalesce={"name": ("legacy_name",), "id": ("old_id",)}
        )

        assert first.to_dicts() == second.to_dicts()


class TestZfill:
    def test_pads_identifier_to_declared_width(self) -> None:
        bronze = _bronze(({"station": "3"}, {"station": "00003"}, {"station": "102"}))

        table = normalize_table(bronze, zfill={"station": 5})

        assert table["station"].to_list() == ["00003", "00003", "00102"]

    def test_null_stays_null(self) -> None:
        """Filling with ``00000`` makes missing valid identifier."""
        bronze = _bronze(({"station": None}, {"station": "3"}))

        table = normalize_table(bronze, zfill={"station": 5})

        assert table["station"].to_list() == [None, "00003"]

    def test_value_longer_than_width_fails(self) -> None:
        """Contract width=5 but 6 chars come = drift signal."""
        bronze = _bronze(({"station": "123456"},))

        with pytest.raises(TabularError, match="longer than the declared width"):
            normalize_table(bronze, zfill={"station": 5})

    def test_non_string_column_fails(self) -> None:
        bronze = _bronze(({"station": 3},))

        with pytest.raises(TabularError, match="declare read_as"):
            normalize_table(bronze, zfill={"station": 5})

    def test_all_null_column_is_promoted_instead_of_rejected(self) -> None:
        # If all values null, Polars infers pl.Null. Can't resolve with read_as
        # (_apply_read_as doesn't touch null), zfill keeps null as null
        # promised, no reason to reject.
        bronze = _bronze(({"station": None}, {"station": None}))

        table = normalize_table(bronze, zfill={"station": 5})

        assert table["station"].to_list() == [None, None]
        assert table.schema["station"] == pl.Utf8

    def test_all_null_coalesced_alias_can_be_zfilled(self) -> None:
        bronze = _bronze(({"legacy_station": None}, {"legacy_station": None}))

        table = normalize_table(
            bronze, coalesce={"station": ("legacy_station",)}, zfill={"station": 5}
        )

        assert table["station"].to_list() == [None, None]

    def test_absent_column_fails(self) -> None:
        bronze = _bronze(({"other": "1"},))

        with pytest.raises(TabularError, match="absent from the table"):
            normalize_table(bronze, zfill={"station": 5})

    def test_applies_to_renamed_name(self) -> None:
        """Declaration points to canonical name - applied after rename."""
        bronze = _bronze(({"대여소번호": "3"},))

        table = normalize_table(
            bronze, rename={"대여소번호": "station_no"}, zfill={"station_no": 5}
        )

        assert table["station_no"].to_list() == ["00003"]


class TestYearMonthCast:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("2020-01", "2020-01"), ("202207", "2022-07"), ("2024-12", "2024-12")],
    )
    def test_accepts_both_notations(self, raw: str, expected: str) -> None:
        bronze = _bronze(({"ym": raw},))

        table = normalize_table(bronze, casts={"ym": "year_month"})

        assert table["ym"].to_list() == [expected]
        assert table.schema["ym"] == pl.Utf8

    def test_mixed_notations_in_one_column(self) -> None:
        """Format changes within G2 - single format declaration makes half null."""
        bronze = _bronze(({"ym": "2022-06"}, {"ym": "202207"}))

        table = normalize_table(bronze, casts={"ym": "year_month"})

        assert table["ym"].to_list() == ["2022-06", "2022-07"]

    @pytest.mark.parametrize("raw", ["20230", "2023-1", "202313", "202200", "2023-13"])
    def test_rejects_malformed_values(self, raw: str) -> None:
        """Loose parser silently makes wrong year/month."""
        bronze = _bronze(({"ym": raw},))

        with pytest.raises(TabularError, match="year_month"):
            normalize_table(bronze, casts={"ym": "year_month"})

    def test_null_stays_null(self) -> None:
        bronze = _bronze(({"ym": None}, {"ym": "202301"}))

        table = normalize_table(bronze, casts={"ym": "year_month"})

        assert table["ym"].to_list() == [None, "2023-01"]


class TestErrorMessagesCarryNoSourceValues:
    """Don't embed source value in normalization failure message (#441).

    These messages go to manifest and ``/builds`` responses; PII scan happens before
    runs after. Values leak before scan sees. Column and line count sufficient to find fix.
    """

    SECRET = "010-1234-5678"

    def test_zfill_overflow_reports_a_length_not_a_value(self) -> None:
        bronze = _bronze(({"station": self.SECRET},))

        with pytest.raises(TabularError) as exc:
            normalize_table(bronze, zfill={"station": 5})

        assert self.SECRET not in str(exc.value)
        assert "longer than the declared width" in str(exc.value)

    def test_year_month_rejection_reports_a_count_not_a_value(self) -> None:
        bronze = _bronze(({"ym": self.SECRET},))

        with pytest.raises(TabularError) as exc:
            normalize_table(bronze, casts={"ym": "year_month"})

        assert self.SECRET not in str(exc.value)

    def test_coalesce_disagreement_reports_columns_not_rows(self) -> None:
        bronze = _bronze(({"a": self.SECRET, "b": "other"},))

        with pytest.raises(TabularError) as exc:
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

        assert self.SECRET not in str(exc.value)
        assert "disagree" in str(exc.value)

    def test_date_parts_loss_reports_columns_not_rows(self) -> None:
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"y": self.SECRET, "m": "13", "d": "40"},))

        with pytest.raises(TabularError) as exc:
            normalize_table(
                bronze,
                derived=(DerivedColumn(name="d8", kind="date_parts", columns=("y", "m", "d")),),
            )

        assert self.SECRET not in str(exc.value)
        assert "data loss" in str(exc.value)
