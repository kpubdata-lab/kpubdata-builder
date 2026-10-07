"""Records ↔ Polars frames, as Builder converted them before #876 — for tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import polars as pl

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular.convert import RecordTypeScan


def records_to_dataframe(
    records: Sequence[dict[str, JsonValue]],
    *,
    read_as: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """Convert raw record mapping to Polars DataFrame.

    Relying only on Polars auto-inference risks silent forced conversion of mixed-type
    columns before validation, changing raw values. Detect first and fail fast with
    clear error:

    - Incompatible types mixed in column (and nested list/struct) (#187, #199).
    - Large integers mixed with float in same column, f64 upcast loses precision (#198).

    Args:
        records: JSON-compatible record sequence.
        read_as: Type declaration for source columns (``{column: "str"}``). Public data
            sometimes gives same column different types per record (e.g., lot number mostly
            string but some records integer). Read declared columns as their type,
            reject undeclared mixed types as-is — preserves #187 contract against silent
            forced conversion.

    Returns:
        pl.DataFrame: DataFrame reflecting column structure of input records.

    Raises:
        TabularError: If heterogeneous types mixed or integer precision loss possible.
    """
    scan = RecordTypeScan(read_as=read_as)
    records = [scan.add(record) for record in records]
    scan.check()

    # infer_schema_length=None: scan all records to infer dtype. With default inference window
    # (first few rows) only, first float appearing outside window silently truncates to int in
    # columns inferred as Int64 (#216).
    return pl.DataFrame(list(records), infer_schema_length=None)


def dataframe_to_records(df: pl.DataFrame) -> list[dict[str, JsonValue]]:
    """Convert Polars DataFrame to list of plain dict records.

    Args:
        df: DataFrame to convert.

    Returns:
        list[dict[str, JsonValue]]: Row-by-row dict representation.
    """
    return cast(list[dict[str, JsonValue]], df.to_dicts())
