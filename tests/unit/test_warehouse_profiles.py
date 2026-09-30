"""Column profiles of a committed snapshot (#817)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, cast

import polars as pl
import pytest

from kpubdata_builder.query.profile import MIN_RANGE_VALUES, ProfilePlan, profile_table
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE, warehouse_workspace
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import SnapshotLayout, TableCatalog, materialize
from kpubdata_builder.warehouse import gc as warehouse_gc

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
    n = MIN_RANGE_VALUES + 2
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
    assert (amount["min"], amount["max"], amount["excluded_count"]) == (0.0, n - 3.0, 2)
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
