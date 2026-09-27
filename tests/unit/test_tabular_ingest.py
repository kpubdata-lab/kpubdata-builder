"""ingestion.tabular_ingest: file/url source raw bytes → record parsing validation (#498)."""

from __future__ import annotations

import io

import polars as pl
import pytest

from kpubdata_builder.ingestion import IngestionError, parse_tabular_bytes


def test_parse_csv_bytes_returns_typed_records() -> None:
    raw = b"id,amount\n1,1000\n2,2500\n"

    records = parse_tabular_bytes(raw, format="csv")

    assert records == ({"id": 1, "amount": 1000}, {"id": 2, "amount": 2500})


def test_parse_csv_bytes_respects_encoding() -> None:
    raw = "id,name\n1,서울\n".encode("euc-kr")

    records = parse_tabular_bytes(raw, format="csv", encoding="euc-kr")

    assert records == ({"id": 1, "name": "서울"},)


def test_parse_json_array_of_objects() -> None:
    raw = b'[{"id": 1, "v": 1.5}, {"id": 2, "v": 2.5}]'

    records = parse_tabular_bytes(raw, format="json")

    assert records == ({"id": 1, "v": 1.5}, {"id": 2, "v": 2.5})


def test_parse_json_rejects_non_array_top_level() -> None:
    raw = b'{"data": [{"id": 1}]}'

    with pytest.raises(IngestionError, match="array of objects"):
        parse_tabular_bytes(raw, format="json")


def test_parse_json_rejects_array_of_scalars() -> None:
    raw = b"[1, 2, 3]"

    with pytest.raises(IngestionError, match="array of objects"):
        parse_tabular_bytes(raw, format="json")


def test_parse_json_rejects_malformed_json() -> None:
    with pytest.raises(IngestionError, match="failed to parse json"):
        parse_tabular_bytes(b"not json", format="json")


def test_parse_jsonl_skips_blank_lines() -> None:
    raw = b'{"id": 1}\n\n{"id": 2}\n'

    records = parse_tabular_bytes(raw, format="jsonl")

    assert records == ({"id": 1}, {"id": 2})


def test_parse_jsonl_rejects_non_object_line() -> None:
    raw = b'{"id": 1}\n[1, 2]\n'

    with pytest.raises(IngestionError, match="must be a JSON object"):
        parse_tabular_bytes(raw, format="jsonl")


def test_parse_jsonl_rejects_malformed_line() -> None:
    with pytest.raises(IngestionError, match="jsonl line 1"):
        parse_tabular_bytes(b"not json\n", format="jsonl")


def test_parse_jsonl_rejects_all_blank_content() -> None:
    with pytest.raises(IngestionError, match="no non-empty lines"):
        parse_tabular_bytes(b"\n\n\n", format="jsonl")


def test_parse_parquet_round_trip() -> None:
    frame = pl.DataFrame({"id": [1, 2], "amount": [1000, 2500]})
    buffer = io.BytesIO()
    frame.write_parquet(buffer)

    records = parse_tabular_bytes(buffer.getvalue(), format="parquet")

    assert records == ({"id": 1, "amount": 1000}, {"id": 2, "amount": 2500})


def test_parse_parquet_rejects_corrupt_bytes() -> None:
    with pytest.raises(IngestionError, match="failed to parse parquet"):
        parse_tabular_bytes(b"not a parquet file", format="parquet")


def test_parse_rejects_empty_content() -> None:
    with pytest.raises(IngestionError, match="empty"):
        parse_tabular_bytes(b"", format="csv")


def test_parse_rejects_unsupported_format() -> None:
    with pytest.raises(IngestionError, match="unsupported format"):
        parse_tabular_bytes(b"data", format="xlsx")


def test_parse_rejects_undecodable_bytes() -> None:
    raw = "안녕".encode("euc-kr")

    with pytest.raises(IngestionError, match="failed to decode"):
        parse_tabular_bytes(raw, format="csv", encoding="ascii")


def test_parse_rejects_unknown_encoding_name() -> None:
    with pytest.raises(IngestionError, match="failed to decode"):
        parse_tabular_bytes(b"id\n1\n", format="csv", encoding="not-a-real-encoding")


def test_parse_csv_rejects_malformed_csv() -> None:
    # Header has 2 columns but data row has 3 fields — polars rejects with parsing error.
    raw = b"a,b\n1,2,3\n"

    with pytest.raises(IngestionError, match="failed to parse csv"):
        parse_tabular_bytes(raw, format="csv")


def test_parse_csv_reads_declared_column_as_text() -> None:
    # CSV parsing is type inference. If declaration arrives late in Silver,
    # `00123` is already integer 123, and leading zeros cannot be recovered (#613).
    raw = b"code,amount\n00123,1000\n00456,2500\n"

    records = parse_tabular_bytes(raw, format="csv", read_as={"code": "str"})

    assert records == (
        {"code": "00123", "amount": 1000},
        {"code": "00456", "amount": 2500},
    )


def test_parse_csv_leaves_undeclared_columns_to_inference() -> None:
    raw = b"code,amount\n00123,1000\n"

    records = parse_tabular_bytes(raw, format="csv", read_as={"code": "str"})

    assert records[0]["amount"] == 1000


def test_parse_csv_without_declaration_is_unchanged() -> None:
    raw = b"code,amount\n00123,1000\n"

    assert parse_tabular_bytes(raw, format="csv") == parse_tabular_bytes(
        raw, format="csv", read_as={}
    )
