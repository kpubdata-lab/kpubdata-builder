"""Gold selection, masking, composition and persist on DuckDB (#870)."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder.spec import JoinSpec
from kpubdata_builder.spec.models import GoldFilter, GoldSelection
from kpubdata_builder.stages.gold.compose import build_composed_gold_package
from kpubdata_builder.stages.gold.pii import PII_MASK_TOKEN, mask_columns
from kpubdata_builder.stages.gold.select import GoldSelectionError, apply_gold_selection
from kpubdata_builder.tabular.builder_parquet import builder_dtypes, read_builder_parquet
from kpubdata_builder.tabular.duckdb_load import TableHandle
from kpubdata_builder.tabular.polars_bridge import handle_from_frame, to_polars

_SRC = Path(__file__).parents[2] / "src" / "kpubdata_builder" / "stages" / "gold"

_TYPED = pl.DataFrame(
    {
        "int": pl.Series([1, 2, None], dtype=pl.Int64),
        "float": pl.Series([1.0, 2.5, None], dtype=pl.Float64),
        "dec": pl.Series([Decimal("1.50"), Decimal("2"), None], dtype=pl.Decimal(38, 2)),
        "str": ["1", "b", None],
        "bool": [True, False, None],
        "date": [dt.date(2020, 1, 1), dt.date(2021, 1, 1), None],
        "dur": [dt.timedelta(1), dt.timedelta(2), None],
        "list": [[1], [2], None],
        "null": pl.Series([None, None, None], dtype=pl.Null),
    }
)


def _table(frame: pl.DataFrame, tmp_path: Path) -> TableHandle:
    return handle_from_frame(frame, workdir=tmp_path)


# ---------------------------------------------------------------------- selection


@pytest.mark.parametrize(
    ("column", "op", "value", "kept"),
    [
        ("int", "ge", 2, 1),
        ("int", "gt", 1.5, 1),
        ("float", "eq", 1, 1),
        ("dec", "gt", 1.6, 1),
        ("str", "eq", "b", 1),
        ("str", "gt", "1", 1),
        ("bool", "eq", True, 1),
        ("int", "in", [1, 2], 2),
        ("int", "in", [1, None], 1),
        ("str", "in", "b", 1),
        # A null value, or a column of nulls, keeps nothing: a null never passes.
        ("int", "eq", None, 0),
        ("null", "eq", 1, 0),
        ("null", "in", ["a", 1], 0),
    ],
)
def test_comparable_values(column: str, op: str, value: object, kept: int, tmp_path: Path) -> None:
    result, stats = apply_gold_selection(
        _table(_TYPED, tmp_path), GoldSelection(filters=(GoldFilter(column, op, value),))
    )

    assert (result.height, stats.output_rows) == (kept, kept)


@pytest.mark.parametrize(
    ("column", "op", "value"),
    [
        ("int", "eq", "1"),
        ("int", "eq", True),
        ("float", "gt", False),
        ("str", "gt", 0),
        ("bool", "eq", 1),
        ("bool", "gt", "1"),
        ("date", "gt", "2020-06-01"),
        ("date", "gt", 1),
        ("dur", "gt", 1),
        ("list", "eq", 1),
        ("int", "in", [1, "b"]),
        ("str", "in", [1, 2]),
    ],
)
def test_incomparable_values_are_refused_not_cast(
    column: str, op: str, value: object, tmp_path: Path
) -> None:
    """Negative (#870): DuckDB would cast many of these implicitly, and Polars compared
    some of them silently (a date with a number, a boolean with text). Each is refused
    with the column's dtype named."""
    with pytest.raises(GoldSelectionError, match="cannot be applied to a"):
        apply_gold_selection(
            _table(_TYPED, tmp_path), GoldSelection(filters=(GoldFilter(column, op, value),))
        )


def test_a_value_is_bound_never_spliced_into_sql(tmp_path: Path) -> None:
    frame = pl.DataFrame({"s": ["a'; DROP TABLE x; --", "b"]})

    result, _ = apply_gold_selection(
        _table(frame, tmp_path),
        GoldSelection(filters=(GoldFilter("s", "eq", "a'; DROP TABLE x; --"),)),
    )

    assert to_polars(result)["s"].to_list() == ["a'; DROP TABLE x; --"]


def test_the_selection_keeps_source_order_and_renumbers_rows(tmp_path: Path) -> None:
    frame = pl.DataFrame({"id": list("abcdef"), "v": [5, 1, 4, 2, 6, 3]})

    result, _ = apply_gold_selection(
        _table(frame, tmp_path),
        GoldSelection(select=("id",), filters=(GoldFilter("v", "gt", 2),)),
    )

    assert to_polars(result)["id"].to_list() == ["a", "c", "e", "f"]
    # The row ordinal is renumbered from 0, so positional reads still work (ADR 0021 D8).
    assert result.rows_at([3, 0]) == ({"id": "f"}, {"id": "a"})


def test_a_selection_without_columns_keeps_every_column(tmp_path: Path) -> None:
    result, _ = apply_gold_selection(_table(_TYPED, tmp_path), GoldSelection())

    assert to_polars(result).equals(_TYPED)


# -------------------------------------------------------------------------- masking


def test_masking_is_a_new_table_and_leaves_its_input(tmp_path: Path) -> None:
    table = _table(pl.DataFrame({"tel": ["010", None], "n": [1, 2]}), tmp_path)

    masked = mask_columns(table, ["tel", "n"])

    assert to_polars(masked).to_dicts() == [
        {"tel": PII_MASK_TOKEN, "n": None},
        {"tel": None, "n": None},
    ]
    assert table.rows(limit=1) == ({"tel": "010", "n": 1},)
    assert masked.dtypes == table.dtypes


# ---------------------------------------------------------------------- composition


def test_inner_join_rows_follow_the_left_then_the_right_order(tmp_path: Path) -> None:
    """ADR 0021 D8: the join's order is stated, not the engine's. Polars' inner join
    put these rows in the right side's order, because the left side is the smaller."""
    left = _table(pl.DataFrame({"k": ["k3", "k1"], "x": ["l0", "l1"]}), tmp_path)
    right = _table(
        pl.DataFrame({"k": ["k1", "k3", "k1", "k3", "k9"], "y": ["r0", "r1", "r2", "r3", "r4"]}),
        tmp_path,
    )
    join = JoinSpec(left="a", right="b", left_key="k", right_key="k", type="inner")

    package, stats = build_composed_gold_package(
        left_silver=None,  # type: ignore[arg-type]
        right_silver=None,  # type: ignore[arg-type]
        left_table=left,
        right_table=right,
        join=join,
        dataset_name="j",
    )

    assert to_polars(package.table).rows() == [
        ("k3", "l0", "r1"),
        ("k3", "l0", "r3"),
        ("k1", "l1", "r0"),
        ("k1", "l1", "r2"),
    ]
    assert stats.output_row_count == 4
    assert package.table.rows_at([2]) == ({"k": "k1", "x": "l1", "y": "r0"},)


def test_join_output_names_and_dtypes(tmp_path: Path) -> None:
    left = _table(
        pl.DataFrame({"id": [1, 2], "v": [1.5, 2.5], "d": [dt.date(2020, 1, 1)] * 2}), tmp_path
    )
    right = _table(pl.DataFrame({"rid": [2, 3], "v": ["x", "y"], "w": [True, None]}), tmp_path)
    join = JoinSpec(left="a", right="rents", left_key="id", right_key="rid", type="left")

    package, _ = build_composed_gold_package(
        left_silver=None,  # type: ignore[arg-type]
        right_silver=None,  # type: ignore[arg-type]
        left_table=left,
        right_table=right,
        join=join,
        dataset_name="j",
    )

    frame = to_polars(package.table)
    # The right key is not in the output; a right column the left has is suffixed.
    assert frame.columns == ["id", "v", "d", "v_rents", "w"]
    assert frame.dtypes == [pl.Int64, pl.Float64, pl.Date, pl.String, pl.Boolean]
    assert frame.rows() == [
        (1, 1.5, dt.date(2020, 1, 1), None, None),
        (2, 2.5, dt.date(2020, 1, 1), "x", True),
    ]


def test_nan_and_null_keys_never_match_and_are_counted(tmp_path: Path) -> None:
    nan = float("nan")
    left = _table(pl.DataFrame({"k": [nan, nan, None, 1.0]}), tmp_path)
    right = _table(pl.DataFrame({"k": [nan, None, 1.0], "y": ["a", "b", "c"]}), tmp_path)
    join = JoinSpec(left="a", right="b", left_key="k", right_key="k", type="left")

    package, stats = build_composed_gold_package(
        left_silver=None,  # type: ignore[arg-type]
        right_silver=None,  # type: ignore[arg-type]
        left_table=left,
        right_table=right,
        join=join,
        dataset_name="j",
    )

    assert (stats.left_null_key_rows, stats.right_null_key_rows) == (3, 2)
    assert stats.observed_cardinality == "one_to_one"
    # A NaN key comes out as the null it stands for (#793), and matches nothing.
    assert to_polars(package.table).rows() == [(None, None), (None, None), (None, None), (1.0, "c")]


# --------------------------------------------------------------------------- persist


def test_gold_parquet_records_builder_dtypes_for_the_warehouse(tmp_path: Path) -> None:
    """Written by DuckDB COPY with the dtypes in the file, and read back as written —
    a Null column included, which Parquet stores as INTEGER."""
    table = _table(_TYPED.drop("list"), tmp_path)
    path = tmp_path / "table.parquet"

    table.write_parquet(path)

    assert builder_dtypes(path) == dict(zip(table.columns, table.dtypes, strict=True))
    assert read_builder_parquet(path).equals(_TYPED.drop("list"))


# ------------------------------------------------------------------- polars removed


@pytest.mark.parametrize("module", ["select.py", "compose.py", "models.py", "pii.py"])
def test_gold_modules_do_not_import_polars(module: str) -> None:
    source = (_SRC / module).read_text(encoding="utf-8")

    assert not re.search(r"^\s*(import polars|from polars)", source, flags=re.M)
