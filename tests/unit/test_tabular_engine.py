"""Polars tabular engine (#49): schema inference, statistics, preview, and reverse conversion."""

from __future__ import annotations

import kpubdata_builder.tabular as tabular
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular import (
    ColumnInfo,
    PreviewSlice,
    SchemaInfo,
    TableStatistics,
)
from tests.support.polars_convert import dataframe_to_records, records_to_dataframe
from tests.support.polars_engine import compute_statistics, generate_preview, infer_schema


class TestInferSchema:
    def test_returns_schema_info_with_column_details(self) -> None:
        df = records_to_dataframe(
            (
                {"id": "1", "score": 10},
                {"id": "2", "score": 20},
                {"id": "2", "score": None},
            )
        )

        schema = infer_schema(df)

        assert isinstance(schema, SchemaInfo)
        assert [c.name for c in schema.columns] == ["id", "score"]

        id_col, score_col = schema.columns
        assert isinstance(id_col, ColumnInfo)
        assert id_col.dtype == "String"
        assert id_col.nullable is False
        assert id_col.unique_count == 2

        assert score_col.dtype == "Int64"
        assert score_col.nullable is True

    def test_empty_dataframe_has_no_columns(self) -> None:
        schema = infer_schema(records_to_dataframe(()))

        assert isinstance(schema, SchemaInfo)
        assert schema.columns == ()


class TestComputeStatistics:
    def test_counts_rows_nulls_and_duplicate_rate(self) -> None:
        df = records_to_dataframe(
            (
                {"id": "1", "v": "x"},
                {"id": "1", "v": "x"},  # Full duplicate of row 0
                {"id": "2", "v": None},
            )
        )

        stats = compute_statistics(df)

        assert isinstance(stats, TableStatistics)
        assert stats.row_count == 3
        assert stats.null_counts == {"id": 0, "v": 1}
        # 3 rows, 2 unique → duplicate rate = 1 - 2/3
        assert abs(stats.duplicate_rate - (1 / 3)) < 1e-9

    def test_empty_dataframe_yields_zeroed_statistics(self) -> None:
        stats = compute_statistics(records_to_dataframe(()))

        assert stats.row_count == 0
        assert stats.null_counts == {}
        assert stats.duplicate_rate == 0.0


class TestGeneratePreview:
    def test_respects_limit_and_reports_total(self) -> None:
        records: list[dict[str, JsonValue]] = [{"n": i} for i in range(10)]
        df = records_to_dataframe(records)

        preview = generate_preview(df, limit=3)

        assert isinstance(preview, PreviewSlice)
        assert preview.total_rows == 10
        assert len(preview.rows) == 3
        assert preview.rows[0] == {"n": 0}

    def test_uses_default_limit_when_unspecified(self) -> None:
        df = records_to_dataframe([{"n": i} for i in range(20)])

        preview = generate_preview(df)

        assert preview.total_rows == 20
        assert len(preview.rows) == 5

    def test_preview_of_empty_dataframe(self) -> None:
        preview = generate_preview(records_to_dataframe(()))

        assert preview.rows == ()
        assert preview.total_rows == 0

    def test_zero_limit_returns_no_rows_but_keeps_total(self) -> None:
        preview = generate_preview(records_to_dataframe(({"n": 1}, {"n": 2})), limit=0)

        assert preview.rows == ()
        assert preview.total_rows == 2


class TestDataframeToRecords:
    def test_round_trips_with_records_to_dataframe(self) -> None:
        records: list[dict[str, JsonValue]] = [
            {"id": "1", "amount": 1000},
            {"id": "2", "amount": 2500},
        ]

        result = dataframe_to_records(records_to_dataframe(records))

        assert result == records

    def test_empty_dataframe_returns_empty_list(self) -> None:
        assert dataframe_to_records(records_to_dataframe(())) == []

    def test_round_trips_nested_json_values(self) -> None:
        records: list[dict[str, JsonValue]] = [
            {"id": "1", "meta": {"city": "서울", "codes": ["1", "2", None]}},
        ]

        assert dataframe_to_records(records_to_dataframe(records)) == records


def test_root_package_does_not_reexport_dataframe_converters() -> None:
    assert "records_to_dataframe" not in tabular.__all__
    assert "dataframe_to_records" not in tabular.__all__
    assert not hasattr(tabular, "records_to_dataframe")
    assert not hasattr(tabular, "dataframe_to_records")
