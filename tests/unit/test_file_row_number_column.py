"""A table with a column named ``file_row_number`` can be read (#1149).

The sandbox keeps a table's row order by asking DuckDB for the row's position in the
file (``read_parquet(…, file_row_number = true)``). DuckDB refuses that on a file that
already has a column of that name, in any letter case, so every read of such a table —
SQL, rows, aggregate, profile, export — failed with ``query_execution_failed``, though
the table had been built without complaint. The position is counted instead.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest

from kpubdata_builder.query.sandbox import DATASET, ORDERED_DATASET, ROW_ORDER, open_sandbox
from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular.duckdb_runtime import BuildProfile
from kpubdata_builder.warehouse import materialize
from tests.support.polars_bridge import handle_from_frame

_NAMES = ["file_row_number", "FILE_ROW_NUMBER", "File_Row_Number"]
_NAME = "air.station"
_DEV = Principal("dev")


def _no_client(**_: object) -> None:
    return None


@pytest.mark.parametrize("name", _NAMES)
def test_the_sandbox_opens_it_and_reads_the_column(tmp_path: Path, name: str) -> None:
    """The reproduction of the issue: a table as Builder writes one."""
    handle = handle_from_frame(pl.DataFrame({name: [5, 6], "v": [1, 2]}), workdir=tmp_path / "work")
    path = tmp_path / "table.parquet"
    handle.write_parquet(path)
    handle.close()

    with open_sandbox(str(path)) as sandbox:
        assert sandbox.columns == (name, "v")
        assert sandbox.dataset_refused is None
        # The column is the table's own values, not DuckDB's row numbers.
        assert sandbox.connection.execute(
            f'SELECT "{name}", v FROM {DATASET} ORDER BY v'
        ).fetchall() == [(5, 1), (6, 2)]
        ordered = (
            f'SELECT {sandbox.alias(name)}, "{ROW_ORDER}" FROM {ORDERED_DATASET} '
            f'ORDER BY "{ROW_ORDER}"'
        )
        assert sandbox.connection.execute(ordered).fetchall() == [(5, 0), (6, 1)]


@pytest.mark.parametrize("threads", [1, 4])
def test_the_counted_position_is_the_rows_place_in_the_file(tmp_path: Path, threads: int) -> None:
    """Over many row groups and with several threads, where an unordered count would show.

    The count is in file order because the scan is: DuckDB's ``preserve_insertion_order``,
    which is on by default and which the sandbox does not set. A change that turns it
    off — in the sandbox's connection settings, or a DuckDB release that changes the
    default or how a window with no ordering runs — is meant to fail here.
    """
    rows = 120_000
    shuffled = list(range(rows))
    random.Random(1149).shuffle(shuffled)
    path = tmp_path / "t.parquet"
    # No column is in file order but ``place``: the file's order is not a sort of anything.
    pl.DataFrame({"file_row_number": shuffled, "place": list(range(rows))}).write_parquet(
        path, row_group_size=2_000
    )
    profile = BuildProfile(threads=threads, memory_limit="256MB", max_temp_directory_size="1GB")

    with open_sandbox(str(path), profile=profile) as sandbox:
        place = sandbox.alias("place")
        misplaced = sandbox.connection.execute(
            f'SELECT count(*) FROM {ORDERED_DATASET} WHERE {place} <> "{ROW_ORDER}"'
        ).fetchone()
        page = sandbox.connection.execute(
            f'SELECT {place} FROM {ORDERED_DATASET} WHERE "{ROW_ORDER}" >= 100000 '
            f'ORDER BY "{ROW_ORDER}" LIMIT 3'
        ).fetchall()
        extent = sandbox.connection.execute(
            f'SELECT count(*), min("{ROW_ORDER}"), max("{ROW_ORDER}") FROM {ORDERED_DATASET}'
        ).fetchone()

        preserved = sandbox.connection.execute(
            "SELECT current_setting('preserve_insertion_order')"
        ).fetchone()

    # The ground the count stands on, stated: see the docstring.
    assert preserved == (True,)
    assert misplaced == (0,)
    assert page == [(100000,), (100001,), (100002,)]
    assert extent == (rows, 0, rows - 1)


def test_a_table_without_such_a_column_still_takes_duckdbs_own_position(tmp_path: Path) -> None:
    """The counted position is for the one case that needs it."""
    path = tmp_path / "t.parquet"
    pl.DataFrame({"row_number": [7, 8, 9]}).write_parquet(path)

    with open_sandbox(str(path)) as sandbox:
        view = sandbox.connection.execute(
            "SELECT sql FROM duckdb_views() WHERE view_name = ?", [ORDERED_DATASET]
        ).fetchone()
        ordered = sandbox.connection.execute(
            f'SELECT "{ROW_ORDER}" FROM {ORDERED_DATASET} ORDER BY "{ROW_ORDER}"'
        ).fetchall()

    assert view is not None
    assert "(file_row_number = " in view[0]
    assert "over (" not in view[0].lower()
    assert ordered == [(0,), (1,), (2,)]


def _ok(response: ServiceResponse) -> dict[str, Any]:
    assert response.status_code in (200, 201), response.body
    return cast(dict[str, Any], response.body)


@pytest.mark.parametrize("name", ["file_row_number", "FILE_ROW_NUMBER"])
def test_every_read_of_such_a_table_answers(tmp_path: Path, name: str) -> None:
    """Through the service and its real query engine: SQL, rows, aggregate, profile, export."""
    service = BuilderService(
        output_root=tmp_path, client_factory=_no_client, warehouse_root=tmp_path / "wh"
    )
    catalog = service._table_catalog()
    assert catalog is not None
    gold = tmp_path / "gold"
    gold.mkdir()
    # Not in order of either column, so only the file's own order gives 30, 10, 20.
    pl.DataFrame({name: [30, 10, 20], "station": ["c", "a", "b"]}).write_parquet(
        gold / "table.parquet"
    )
    materialize(
        catalog, workspace_id=PERSONAL_WORKSPACE, logical_name=_NAME, source_dir=gold, run_id="r"
    )
    sql: dict[str, JsonValue] = {
        "table": _NAME,
        "sql": f'SELECT "{name}", station FROM dataset ORDER BY station',
    }

    queried = _ok(service.query_warehouse(sql, principal=_DEV))
    paged = _ok(service.read_warehouse_rows({"table": _NAME}, principal=_DEV))
    counted = _ok(
        service.aggregate_warehouse(
            {
                "table": _NAME,
                "measures": [
                    {"fn": "count_rows", "as": "n"},
                    {"fn": "max", "column": name, "as": "largest"},
                ],
            },
            principal=_DEV,
        )
    )
    profiled = _ok(service.get_warehouse_profile(_NAME, "current", principal=_DEV))
    exported = service.create_warehouse_export(sql, principal=_DEV)

    assert queried["result"]["rows"] == [
        {name: 10, "station": "a"},
        {name: 20, "station": "b"},
        {name: 30, "station": "c"},
    ]
    # Rows come in the file's order, which is the order no column gives.
    assert [row[name] for row in paged["rows"]] == [30, 10, 20]
    assert paged["columns"] == [name, "station"]
    assert counted["rows"] == [{"n": 3, "largest": 30}]
    assert name in [column["name"] for column in profiled["profile"]["columns"]]
    assert exported.status_code in (200, 201), exported.body
    assert exported.body.get("code") != "query_execution_failed"
