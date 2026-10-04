"""Column profiles of a committed snapshot (#817)."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, cast

import polars as pl
import pytest
import yaml

from kpubdata_builder.query.engine import QueryEngine, QueryTimeoutError
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.profile import (
    MIN_RANGE_VALUES,
    RANGE_TRIM,
    UNCHECKED_VALUES_KIND,
    ProfilePlan,
    profile_table,
)
from kpubdata_builder.query.service import (
    EXPORT_TIMEOUT_SECONDS,
    PROFILE_TIMEOUT_SECONDS,
    QueryService,
)
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE, warehouse_workspace
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import SnapshotLayout, TableCatalog, materialize
from kpubdata_builder.warehouse import gc as warehouse_gc

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
_NAME = "air.station"
_DEV = Principal("dev")


def _no_client(**_: object) -> NoReturn:
    raise AssertionError("profiling must not open a provider client")


def _service(tmp_path: Path, engine: QueryService | None = None) -> BuilderService:
    return BuilderService(
        output_root=tmp_path,
        client_factory=_no_client,
        warehouse_root=tmp_path / "wh",
        query_service=engine,
    )


def _catalog(service: BuilderService) -> TableCatalog:
    catalog = service._table_catalog()
    assert catalog is not None
    return catalog


def _commit(catalog: TableCatalog, tmp_path: Path, frame: pl.DataFrame, workspace: str) -> str:
    gold = tmp_path / f"gold-{len(list(tmp_path.iterdir()))}"
    gold.mkdir()
    frame.write_parquet(gold / "table.parquet")
    return materialize(
        catalog, workspace_id=workspace, logical_name=_NAME, source_dir=gold, run_id="r"
    ).snapshot.id


def _columns(response: ServiceResponse) -> dict[str, dict[str, JsonValue]]:
    assert response.status_code == 200, response.body
    profile = cast(dict[str, JsonValue], response.body["profile"])
    return {cast(str, c["name"]): c for c in cast(list[dict[str, JsonValue]], profile["columns"])}


def _table(path: Path, frame: pl.DataFrame) -> str:
    frame.write_parquet(path)
    return str(path)


_OPEN = ProfilePlan(allow_all_pii=False, allow_columns=())


def test_edge_cases_are_counted_not_guessed(tmp_path: Path) -> None:
    n = _OPEN.min_range_values + 2
    frame = pl.DataFrame(
        {
            "amount": [float(i) for i in range(n - 2)] + [float("nan"), float("inf")],
            "all_null": pl.Series([None] * n, dtype=pl.Int64),
            "constant": [7] * n,
            "price": pl.Series([Decimal("12.50")] * n, dtype=pl.Decimal(10, 2)),
            "big": [2**60] * n,
            "at": [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)] * n,
            "label": ["x"] * n,
        }
    )

    body = profile_table(_table(tmp_path / "t.parquet", frame), _OPEN)
    cols = {c["name"]: c for c in cast(list[dict[str, JsonValue]], body["columns"])}

    assert body["row_count"] == n
    assert (body["scope"], body["accuracy"]) == (
        {"mode": "full", "sampled": False, "sample_size": None},
        "exact",
    )
    amount = cast(dict[str, JsonValue], cols["amount"]["range"])
    assert (cols["amount"]["nan_count"], cols["amount"]["infinite_count"]) == (1, 1)
    # n - 2 finite values 0..n-3; RANGE_TRIM are left out at each end (#903).
    assert (amount["status"], amount["trimmed_count"]) == ("trimmed", 2 * RANGE_TRIM)
    assert (amount["min"], amount["max"], amount["excluded_count"]) == (
        float(RANGE_TRIM),
        n - 3.0 - RANGE_TRIM,
        2,
    )
    assert cols["all_null"]["null_ratio"] == 1.0
    assert cols["all_null"]["range"] == {
        "status": "no_values",
        "value_count": 0,
        "excluded_count": 0,
    }
    constant = cast(dict[str, JsonValue], cols["constant"]["range"])
    assert (constant["min"], constant["max"]) == (7, 7)
    price = cast(dict[str, JsonValue], cols["price"]["range"])
    assert (price["min"], price["wire_encoding"]) == ("12.50", "decimal_string")
    assert cast(dict[str, JsonValue], cols["big"]["range"])["min"] == str(2**60)
    assert cols["at"]["time_zone"] == "UTC"
    assert cast(dict[str, JsonValue], cols["at"]["range"])["min"] == "2026-01-01T00:00:00+00:00"
    assert cols["label"]["range"] == {"status": "not_applicable"}
    assert cols["label"]["nan_count"] is None


def test_an_empty_table_has_no_ratio_rather_than_zero(tmp_path: Path) -> None:
    frame = pl.DataFrame({"v": pl.Series([], dtype=pl.Float64)})

    body = profile_table(_table(tmp_path / "t.parquet", frame), _OPEN)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])

    assert body["row_count"] == 0
    assert column["null_ratio"] is None
    assert cast(dict[str, JsonValue], column["range"])["status"] == "no_values"


def test_a_small_group_range_is_withheld(tmp_path: Path) -> None:
    """Negative: a min or max over a handful of values can point at one record."""
    frame = pl.DataFrame({"income": [1, 2, 900_000_000] + [None] * 20})

    body = profile_table(_table(tmp_path / "t.parquet", frame), _OPEN)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])

    assert column["range"] == {
        "status": "withheld_small_group",
        "value_count": 3,
        "excluded_count": 0,
    }


def test_one_records_extreme_is_not_the_reported_min_or_max(tmp_path: Path) -> None:
    """Negative (#903): the highest income and the earliest date are one record's."""
    incomes = [*range(30_000, 30_040), 900_000_000, -5]
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(41)] + [dt.date(1931, 4, 2)]
    frame = pl.DataFrame({"income": incomes, "joined": days})

    body = profile_table(_table(tmp_path / "t.parquet", frame), _OPEN)
    income, joined = (
        cast(dict[str, JsonValue], column["range"])
        for column in cast(list[dict[str, JsonValue]], body["columns"])
    )

    assert body["range_trim"] == RANGE_TRIM
    assert income["status"] == joined["status"] == "trimmed"
    # 42 values: the 5 lowest are -5, 30000..30003 and the 5 highest 900000000, 30039..30036.
    assert (income["min"], income["max"]) == (30_004, 30_035)
    assert (joined["min"], joined["max"]) == ("2024-01-05", "2024-02-05")
    assert (income["value_count"], income["trimmed_count"]) == (42, 10)
    assert "900000000" not in json.dumps(body)
    assert "1931" not in json.dumps(body)


def test_a_value_many_records_share_is_still_reported(tmp_path: Path) -> None:
    """Trimming removes rows, not distinct values: a floor that RANGE_TRIM + 1 records
    sit on is not one record's value."""
    frame = pl.DataFrame({"fee": [0] * (RANGE_TRIM + 1) + list(range(100, 120))})

    body = profile_table(_table(tmp_path / "t.parquet", frame), _OPEN)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])

    assert cast(dict[str, JsonValue], column["range"])["min"] == 0


@pytest.mark.parametrize("count", [MIN_RANGE_VALUES, 2 * RANGE_TRIM])
def test_a_range_with_nothing_left_after_trimming_is_withheld(tmp_path: Path, count: int) -> None:
    """Ten values pass the small-group floor, but removing five from each end leaves none."""
    frame = pl.DataFrame({"v": list(range(count))})

    body = profile_table(_table(tmp_path / "t.parquet", frame), _OPEN)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])

    assert body["min_range_values"] == 2 * RANGE_TRIM + 1
    assert column["range"] == {
        "status": "withheld_small_group",
        "value_count": count,
        "excluded_count": 0,
    }


def test_the_smallest_trimmed_range_is_its_middle_value(tmp_path: Path) -> None:
    frame = pl.DataFrame({"v": list(range(2 * RANGE_TRIM + 1))})

    body = profile_table(_table(tmp_path / "t.parquet", frame), _OPEN)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])
    found = cast(dict[str, JsonValue], column["range"])

    assert (found["min"], found["max"]) == (RANGE_TRIM, RANGE_TRIM)


def test_the_plan_carries_the_trim_to_the_worker() -> None:
    plan = ProfilePlan(False, (), range_trim=2)

    assert ProfilePlan.from_json(plan.to_json()) == plan
    assert plan.min_range_values == MIN_RANGE_VALUES
    # A plan written before the trim existed is read with the default, not with none.
    legacy = '{"allow_all_pii": false, "allow_columns": []}'
    assert ProfilePlan.from_json(legacy).range_trim == RANGE_TRIM
    with pytest.raises(ValueError, match="range_trim"):
        ProfilePlan(False, (), range_trim=-1)


def test_an_untrimmed_plan_says_exact(tmp_path: Path) -> None:
    frame = pl.DataFrame({"v": list(range(20))})

    body = profile_table(_table(tmp_path / "t.parquet", frame), ProfilePlan(False, (), 0))
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])
    found = cast(dict[str, JsonValue], column["range"])

    assert (found["status"], found["min"], found["max"]) == ("exact", 0, 19)
    assert found["trimmed_count"] == 0


def test_suspected_personal_columns_are_not_profiled(tmp_path: Path) -> None:
    """Negative: a value pattern or a name heuristic withholds every statistic."""
    frame = pl.DataFrame(
        {"contact": ["010-1234-5678"] * 12, "owner_nm": ["a"] * 12, "count": list(range(12))}
    )

    body = profile_table(_table(tmp_path / "t.parquet", frame), _OPEN)
    cols = {c["name"]: c for c in cast(list[dict[str, JsonValue]], body["columns"])}

    for name, kind in (("contact", "phone"), ("owner_nm", "name")):
        column = cols[name]
        assert column["status"] == "withheld"
        assert column["sensitivity"] == {"status": "suspected", "kinds": [kind]}
        assert all(
            column[k] is None
            for k in ("null_count", "null_ratio", "nan_count", "infinite_count", "range")
        )
    assert cols["count"]["status"] == "profiled"


@pytest.mark.parametrize(
    "plan",
    [ProfilePlan(False, ("contact",)), ProfilePlan(True, ())],
    ids=["allow-column", "allow-all"],
)
def test_the_spec_policy_can_accept_a_column(tmp_path: Path, plan: ProfilePlan) -> None:
    frame = pl.DataFrame({"contact": ["010-1234-5678"] * 12})

    body = profile_table(_table(tmp_path / "t.parquet", frame), plan)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])

    assert column["status"] == "profiled"
    assert cast(dict[str, JsonValue], column["sensitivity"])["status"] == "allowed_by_spec"


class _CountingService(QueryService):
    def __init__(self) -> None:
        super().__init__()
        self.profiles = 0

    def execute_profile(self, table_path: Path, plan_json: str):  # type: ignore[no-untyped-def]
        self.profiles += 1
        return super().execute_profile(table_path, plan_json)


def test_the_endpoint_profiles_once_and_ties_it_to_the_snapshot(tmp_path: Path) -> None:
    engine = _CountingService()
    service = _service(tmp_path, engine)
    catalog = _catalog(service)
    snapshot = _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(20))}), PERSONAL_WORKSPACE)

    first = service.get_warehouse_profile(_NAME, "current", principal=_DEV)
    again = service.get_warehouse_profile(_NAME, snapshot, principal=_DEV)

    assert engine.profiles == 1
    profile = cast(dict[str, JsonValue], first.body["profile"])
    assert profile["snapshot_id"] == snapshot
    assert profile["artifact_digest"] == catalog.get_snapshot(snapshot).artifact_digest
    assert again.body == first.body
    assert catalog.live_lease_count(snapshot) == 0
    table_id = catalog.list_tables()[0].id
    assert SnapshotLayout(catalog.root, table_id).profile_path(snapshot).is_file()
    assert not any(
        p.name.endswith(".json") and "profile" in p.name
        for p in SnapshotLayout(catalog.root, table_id).snapshot_dir(snapshot).iterdir()
    )


def test_a_cached_profile_for_other_bytes_is_not_reused(tmp_path: Path) -> None:
    engine = _CountingService()
    service = _service(tmp_path, engine)
    catalog = _catalog(service)
    snapshot = _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(20))}), PERSONAL_WORKSPACE)
    service.get_warehouse_profile(_NAME, "current", principal=_DEV)
    path = SnapshotLayout(catalog.root, catalog.list_tables()[0].id).profile_path(snapshot)
    path.write_text(path.read_text().replace('"artifact_digest": "', '"artifact_digest": "x'))

    service.get_warehouse_profile(_NAME, "current", principal=_DEV)

    assert engine.profiles == 2


def test_garbage_collection_removes_the_profile_with_the_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    catalog = _catalog(service)
    old = _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(20))}), PERSONAL_WORKSPACE)
    service.get_warehouse_profile(_NAME, "current", principal=_DEV)
    _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(30))}), PERSONAL_WORKSPACE)
    table_id = catalog.list_tables()[0].id
    path = SnapshotLayout(catalog.root, table_id).profile_path(old)
    assert path.is_file()

    warehouse_gc.collect(catalog, table_id, keep=1)

    assert not path.exists()


def test_another_owners_table_and_no_warehouse_are_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    alice = Principal("oidc", "alice", "oidc:alice")
    service = _service(tmp_path)
    _commit(_catalog(service), tmp_path, pl.DataFrame({"v": [1]}), warehouse_workspace("oidc:bob"))

    response = service.get_warehouse_profile(_NAME, "current", principal=alice)
    (tmp_path / "bare").mkdir()
    bare = BuilderService(output_root=tmp_path / "bare", client_factory=_no_client)

    assert (response.status_code, response.body["code"]) == (404, "table_not_found")
    missing = bare.get_warehouse_profile(_NAME, "current", principal=_DEV)
    assert missing.body["code"] == "warehouse_not_configured"


def test_the_route_takes_the_snapshot_as_a_query_parameter(tmp_path: Path) -> None:
    service = _service(tmp_path)
    catalog = _catalog(service)
    old = _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(20))}), PERSONAL_WORKSPACE)
    _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(30))}), PERSONAL_WORKSPACE)

    response = dispatch(
        service, "GET", f"/warehouse/tables/{_NAME}/profile", None, f"snapshot={old}"
    )

    assert isinstance(response, ServiceResponse)
    assert _columns(response)["v"]["null_count"] == 0
    assert cast(dict[str, JsonValue], response.body["snapshot"])["snapshot_id"] == old


class _Result:
    def __init__(self) -> None:
        self.items = [{"id": str(i), "contact": "010-1234-5678", "pm10": i} for i in range(12)]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


@pytest.mark.parametrize(
    ("policy", "expected"),
    [("", "suspected"), ("pii:\n  mode: warn\n  allow_columns: [contact]\n", "allowed_by_spec")],
    ids=["no-policy", "allowed"],
)
def test_a_build_reads_its_specs_pii_policy(tmp_path: Path, policy: str, expected: str) -> None:
    """End to end: the policy of the run that produced the snapshot decides."""
    spec = (
        "dataset_id: e2e.air\ntitle: E2E\ndescription: d\n"
        "sources:\n  - provider: datago\n    dataset: air_quality\n"
        "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n" + policy
    )
    service = BuilderService(
        output_root=tmp_path, client_factory=lambda **_: _Client(), warehouse_root=tmp_path / "wh"
    )
    built = service.build(spec, run_id="r1")
    assert built.status_code == 200, built.body
    (committed,) = cast(dict[str, dict[str, JsonValue]], built.body["materialized"]).values()

    columns = _columns(
        service.get_warehouse_profile(
            cast(str, committed["logical_name"]), "current", principal=_DEV
        )
    )

    assert cast(dict[str, JsonValue], columns["contact"]["sensitivity"])["status"] == expected


_PHONE = "010-1234-5678"


@pytest.mark.parametrize(
    "series",
    [
        pl.Series("memo", [_PHONE] + ["x"] * 11, dtype=pl.Categorical),
        pl.Series("memo", [_PHONE] + ["x"] * 11, dtype=pl.Enum([_PHONE, "x"])),
        pl.Series("memo", [["a", _PHONE]] + [["x"]] * 11, dtype=pl.List(pl.String)),
        pl.Series("memo", [["a", _PHONE]] + [["x", "y"]] * 11, dtype=pl.Array(pl.String, 2)),
        pl.Series("memo", [[_PHONE]] + [None] * 11, dtype=pl.List(pl.Categorical)),
    ],
    ids=["categorical", "enum", "list-string", "array-string", "list-categorical"],
)
def test_text_values_are_pattern_checked_whatever_their_storage(
    tmp_path: Path, series: pl.Series
) -> None:
    """#897: one phone number in a plainly named non-String text column is found."""
    body = profile_table(_table(tmp_path / "t.parquet", series.to_frame()), _OPEN)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])

    assert column["sensitivity"] == {"status": "suspected", "kinds": ["phone"]}
    assert column["status"] == "withheld"
    assert column["null_count"] is None


@pytest.mark.parametrize(
    "series",
    [
        pl.Series("memo", ["x"] * 12, dtype=pl.Categorical),
        pl.Series("memo", [["a", "b"]] * 12, dtype=pl.List(pl.String)),
        pl.Series("memo", [[1, 2]] * 12, dtype=pl.List(pl.Int64)),
        pl.Series("memo", [{"n": 1}] * 12, dtype=pl.Struct({"n": pl.Int64})),
    ],
    ids=["categorical", "list-string", "list-int", "struct-of-int"],
)
def test_checked_or_textless_columns_without_a_match_are_profiled(
    tmp_path: Path, series: pl.Series
) -> None:
    """Negative: checking more types does not withhold columns that match nothing."""
    body = profile_table(_table(tmp_path / "t.parquet", series.to_frame()), _OPEN)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])

    assert column["sensitivity"] == {"status": "not_detected", "kinds": []}
    assert column["status"] == "profiled"


@pytest.mark.parametrize(
    "series",
    [
        pl.Series("memo", [{"note": "x"}] * 12, dtype=pl.Struct({"note": pl.String})),
        pl.Series("memo", [[["x"]]] * 12, dtype=pl.List(pl.List(pl.String))),
        pl.Series("memo", [b"x"] * 12, dtype=pl.Binary),
    ],
    ids=["struct", "list-of-list", "binary"],
)
def test_text_the_patterns_do_not_read_is_suspected(tmp_path: Path, series: pl.Series) -> None:
    """#897: a type that can hold text but is not pattern-checked is never not_detected."""
    body = profile_table(_table(tmp_path / "t.parquet", series.to_frame()), _OPEN)
    (column,) = cast(list[dict[str, JsonValue]], body["columns"])

    assert column["sensitivity"] == {"status": "suspected", "kinds": [UNCHECKED_VALUES_KIND]}
    assert column["status"] == "withheld"
    accepted = profile_table(str(tmp_path / "t.parquet"), ProfilePlan(False, ("memo",)))
    (allowed,) = cast(list[dict[str, JsonValue]], accepted["columns"])
    assert allowed["status"] == "profiled"


def test_profiling_has_its_own_timeout() -> None:
    """#896: a profile scans every row, so it does not share the query timeout."""
    service = QueryService()

    assert PROFILE_TIMEOUT_SECONDS == 60.0
    assert service._profile_engine._timeout_seconds == PROFILE_TIMEOUT_SECONDS
    assert service._engine._timeout_seconds == 10.0
    assert service._export_engine._timeout_seconds == EXPORT_TIMEOUT_SECONDS


class _TimingOutEngine(QueryEngine):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        self.calls += 1
        raise QueryTimeoutError("query execution timed out")


def test_a_timeout_is_not_rescanned_until_the_retry_window_passes(tmp_path: Path) -> None:
    """#896: refreshing after a timeout answers 504 at once and holds no query slot."""
    engine = _TimingOutEngine()
    service = _service(tmp_path, QueryService(profile_engine=engine, max_concurrency=1))
    now = [1000.0]
    service._profiles_api._timeouts._clock = lambda: now[0]
    catalog = _catalog(service)
    _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(20))}), PERSONAL_WORKSPACE)

    first = service.get_warehouse_profile(_NAME, "current", principal=_DEV)
    again = service.get_warehouse_profile(_NAME, "current", principal=_DEV)

    assert (first.status_code, first.body["code"]) == (504, "query_timeout")
    assert (again.status_code, again.body["code"]) == (504, "query_timeout")
    assert engine.calls == 1
    assert catalog.live_lease_count(catalog.list_tables()[0].current_snapshot_id or "") == 0

    now[0] += 301.0
    service.get_warehouse_profile(_NAME, "current", principal=_DEV)
    assert engine.calls == 2


def test_a_timeout_on_one_snapshot_does_not_block_another(tmp_path: Path) -> None:
    engine = _TimingOutEngine()
    service = _service(tmp_path, QueryService(profile_engine=engine))
    catalog = _catalog(service)
    old = _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(20))}), PERSONAL_WORKSPACE)
    service.get_warehouse_profile(_NAME, old, principal=_DEV)
    _commit(catalog, tmp_path, pl.DataFrame({"v": list(range(30))}), PERSONAL_WORKSPACE)

    service.get_warehouse_profile(_NAME, "current", principal=_DEV)

    assert engine.calls == 2


def _profile_schema() -> tuple[dict[str, object], dict[str, object]]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = response_schema(contract, "/warehouse/tables/{name}/profile", "get", 200)
    assert schema is not None
    return contract, schema


def test_the_profile_matches_the_contract_and_negative_counts_do_not(tmp_path: Path) -> None:
    """#896: nan_count and infinite_count are counts; -1 fails the schema."""
    service = _service(tmp_path)
    frame = pl.DataFrame({"v": [float(i) for i in range(19)] + [float("nan")]})
    _commit(_catalog(service), tmp_path, frame, PERSONAL_WORKSPACE)
    response = service.get_warehouse_profile(_NAME, "current", principal=_DEV)
    contract, schema = _profile_schema()
    assert validate(response.body, schema, contract) == []

    for field in ("nan_count", "infinite_count"):
        body = cast(dict[str, JsonValue], json.loads(json.dumps(response.body)))
        profile = cast(dict[str, JsonValue], body["profile"])
        cast(list[dict[str, JsonValue]], profile["columns"])[0][field] = -1

        assert any("minimum" in error for error in validate(body, schema, contract)), field
