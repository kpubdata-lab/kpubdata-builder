"""The DuckDB runtime foundation: version floor, connections, SQL and dtypes (#866)."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import duckdb
import polars as pl
import pytest

from kpubdata_builder.tabular import dtypes, duckdb_runtime, sql
from kpubdata_builder.tabular.duckdb_runtime import (
    MINIMUM_DUCKDB_VERSION,
    ROW_SEQ_COLUMN,
    BuildProfile,
    ReservedColumnError,
    TabularRelation,
    build_connection,
    reserve_row_seq,
    worker_temp_directory,
)
from kpubdata_builder.tabular.sql import Statement, quote_identifier

_ROOT = Path(__file__).parents[2]


# ------------------------------------------------------------------ version floor


def test_pyproject_floor_is_the_verified_minimum() -> None:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^\s*"(duckdb[^"]*)",', text, re.MULTILINE)
    assert match is not None
    requirement = match.group(1)

    floor = re.search(r">=\s*([\d.]+)", requirement)

    assert floor is not None
    assert tuple(int(p) for p in floor.group(1).split(".")) == MINIMUM_DUCKDB_VERSION
    assert "<2" in requirement


def test_installed_duckdb_meets_the_floor_and_has_every_sandbox_setting() -> None:
    duckdb_runtime.check_duckdb()

    assert duckdb_runtime.duckdb_version() >= MINIMUM_DUCKDB_VERSION


def test_an_older_duckdb_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb, "__version__", "1.1.3")

    with pytest.raises(RuntimeError, match="older than 1.2.0"):
        duckdb_runtime.check_duckdb()


# ------------------------------------------------------------------ connections


def test_connection_is_utc_whatever_the_host_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Asia/Seoul")

    with build_connection(tmp_path, "datago.apt_trade") as connection:
        zone = connection.execute("SELECT current_setting('TimeZone')").fetchone()
        rendered = connection.execute(
            "SELECT (TIMESTAMPTZ '2024-01-01 09:00:00+09')::VARCHAR"
        ).fetchone()

    assert zone == ("UTC",)
    assert rendered == ("2024-01-01 00:00:00+00",)


def test_connection_takes_the_profile_and_never_loads_extensions(tmp_path: Path) -> None:
    profile = BuildProfile(memory_limit="256MB", threads=1, max_temp_directory_size="1GB")

    with build_connection(tmp_path, "s", profile=profile) as connection:
        settings = dict(
            connection.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN "
                "('threads', 'temp_directory', 'autoinstall_known_extensions', "
                "'autoload_known_extensions', 'memory_limit')"
            ).fetchall()
        )

    assert settings["threads"] == "1"
    assert settings["temp_directory"] == str(worker_temp_directory(tmp_path, "s", 0))
    assert settings["autoinstall_known_extensions"] == "false"
    assert settings["autoload_known_extensions"] == "false"
    assert settings["memory_limit"].endswith("MiB")


def test_profile_rejects_zero_threads() -> None:
    with pytest.raises(ValueError, match="threads"):
        BuildProfile(threads=0)


def test_connections_do_not_share_tables_or_settings(tmp_path: Path) -> None:
    with (
        build_connection(tmp_path, "a") as first,
        build_connection(tmp_path, "b") as second,
    ):
        first.execute("CREATE TABLE only_here AS SELECT 1 AS v")
        first.execute("SET threads = 1")

        with pytest.raises(duckdb.CatalogException):
            second.execute("SELECT * FROM only_here")
        assert second.execute("SELECT current_setting('threads')").fetchone() == (2,)


def test_workers_in_threads_each_get_their_own_connection(tmp_path: Path) -> None:
    results: dict[int, int] = {}

    def work(worker: int) -> None:
        with build_connection(tmp_path, "s", worker) as connection:
            connection.execute(f"CREATE TABLE t AS SELECT {worker} AS v")
            row = connection.execute("SELECT v FROM t").fetchone()
            assert row is not None
            results[worker] = row[0]

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == {0: 0, 1: 1, 2: 2, 3: 3}


# ------------------------------------------------------------------ temp directories


def test_temp_directory_is_per_worker_and_removed_on_close(tmp_path: Path) -> None:
    with build_connection(tmp_path, "datago.apt_trade", 1):
        own = worker_temp_directory(tmp_path, "datago.apt_trade", 1)
        assert own.is_dir()
        with build_connection(tmp_path, "datago.apt_trade", 2):
            other = worker_temp_directory(tmp_path, "datago.apt_trade", 2)
            assert other.is_dir()
        assert not other.exists()
        assert own.is_dir(), "a worker removes only its own directory"

    assert not (tmp_path / "_duckdb_tmp").exists()


def test_temp_directory_is_removed_when_the_work_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), build_connection(tmp_path, "s"):
        raise RuntimeError("boom")

    assert not (tmp_path / "_duckdb_tmp").exists()


def test_spill_files_go_to_the_workers_directory(tmp_path: Path) -> None:
    profile = BuildProfile(memory_limit="64MB", threads=1)
    with build_connection(tmp_path, "s", profile=profile) as connection:
        connection.execute(
            "CREATE TABLE big AS SELECT i, repeat('x', 200) AS pad FROM range(1500000) t(i)"
        )
        connection.execute("SELECT * FROM big ORDER BY pad DESC, i DESC").fetchmany(1)
        seen = [p.name for p in worker_temp_directory(tmp_path, "s", 0).rglob("*")]
        in_use = connection.execute("SELECT count(*) FROM duckdb_temporary_files()").fetchone()

    # 300 MB of rows under a 64 MB limit must spill, and it spills here.
    assert seen
    assert in_use is not None and in_use[0] > 0
    assert not (tmp_path / "_duckdb_tmp").exists()


@pytest.mark.parametrize("source_key", ["datago.apt_trade", "../../etc", "서울 자전거", "a/b"])
def test_temp_directory_names_are_path_safe(tmp_path: Path, source_key: str) -> None:
    path = worker_temp_directory(tmp_path, source_key, 0)

    assert path.parent == tmp_path / "_duckdb_tmp"
    assert re.fullmatch(r"[A-Za-z0-9._-]+", path.name)
    assert not path.name.startswith(".")


def test_names_that_fold_together_still_get_their_own_directory(tmp_path: Path) -> None:
    """Two Hangul aliases are both ``_`` once made path-safe; their spill
    directories must still differ (#891 review)."""
    names = ["매매", "전월세", "a b", "a/b", "a_b"]

    paths = {worker_temp_directory(tmp_path, name, 0) for name in names}

    assert len(paths) == len(names)
    assert worker_temp_directory(tmp_path, "매매", 0) != worker_temp_directory(tmp_path, "매매", 1)


def test_sources_whose_names_fold_together_run_side_by_side(tmp_path: Path) -> None:
    """Neither connection removes the other's live spill directory."""
    seen: dict[str, bool] = {}

    def work(name: str) -> None:
        with build_connection(tmp_path, name) as connection:
            directory = worker_temp_directory(tmp_path, name, 0)
            connection.execute("CREATE TABLE t AS SELECT range AS v FROM range(1000)")
            barrier.wait()
            seen[name] = directory.is_dir()
            barrier.wait()

    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=work, args=(n,)) for n in ("매매", "전월세")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert seen == {"매매": True, "전월세": True}
    assert not (tmp_path / "_duckdb_tmp").exists()


def test_a_directory_in_use_is_never_taken_over(tmp_path: Path) -> None:
    with (
        build_connection(tmp_path, "s"),
        pytest.raises(FileExistsError),
        build_connection(tmp_path, "s"),
    ):
        pass


def test_clear_temp_root_removes_what_a_crash_left(tmp_path: Path) -> None:
    from kpubdata_builder.tabular.duckdb_runtime import clear_temp_root

    leftover = worker_temp_directory(tmp_path, "s", 0)
    leftover.mkdir(parents=True)

    clear_temp_root(tmp_path)

    with build_connection(tmp_path, "s"):
        pass


# ------------------------------------------------------------------ SQL helpers


@pytest.mark.parametrize(
    ("name", "quoted"),
    [
        ("v", '"v"'),
        ("select", '"select"'),
        ('a"b', '"a""b"'),
        ("이동거리(m)", '"이동거리(m)"'),
        ('"; DROP TABLE t; --', '"""; DROP TABLE t; --"'),
    ],
)
def test_quote_identifier(name: str, quoted: str) -> None:
    assert quote_identifier(name) == quoted


def test_a_quoted_identifier_is_only_ever_a_name(tmp_path: Path) -> None:
    hostile = 'v" FROM t; DROP TABLE t; --'

    with build_connection(tmp_path, "s") as connection:
        connection.execute(f"CREATE TABLE t ({quote_identifier(hostile)} INTEGER)")
        connection.execute("INSERT INTO t VALUES (1)")
        columns = connection.execute(f"SELECT {quote_identifier(hostile)} FROM t").description

        assert columns is not None and columns[0][0] == hostile
        assert connection.execute("SELECT count(*) FROM t").fetchone() == (1,)


@pytest.mark.parametrize("name", ["", "a\x00b"])
def test_quote_identifier_refuses_impossible_names(name: str) -> None:
    with pytest.raises(ValueError):
        quote_identifier(name)


def test_values_are_bound_not_interpolated(tmp_path: Path) -> None:
    hostile = "x'); DROP TABLE t; --"

    with build_connection(tmp_path, "s") as connection:
        connection.execute("CREATE TABLE t (v VARCHAR)")
        sql.execute(connection, Statement("INSERT INTO t VALUES (?)", (hostile,)))
        rows = sql.execute(
            connection,
            Statement(
                f"SELECT v FROM t WHERE v IN ({sql.placeholders([hostile, 'y'])})", (hostile, "y")
            ),
        ).fetchall()

    assert rows == [(hostile,)]


def test_statement_counts_placeholders_outside_quotes() -> None:
    assert Statement("SELECT '?', \"?\" FROM t WHERE a = ?", (1,)).params == (1,)
    with pytest.raises(ValueError, match="1 placeholder"):
        Statement("SELECT ? ", ())
    with pytest.raises(ValueError):
        sql.placeholders([])


def test_identifier_list() -> None:
    assert sql.identifier_list(["a", "b c"]) == '"a", "b c"'


def test_relation_names_are_quoted() -> None:
    assert TabularRelation('silver "raw"').sql == '"silver ""raw"""'


# ------------------------------------------------------------------ row ordinal


@pytest.mark.parametrize("name", [ROW_SEQ_COLUMN, "_KPUBDATA_ROW_SEQ", "_Kpubdata_Row_Seq"])
def test_a_source_column_with_the_reserved_name_is_refused(name: str) -> None:
    with pytest.raises(ReservedColumnError, match="reserves"):
        reserve_row_seq(["a", name])


def test_other_names_are_fine() -> None:
    reserve_row_seq(["a", "_kpubdata_row_seq_2", "kpubdata_row_seq"])


def test_duckdb_would_collide_on_case_so_the_check_is_case_insensitive(tmp_path: Path) -> None:
    """The reason reserve_row_seq ignores case: DuckDB does, even for quoted names."""
    with build_connection(tmp_path, "s") as connection, pytest.raises(duckdb.CatalogException):
        connection.execute(
            f"CREATE TABLE t ({quote_identifier('_KPUBDATA_ROW_SEQ')} INTEGER, "
            f"{quote_identifier(ROW_SEQ_COLUMN)} BIGINT)"
        )


# ------------------------------------------------------------------ canonical dtypes

#: Every canonical dtype, next to the Polars dtype that prints the same today. The
#: vocabulary is Builder's; this pins that the migration does not respell it.
_VOCABULARY: list[tuple[str, pl.DataType]] = [
    ("BOOLEAN", pl.Boolean()),
    ("TINYINT", pl.Int8()),
    ("SMALLINT", pl.Int16()),
    ("INTEGER", pl.Int32()),
    ("BIGINT", pl.Int64()),
    ("HUGEINT", pl.Int128()),
    ("UTINYINT", pl.UInt8()),
    ("USMALLINT", pl.UInt16()),
    ("UINTEGER", pl.UInt32()),
    ("UBIGINT", pl.UInt64()),
    ("FLOAT", pl.Float32()),
    ("DOUBLE", pl.Float64()),
    ("VARCHAR", pl.String()),
    ("BLOB", pl.Binary()),
    ("DATE", pl.Date()),
    ("TIME", pl.Time()),
    ("TIMESTAMP", pl.Datetime("us")),
    ("TIMESTAMP_MS", pl.Datetime("ms")),
    ("TIMESTAMP_NS", pl.Datetime("ns")),
    ("TIMESTAMP WITH TIME ZONE", pl.Datetime("us", "UTC")),
    ('"NULL"', pl.Null()),
    ("DECIMAL(10,2)", pl.Decimal(10, 2)),
    ("DECIMAL(38,0)", pl.Decimal(38, 0)),
    ("BIGINT[]", pl.List(pl.Int64())),
    ("VARCHAR[][]", pl.List(pl.List(pl.String()))),
    ("STRUCT(a BIGINT, b VARCHAR)", pl.Struct({"a": pl.Int64(), "b": pl.String()})),
    ('STRUCT("x y" INTEGER, "q""t" DOUBLE)', pl.Struct({"x y": pl.Int32(), 'q"t': pl.Float64()})),
    (
        "STRUCT(a DECIMAL(10,2), b STRUCT(c DATE)[])",
        pl.Struct({"a": pl.Decimal(10, 2), "b": pl.List(pl.Struct({"c": pl.Date()}))}),
    ),
]


@pytest.mark.parametrize(
    ("duckdb_type", "polars_dtype"), _VOCABULARY, ids=[v[0] for v in _VOCABULARY]
)
def test_canonical_dtype_is_spelt_as_today(duckdb_type: str, polars_dtype: pl.DataType) -> None:
    canonical = dtypes.canonical_dtype(duckdb_type)

    assert canonical == str(polars_dtype)
    assert dtypes.logical_type(canonical) == polars_dtype.base_type().__name__.lower()


def test_the_mapping_covers_what_duckdb_actually_reports(tmp_path: Path) -> None:
    """The table is written against DuckDB's own type names, not guessed ones."""
    with build_connection(tmp_path, "s") as connection:
        relation = connection.sql(
            "SELECT true a, 1::TINYINT b, 1::SMALLINT c, 1::INTEGER d, 1::BIGINT e, "
            "1::HUGEINT f, 1::UTINYINT g, 1::USMALLINT h, 1::UINTEGER i, 1::UBIGINT j, "
            "1::FLOAT k, 1::DOUBLE l, 'x' m, 'x'::BLOB n, DATE '2024-01-01' o, "
            "TIME '01:00' p, TIMESTAMP '2024-01-01' q, TIMESTAMP_MS '2024-01-01' r, "
            "TIMESTAMP_NS '2024-01-01' s, TIMESTAMPTZ '2024-01-01' t, 1.5::DECIMAL(10,2) u, "
            "[1::BIGINT] v, {'a': 1::BIGINT, 'b': 'x'} w"
        )
        reported = [str(t) for t in relation.types]

    expected = {duckdb_type for duckdb_type, _ in _VOCABULARY}
    assert set(reported) <= expected, set(reported) - expected


@pytest.mark.parametrize(
    "duckdb_type", ["INTERVAL", "TIMESTAMP_S", "UUID", "UHUGEINT", "MAP(VARCHAR, INTEGER)"]
)
def test_types_without_a_spelling_are_an_error_not_a_default(duckdb_type: str) -> None:
    with pytest.raises(dtypes.UnsupportedDtype, match="no Builder dtype"):
        dtypes.canonical_dtype(duckdb_type)


def test_the_runtime_is_not_part_of_the_package_root() -> None:
    import kpubdata_builder.tabular as tabular

    assert not {"TabularRelation", "build_connection", "connect"} & set(tabular.__all__)
