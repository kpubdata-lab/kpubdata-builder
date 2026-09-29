"""A provider's reported total is kept apart from what was fetched (#816).

"How many rows" used to be one number. The provider's own total, the rows fetched and the
rows in the committed snapshot are different numbers, and a partial fetch looked like the
whole population. These pin that:

- 0 and "not reported" stay apart;
- a total repeated on every page is read once, never summed;
- `param_grid` combinations keep their own totals and are never summed either;
- a fetch that collected fewer rows than reported is `partial`, in the manifest, on the
  committed snapshot and in the dataset's completeness axis.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from kpubdata_builder.manifest import summarize_reported_totals
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service.datasets import RunRecord, status_axes
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.build import build_bronze_artifact
from kpubdata_builder.stages.bronze.models import CallTotal
from kpubdata_builder.warehouse import TableCatalog
from kpubdata_builder.warehouse.catalog import CATALOG_FILENAME, SCHEMA_VERSION


@dataclass
class _Batch:
    """The part of kpubdata's RecordBatch Bronze reads."""

    items: list[dict[str, JsonValue]]
    total_count: int | None = None


@dataclass
class _Paged:
    """A paginated dataset: ``pages[params_key]`` is the list of pages for one call."""

    pages: dict[str, list[_Batch]] = field(default_factory=dict)

    def list(self, **params: JsonValue) -> _Batch:
        raise AssertionError("a paginated dataset is read with list_all")

    def list_all(self, **params: JsonValue) -> Iterator[_Batch]:
        yield from self.pages[json.dumps(params, sort_keys=True)]


@dataclass
class _Single:
    batch: _Batch

    def list(self, **params: JsonValue) -> _Batch:
        return self.batch


class _Client:
    def __init__(self, dataset: object) -> None:
        self._dataset = dataset

    def dataset(self, _key: str) -> Any:
        return self._dataset

    def close(self) -> None:
        return None


def _rows(n: int, start: int = 0) -> list[dict[str, JsonValue]]:
    return [{"id": str(start + i), "v": start + i} for i in range(n)]


def _key(**params: JsonValue) -> str:
    return json.dumps(params, sort_keys=True)


# ---------------------------------------------------------------- one call


def test_a_reported_zero_is_not_an_unknown_total() -> None:
    zero = build_bronze_artifact(_Client(_Single(_Batch([], total_count=0))), source_key="p.d")
    none = build_bronze_artifact(_Client(_Single(_Batch([], total_count=None))), source_key="p.d")

    assert zero.call_totals == (CallTotal(0, 0, "reported", 0, 1),)
    assert none.call_totals == (CallTotal(0, None, "unknown", 0, 1),)
    total, coverage = summarize_reported_totals(zero.call_totals, observed_at="t")
    assert (total.status, total.value, coverage.status) == ("reported", 0, "complete")
    total, coverage = summarize_reported_totals(none.call_totals, observed_at="t")
    assert (total.status, total.value, coverage.status) == ("unknown", None, "unknown")


def test_a_total_repeated_on_every_page_is_read_once() -> None:
    pages = [_Batch(_rows(100, 100 * i), total_count=250) for i in range(2)]
    pages.append(_Batch(_rows(50, 200), total_count=250))
    artifact = build_bronze_artifact(_Client(_Paged({_key(): pages})), source_key="p.d")

    (call,) = artifact.call_totals
    assert (call.value, call.pages, call.fetched_row_count) == (250, 3, 250)
    total, coverage = summarize_reported_totals(artifact.call_totals, observed_at="t")
    assert total.value == 250  # not 750
    assert coverage.status == "complete"


def test_fewer_rows_than_reported_is_partial() -> None:
    pages = [_Batch(_rows(100), total_count=300), _Batch(_rows(100, 100), total_count=300)]
    artifact = build_bronze_artifact(_Client(_Paged({_key(): pages})), source_key="p.d")

    total, coverage = summarize_reported_totals(artifact.call_totals, observed_at="t")

    assert (total.status, total.value) == ("reported", 300)
    assert coverage.status == "partial"
    assert coverage.reasons == ("call 0: fetched 200 of 300 reported",)


def test_pages_that_disagree_give_no_total() -> None:
    pages = [_Batch(_rows(1), total_count=2), _Batch(_rows(1, 1), total_count=3)]
    artifact = build_bronze_artifact(_Client(_Paged({_key(): pages})), source_key="p.d")

    total, coverage = summarize_reported_totals(artifact.call_totals, observed_at="t")

    assert (total.status, total.value) == ("inconsistent", None)
    assert coverage.status == "unknown"


def test_more_rows_than_reported_is_not_called_complete() -> None:
    artifact = build_bronze_artifact(
        _Client(_Single(_Batch(_rows(3), total_count=2))), source_key="p.d"
    )

    _total, coverage = summarize_reported_totals(artifact.call_totals, observed_at="t")

    assert coverage.status == "unknown"
    assert coverage.reasons == ("call 0: fetched 3, more than the 2 reported",)


# ---------------------------------------------------------------- param_grid


def test_param_grid_totals_are_kept_per_combination_and_not_summed() -> None:
    combos = [{"month": "01"}, {"month": "02"}]
    dataset = _Paged(
        {
            _key(month="01"): [_Batch(_rows(10), total_count=10)],
            _key(month="02"): [_Batch(_rows(4, 10), total_count=10)],
        }
    )

    artifact = build_bronze_artifact(_Client(dataset), source_key="p.d", param_combinations=combos)
    total, coverage = summarize_reported_totals(artifact.call_totals, observed_at="t")

    assert (total.status, total.value) == ("not_summed", None)
    assert [(c.index, c.value, c.fetched_row_count) for c in total.calls] == [
        (0, 10, 10),
        (1, 10, 4),
    ]
    assert coverage.status == "partial"
    assert coverage.reasons == ("call 1: fetched 4 of 10 reported",)


def test_overlapping_combinations_are_complete_without_a_sum() -> None:
    """Two combinations over the same rows report 5 each; the population is not 10."""
    combos = [{"region": "all"}, {"region": "seoul"}]
    dataset = _Paged(
        {
            _key(region="all"): [_Batch(_rows(5), total_count=5)],
            _key(region="seoul"): [_Batch(_rows(5), total_count=5)],
        }
    )

    artifact = build_bronze_artifact(_Client(dataset), source_key="p.d", param_combinations=combos)
    total, coverage = summarize_reported_totals(artifact.call_totals, observed_at="t")

    assert total.value is None
    assert coverage.status == "complete"


def test_a_source_without_a_provider_reports_nothing() -> None:
    total, coverage = summarize_reported_totals((), observed_at="t")

    assert (total.status, total.value, total.calls) == ("not_reported", None, ())
    assert coverage.status == "unknown"


# ---------------------------------------------------------------- end to end

_SPEC = """\
dataset_id: partial-fetch
title: Partial fetch
description: The provider reports more rows than it returned
sources:
  - provider: datago
    dataset: air_quality
"""


def _service(tmp_path: Path, batch: _Batch) -> BuilderService:
    runs = tmp_path / "runs"
    runs.mkdir()
    return BuilderService(
        output_root=runs,
        client_factory=lambda **_: _Client(_Single(batch)),
        warehouse_root=tmp_path / "warehouse",
    )


def _build(service: BuilderService) -> dict[str, Any]:
    response = service.build(_SPEC, run_id="run1")
    assert response.status_code == 200, response.body
    manifest = json.loads(
        (service._output_root / "run1" / "manifest.json").read_text(encoding="utf-8")
    )
    return cast(dict[str, Any], manifest)


def test_a_partial_fetch_is_partial_in_the_manifest_snapshot_and_dataset(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _Batch(_rows(3), total_count=10))

    manifest = _build(service)

    (entry,) = manifest["provenance"]
    assert entry["fetched_row_count"] == 3
    assert entry["source_reported_total"]["status"] == "reported"
    assert entry["source_reported_total"]["value"] == 10
    assert entry["coverage"] == {
        "status": "partial",
        "reasons": ["call 0: fetched 3 of 10 reported"],
    }

    detail = dispatch(service, "GET", "/warehouse/tables/partial-fetch.datago.air_quality", None)
    assert detail.status_code == 200, detail.body
    (snapshot,) = cast(list[dict[str, Any]], detail.body["snapshots"])
    assert snapshot["coverage"]["status"] == "partial"
    assert snapshot["coverage"]["fetched_row_count"] == 3
    assert snapshot["coverage"]["source_reported_total"]["value"] == 10
    assert snapshot["row_count"] == 3

    dataset = dispatch(service, "GET", "/datasets/partial-fetch", None)
    assert dataset.status_code == 200, dataset.body
    axes = cast(dict[str, JsonValue], dataset.body["status_axes"])
    assert axes["completeness"] == "partial"


def test_a_complete_fetch_stays_complete(tmp_path: Path) -> None:
    service = _service(tmp_path, _Batch(_rows(3), total_count=3))

    manifest = _build(service)

    assert manifest["provenance"][0]["coverage"] == {"status": "complete", "reasons": []}
    dataset = dispatch(service, "GET", "/datasets/partial-fetch", None)
    assert cast(dict[str, JsonValue], dataset.body["status_axes"])["completeness"] == "complete"


def test_completeness_ignores_manifests_without_coverage() -> None:
    record = RunRecord("r1", "partial-fetch", "ok", None, None, None, None)
    legacy = {"row_counts": {"a": 1}, "provenance": [{"provider": "p", "dataset": "d"}]}

    assert status_axes(legacy, record)["completeness"] == "complete"


# ---------------------------------------------------------------- catalog


def test_a_version_4_catalog_gains_the_coverage_column(tmp_path: Path) -> None:
    root = tmp_path / "wh"
    TableCatalog(root).close()
    with closing(sqlite3.connect(root / CATALOG_FILENAME)) as conn:
        conn.execute("ALTER TABLE table_snapshots DROP COLUMN coverage")
        conn.execute("UPDATE schema_version SET version = 4")
        conn.commit()

    reopened = TableCatalog(root)
    table = reopened.create_table("ws", "t")
    row = reopened.begin_snapshot(
        table.id,
        run_id="r",
        schema_version=1,
        coverage_hash="",
        artifact_digest="",
        coverage='{"status": "partial"}',
    )

    assert reopened.get_snapshot(row.id).coverage == '{"status": "partial"}'
    assert SCHEMA_VERSION == 5
    with closing(sqlite3.connect(root / CATALOG_FILENAME)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 5


@pytest.mark.parametrize("raw", [None, "not json", "[1, 2]"])
def test_an_unreadable_or_missing_coverage_reads_as_unknown(raw: str | None) -> None:
    from kpubdata_builder.service.warehouse_api import _coverage

    assert _coverage(raw) is None
