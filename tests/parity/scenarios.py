"""Scenarios whose current (Polars) results the DuckDB migration must keep (#865).

Each scenario runs the real canonical path — ``BuilderService`` build and the warehouse
query endpoints — on fixed inputs, and returns an engine-neutral dict
(``tests/parity/canonical.py``). ``scripts/generate_duckdb_parity_baseline.py`` writes those
dicts as golden files; ``test_duckdb_parity_baseline.py`` compares against them. The two are
separate so a failing comparison can never quietly rewrite its own expectation.

Nothing here changes production code, and no scenario needs a network or a key.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import os
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from kpubdata_builder.catalog_info import DatasetCatalogInfo
from kpubdata_builder.pipeline import card_facts
from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import FileResponse

from .canonical import (
    canonical_csv,
    canonical_json_file,
    canonical_jsonl,
    canonical_parquet,
    canonical_value,
    strip_text,
    strip_volatile,
)

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "duckdb_parity"
GOLDEN = ROOT / "tests" / "golden" / "duckdb_parity"
_DEV = Principal("dev")

#: Every export kind whose logical output the baseline pins.
_EXPORTS = [
    {"kind": "parquet", "output_path": "data.parquet"},
    {"kind": "csv", "output_path": "data.csv"},
    {"kind": "jsonl", "output_path": "data.jsonl"},
    {"kind": "markdown", "output_path": "data.md"},
    {"kind": "huggingface", "output_path": "hf", "options": {"format": "jsonl"}},
]


class _Result:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items


_Records = list[dict[str, Any]] | dict[str, list[dict[str, Any]]]


class _Client:
    """Serves fixed records — one list for every dataset, or one per dataset key."""

    def __init__(self, records: _Records) -> None:
        self._records = records
        self._key = ""

    def dataset(self, key: str) -> _Client:
        client = _Client(self._records)
        client._key = key
        return client

    def list(self, **_params: object) -> _Result:
        records = self._records
        if isinstance(records, dict):
            records = records[self._key]
        return _Result([dict(r) for r in records])


@contextmanager
def _workspace() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="parity-") as directory:
        yield Path(directory)


@contextmanager
def _environment(**values: str) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _service(root: Path, records: _Records | None = None) -> BuilderService:
    return BuilderService(
        output_root=root,
        client_factory=lambda **_: _Client(records or []),
        warehouse_root=root / "wh",
    )


def _run_outputs(run_dir: Path) -> dict[str, Any]:
    """Every stage's logical output of one run."""
    out: dict[str, Any] = {}
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    out["manifest"] = strip_volatile(
        canonical_value(
            {
                key: manifest.get(key)
                for key in (
                    "status",
                    "partial",
                    "errors",
                    "row_counts",
                    "schema_summaries",
                    "quality_results",
                    "schema_drift",
                    "provenance",
                    "composition",
                    "gold_selection",
                )
            }
        )
    )
    for bronze in sorted(run_dir.glob("bronze/*/*/raw_records.jsonl")):
        key = bronze.parts[-3]
        records = canonical_jsonl(bronze)
        out.setdefault("bronze", {})[key] = {
            "records": records,
            "record_count": len(records),
            "metadata": canonical_json_file(bronze.parent / "metadata.json"),
        }
    for silver in sorted(run_dir.glob("silver/*")):
        entry: dict[str, Any] = {"table": canonical_parquet(silver / "table.parquet")}
        for name in ("schema", "stats", "preview", "validation"):
            path = silver / f"{name}.json"
            if path.is_file():
                entry[name] = canonical_json_file(path)
        out.setdefault("silver", {})[silver.name] = entry
    for gold in sorted(run_dir.glob("gold/*")):
        entry = {"table": canonical_parquet(gold / "table.parquet")}
        for split in sorted(gold.glob("splits/*.parquet")):
            entry.setdefault("splits", {})[split.stem] = canonical_parquet(split)
        if (gold / "data.parquet").is_file():
            entry["export_parquet"] = canonical_parquet(gold / "data.parquet")
        if (gold / "data.csv").is_file():
            entry["export_csv"] = canonical_csv(gold / "data.csv")
        if (gold / "data.jsonl").is_file():
            entry["export_jsonl"] = canonical_jsonl(gold / "data.jsonl")
        if (gold / "data.md").is_file():
            entry["export_markdown"] = (gold / "data.md").read_text(encoding="utf-8")
        if (gold / "hf").is_dir():
            entry["export_huggingface"] = _layout(gold / "hf")
        out.setdefault("gold", {})[gold.name] = entry
    return out


def _layout(directory: Path) -> dict[str, Any]:
    """A directory export's files by relative path, each as its logical content."""
    layout: dict[str, Any] = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        name = str(path.relative_to(directory))
        if path.suffix == ".parquet":
            layout[name] = canonical_parquet(path)
        elif path.suffix == ".jsonl":
            layout[name] = canonical_jsonl(path)
        elif path.suffix == ".csv":
            layout[name] = canonical_csv(path)
        elif path.suffix == ".json":
            layout[name] = canonical_json_file(path)
        else:
            layout[name] = strip_text(path.read_text(encoding="utf-8"))
    return layout


def _spec_text(
    records_spec: dict[str, Any],
    *,
    exports: list[dict[str, Any]] | None = None,
    single_combination: bool = True,
) -> str:
    spec = dict(records_spec)
    sources = []
    for source in spec["sources"]:
        source = dict(source)
        if single_combination and source.get("param_grid"):
            source["param_grid"] = {k: [v[0]] for k, v in source["param_grid"].items()}
        sources.append(source)
    spec["sources"] = sources
    spec["exports"] = exports if exports is not None else _EXPORTS
    return yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)


def _build(root: Path, spec: str, records: _Records | None, **kwargs: Any) -> Any:
    service = _service(root, records)
    response = service.build(spec, run_id="r1", **kwargs)
    return service, response


def _build_outputs(spec: str, records: _Records) -> dict[str, Any]:
    with _workspace() as root:
        _, response = _build(root, spec, records)
        result: dict[str, Any] = {"status_code": response.status_code}
        if (root / "r1" / "manifest.json").is_file():
            result.update(_run_outputs(root / "r1"))
        else:
            result["body"] = strip_volatile(canonical_value(response.body))
        return result


# ------------------------------------------------------------------ spec scenarios


def _fixture_records(name: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = json.loads(
        (FIXTURES / f"{name}.records.json").read_text("utf-8")
    )["records"]
    return records


def spec_trades() -> dict[str, Any]:
    """The trades spec, plus a seeded ratio split so split membership is pinned too."""
    spec = yaml.safe_load((ROOT / "specs" / "seoul-apartment-trades.yaml").read_text("utf-8"))
    spec["splits"] = {"mode": "ratio", "ratios": {"train": 0.8, "test": 0.2}, "seed": 7}
    return _build_outputs(_spec_text(spec), _fixture_records("seoul-apartment-trades"))


def spec_rent() -> dict[str, Any]:
    spec = yaml.safe_load((ROOT / "specs" / "seoul-apartment-rent.yaml").read_text("utf-8"))
    return _build_outputs(_spec_text(spec), _fixture_records("seoul-apartment-rent"))


def composition_trades_rent() -> dict[str, Any]:
    """Two sources joined into one Gold table: the composition statistics and rows."""
    spec = {
        "dataset_id": "parity.composed",
        "title": "Parity composition",
        "description": "trades joined to rents by apartment",
        "sources": [
            {"provider": "datago", "dataset": "apt_trade", "alias": "trades"},
            {"provider": "datago", "dataset": "apt_rent", "alias": "rents"},
        ],
        "composition": {
            "name": "joined",
            "join": {"left": "trades", "right": "rents", "left_key": "aptNm", "right_key": "aptNm"},
        },
    }
    return _build_outputs(
        _spec_text(spec),
        {
            "datago.apt_trade": _fixture_records("seoul-apartment-trades"),
            "datago.apt_rent": _fixture_records("seoul-apartment-rent"),
        },
    )


def spec_bike() -> dict[str, Any]:
    spec = yaml.safe_load((ROOT / "specs" / "seoul-bike-rent-month.yaml").read_text("utf-8"))
    content = (FIXTURES / "seoul-bike-rent-month.jsonl").read_bytes()
    with _workspace() as root:
        service = _service(root)
        owner = "parity-owner"
        metadata = service._upload_repository.put(
            owner, content=content, format="jsonl", encoding="utf-8", original_filename="bike.jsonl"
        )
        spec["sources"][0]["upload_id"] = metadata.upload_id
        response = service.build(_spec_text(spec), run_id="r1", owner_id=owner)
        result: dict[str, Any] = {"status_code": response.status_code}
        if (root / "r1" / "manifest.json").is_file():
            result.update(_run_outputs(root / "r1"))
        return result


def replay_air_station() -> dict[str, Any]:
    """The fixture Builder ships (#837), through the real kpubdata client in replay mode."""
    from kpubdata_builder import cli
    from kpubdata_builder.replay import BUNDLED_FIXTURES

    spec = {
        "dataset_id": "parity.air_station",
        "title": "Parity air station",
        "description": "replay fixture baseline",
        "sources": [
            {
                "provider": "datago",
                "dataset": "air_station",
                "params": {"stationName": "강남구", "dataTerm": "daily"},
            }
        ],
    }
    with (
        _environment(
            KPUBDATA_MODE="replay",
            KPUBDATA_REPLAY_DIR=str(BUNDLED_FIXTURES),
            KPUBDATA_DATAGO_API_KEY="replay-placeholder",
        ),
        _workspace() as root,
    ):
        service = BuilderService(output_root=root, client_factory=cli._create_client)
        response = service.build(_spec_text(spec), run_id="r1")
        result: dict[str, Any] = {"status_code": response.status_code}
        if (root / "r1" / "manifest.json").is_file():
            result.update(_run_outputs(root / "r1"))
        return result


# ------------------------------------------------------------------ R1–R15


def _tiny_spec(schema: dict[str, Any] | None = None, **extra: Any) -> str:
    source: dict[str, Any] = {"provider": "datago", "dataset": "air_quality", "alias": "t"}
    if schema is not None:
        source["schema"] = schema
    spec: dict[str, Any] = {
        "dataset_id": "parity.tiny",
        "title": "Parity",
        "description": "d",
        "sources": [source],
        **extra,
    }
    return _spec_text(spec)


def _silver(records: list[dict[str, Any]], schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return _build_outputs(_tiny_spec(schema), records)


def r01_number_and_string() -> dict[str, Any]:
    return _silver([{"v": 1}, {"v": "a"}, {"v": 2}])


def r02_unsafe_int_and_float() -> dict[str, Any]:
    return _silver([{"v": 9007199254740993}, {"v": 1.5}, {"v": -9007199254740993}])


def r03_float_after_inference_window() -> dict[str, Any]:
    return _silver([{"v": i} for i in range(200)] + [{"v": 1.5}])


def r04_strict_numeric_string_casts() -> dict[str, Any]:
    records = [{"v": "1"}, {"v": " 2"}, {"v": "3.0"}, {"v": "x"}, {"v": ""}]
    return {
        "int": _silver(records, {"casts": {"v": "int"}}),
        "float": _silver(records, {"casts": {"v": "float"}}),
    }


def r05_strict_iso_date() -> dict[str, Any]:
    records = [{"d": "2024-01-01"}, {"d": "2024/01/02"}, {"d": "20240103"}, {"d": "2024-02-30"}]
    return _silver(records, {"casts": {"d": "date"}})


def r06_float_to_int() -> dict[str, Any]:
    return _silver([{"v": 1.9}, {"v": -1.9}, {"v": 2.5}], {"casts": {"v": "int"}})


def r07_case_insensitive_duplicate_columns() -> dict[str, Any]:
    return _silver([{"Name": "a", "name": "b", "NAME": "c"}])


def _warehouse_table(root: Path, records: list[dict[str, Any]]) -> tuple[BuilderService, str]:
    service, response = _build(root, _tiny_spec(), records)
    committed = next(iter(response.body["materialized"].values()))
    return service, committed["logical_name"]


def _response(response: Any) -> dict[str, Any]:
    return {
        "status_code": response.status_code,
        "body": strip_volatile(canonical_value(response.body)),
    }


def _query(records: list[dict[str, Any]], sqls: list[str]) -> dict[str, Any]:
    with _workspace() as root:
        service, table = _warehouse_table(root, records)
        return {
            sql: _response(service.query_warehouse({"table": table, "sql": sql}, principal=_DEV))
            for sql in sqls
        }


def r08_integer_sum() -> dict[str, Any]:
    records = [{"g": "a", "v": 2**53}, {"g": "a", "v": 2**53}, {"g": "b", "v": 1}]
    with _workspace() as root:
        service, table = _warehouse_table(root, records)
        aggregate = service.aggregate_warehouse(
            {
                "table": table,
                "group_by": ["g"],
                "measures": [{"fn": "sum", "column": "v", "additive": True, "as": "total"}],
                "order_by": [{"key": "g", "direction": "asc"}],
            },
            principal=_DEV,
        )
        sql = service.query_warehouse(
            {"table": table, "sql": "SELECT g, SUM(v) AS total FROM dataset GROUP BY g ORDER BY g"},
            principal=_DEV,
        )
        return {"aggregate": _response(aggregate), "sql": _response(sql)}


def r09_interval() -> dict[str, Any]:
    return _query(
        [{"d": "2024-01-31"}],
        [
            "SELECT CAST(d AS DATE) + INTERVAL '1' DAY AS next_day FROM dataset",
            "SELECT CAST(d AS DATE) + INTERVAL '1' MONTH AS next_month FROM dataset",
        ],
    )


def r10_unnamed_aggregate_column() -> dict[str, Any]:
    return _query(
        [{"v": 1}, {"v": 2}],
        ["SELECT COUNT(*) FROM dataset", "SELECT SUM(v), MAX(v) FROM dataset"],
    )


def r11_null_sorting() -> dict[str, Any]:
    records: list[dict[str, Any]] = [{"v": 2}, {"v": None}, {"v": 1}]
    with _workspace() as root:
        service, table = _warehouse_table(root, records)
        result = {
            sql: _response(service.query_warehouse({"table": table, "sql": sql}, principal=_DEV))
            for sql in (
                "SELECT v FROM dataset ORDER BY v",
                "SELECT v FROM dataset ORDER BY v DESC",
            )
        }
        for direction in ("asc", "desc"):
            result[f"rows:{direction}"] = _response(
                service.read_warehouse_rows(
                    {"table": table, "sort": [{"column": "v", "direction": direction}]},
                    principal=_DEV,
                )
            )
        return result


def r12_parquet_logical_equality() -> dict[str, Any]:
    """The same input built twice: logical content equal, bytes free to differ."""
    first = _silver([{"a": 1, "b": "x"}, {"a": 2, "b": None}])
    second = _silver([{"a": 1, "b": "x"}, {"a": 2, "b": None}])
    return {"logically_equal": first == second, "table": first["gold"]}


def r13_zfill_over_width() -> dict[str, Any]:
    return _silver([{"c": "12"}, {"c": "12345"}, {"c": None}], {"zfill": {"c": 3}})


def r14_introspection_and_path_leakage() -> dict[str, Any]:
    return _query(
        [{"v": 1}],
        [
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM read_parquet('/etc/passwd')",
            "SELECT * FROM '/etc/passwd'",
            "SELECT current_setting('home_directory')",
            "SELECT * FROM duckdb_settings()",
            "SELECT * FROM dataset, pragma_version()",
        ],
    )


def r15_sqlglot_dialect() -> dict[str, Any]:
    """The validator's default-dialect parse, pinned per statement (query/security.py)."""
    statements = [
        'SELECT "Name" FROM dataset',
        "SELECT a || b FROM dataset",
        "SELECT CAST(v AS INT) FROM dataset",
        "SELECT v::INT FROM dataset",
        "SELECT * FROM dataset LIMIT 1 OFFSET 1",
        "SELECT * FROM dataset WHERE v ILIKE 'a%'",
        "SELECT * FROM dataset QUALIFY ROW_NUMBER() OVER () = 1",
        "SELECT DATE '2024-01-01' FROM dataset",
        "SELECT v FROM dataset WHERE v IS DISTINCT FROM 1",
    ]
    out: dict[str, Any] = {}
    for sql in statements:
        try:
            out[sql] = {"canonical_sql": validate_read_only_sql(sql).canonical_sql}
        except UnsafeQueryError as exc:
            out[sql] = {"error": str(exc)}
    return out


# ------------------------------------------------------------- query workers


def _bundle_member(name: str, content: bytes) -> Any:
    """An export bundle member's logical content: CSV rows, or JSON without volatile keys."""
    text = content.decode("utf-8-sig")
    if name.endswith(".csv"):
        return [row for row in csv.reader(io.StringIO(text))]
    if name.endswith(".json"):
        return strip_volatile(canonical_value(json.loads(text)))
    return strip_text(text)


def query_workers() -> dict[str, Any]:
    """The five query workers on one table: SQL, rows, aggregate, profile, export."""
    with _workspace() as root:
        service, table = _warehouse_table(root, _fixture_records("seoul-apartment-trades")[:12])
        export = service.create_warehouse_export(
            # Ordered on both columns: SQL leaves the order of ties undefined, and DuckDB's
            # sort does not keep it from one version to the next (#874).
            {
                "table": table,
                "sql": "SELECT aptNm, dealAmount FROM dataset ORDER BY aptNm, dealAmount",
            },
            principal=_DEV,
        )
        exported: Any = None
        export_body = export.body if isinstance(export.body, dict) else {}
        export_id = export_body.get("export_id")
        if export.status_code == 200 and isinstance(export_id, str):
            download = service.download_warehouse_export(export_id, principal=_DEV)
            if isinstance(download, FileResponse):
                with zipfile.ZipFile(download.file_path) as archive:
                    exported = {
                        name: _bundle_member(name, archive.read(name))
                        for name in sorted(archive.namelist())
                    }
        return {
            "sql": _response(
                service.query_warehouse(
                    {
                        "table": table,
                        "sql": "SELECT aptNm, COUNT(*) AS n FROM dataset GROUP BY aptNm "
                        "ORDER BY aptNm",
                    },
                    principal=_DEV,
                )
            ),
            "rows": _response(
                service.read_warehouse_rows(
                    {
                        "table": table,
                        "page_size": 5,
                        "sort": [{"column": "aptNm", "direction": "asc"}],
                    },
                    principal=_DEV,
                )
            ),
            "aggregate": _response(
                service.aggregate_warehouse(
                    {
                        "table": table,
                        "group_by": ["aptNm"],
                        "measures": [{"fn": "count_rows", "as": "n"}],
                        "order_by": [{"key": "aptNm", "direction": "asc"}],
                    },
                    principal=_DEV,
                )
            ),
            "profile": _response(service.get_warehouse_profile(table, "current", principal=_DEV)),
            "export": {"response": _response(export), "file": exported},
        }


#: What the kpubdata catalog said about the scenarios' datasets when the baseline was
#: made (kpubdata 0.8). Dataset cards read the catalog (#694), and a later kpubdata that
#: declares more (kpubdata#617 adds attributions) must not change the baseline: the
#: scenarios pin the catalog to this. A dataset not listed is not in the catalog.
FROZEN_CATALOG: dict[str, DatasetCatalogInfo] = {
    "datago.air_quality": DatasetCatalogInfo(
        source_url="https://www.data.go.kr", license_type="공공누리_1유형", attribution=None
    ),
    "datago.air_station": DatasetCatalogInfo(
        source_url="https://www.data.go.kr/data/15000581/openapi.do",
        license_type=None,
        attribution=None,
    ),
    "datago.apt_rent": DatasetCatalogInfo(
        source_url="https://www.data.go.kr", license_type="공공누리_1유형", attribution=None
    ),
    "datago.apt_trade": DatasetCatalogInfo(
        source_url="https://www.data.go.kr", license_type="공공누리_1유형", attribution=None
    ),
}


@contextmanager
def _frozen_catalog() -> Iterator[None]:
    original = card_facts.catalog_info
    card_facts.catalog_info = FROZEN_CATALOG.get
    try:
        yield
    finally:
        card_facts.catalog_info = original


def _pinned(scenario: Callable[[], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    @functools.wraps(scenario)
    def run() -> dict[str, Any]:
        with _frozen_catalog():
            return scenario()

    return run


#: Name → scenario. The name is the golden file's stem.
_SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "spec_seoul_apartment_trades": spec_trades,
    "spec_seoul_apartment_rent": spec_rent,
    "spec_seoul_bike_rent_month": spec_bike,
    "composition_trades_rent": composition_trades_rent,
    "replay_air_station": replay_air_station,
    "r01_number_and_string": r01_number_and_string,
    "r02_unsafe_int_and_float": r02_unsafe_int_and_float,
    "r03_float_after_inference_window": r03_float_after_inference_window,
    "r04_strict_numeric_string_casts": r04_strict_numeric_string_casts,
    "r05_strict_iso_date": r05_strict_iso_date,
    "r06_float_to_int": r06_float_to_int,
    "r07_case_insensitive_duplicate_columns": r07_case_insensitive_duplicate_columns,
    "r08_integer_sum": r08_integer_sum,
    "r09_interval": r09_interval,
    "r10_unnamed_aggregate_column": r10_unnamed_aggregate_column,
    "r11_null_sorting": r11_null_sorting,
    "r12_parquet_logical_equality": r12_parquet_logical_equality,
    "r13_zfill_over_width": r13_zfill_over_width,
    "r14_introspection_and_path_leakage": r14_introspection_and_path_leakage,
    "r15_sqlglot_dialect": r15_sqlglot_dialect,
    "query_workers": query_workers,
}
SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    name: _pinned(scenario) for name, scenario in _SCENARIOS.items()
}


__all__ = ["FIXTURES", "FROZEN_CATALOG", "GOLDEN", "SCENARIOS"]
