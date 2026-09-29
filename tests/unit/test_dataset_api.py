"""Built Dataset Catalog/Detail API test (#488).

Validates run grouping for identical dataset_id, latest run selection,
legacy exclusion, ownership isolation, and multi-source row_count preservation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.datasets import (
    RunRecord,
    filter_ownership,
    group_latest_by_dataset,
    is_more_recent,
    pick_latest,
)
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue, parse_spec
from kpubdata_builder.spec.serializer import write_buildspec_snapshot
from kpubdata_builder.store import rebuild_index

_MULTI_SOURCE_SOURCES = (
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "    alias: air\n"
    "  - provider: datago\n"
    "    dataset: village_fcst\n"
    "    alias: fcst\n"
)

_SINGLE_SOURCE = "  - provider: datago\n    dataset: air_quality\n    alias: air\n"


def _spec_yaml(
    dataset_id: str,
    *,
    title: str = "Sample Dataset",
    sources: str = _SINGLE_SOURCE,
) -> str:
    return (
        f"dataset_id: {dataset_id}\n"
        f"title: {title}\n"
        "description: fixture\n"
        "sources:\n"
        f"{sources}"
        "exports:\n"
        "  - kind: jsonl\n"
        "    output_path: out/data.jsonl\n"
    )


def _write_fixture_run(
    tmp_path: Path,
    run_id: str,
    *,
    dataset_id: str,
    title: str = "Fixture",
    sources: str = _SINGLE_SOURCE,
    row_counts: dict[str, int] | None = None,
    started_at: str = "2025-01-01T00:00:00+00:00",
    finished_at: str = "2025-01-01T00:05:00+00:00",
    errors: tuple[str, ...] = (),
    created_by: str | None = None,
) -> None:
    """Record only canonical snapshot + manifest without running actual pipeline.

    When verifying logic like row_count aggregation that is independent of
    orchestrator's concurrent source execution (ThreadPoolExecutor), this
    deterministic fixture avoids depending on actual concurrent multi-source
    build execution. Snapshot is recorded with real spec.serializer
    .write_buildspec_snapshot so operations code uses identical canonical format.
    """
    spec_yaml = _spec_yaml(dataset_id, title=title, sources=sources)
    spec = parse_spec(cast(dict[str, object], yaml.safe_load(spec_yaml)))
    write_buildspec_snapshot(spec, output_root=tmp_path, run_id=run_id)

    run_dir = tmp_path / run_id
    manifest: dict[str, object] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "errors": list(errors),
        "row_counts": row_counts or {},
        "created_by": created_by,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> list[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient(
        {
            "datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}],
            "datago.village_fcst": [{"id": "1", "w": 1}, {"id": "2", "w": 2}, {"id": "3", "w": 3}],
        }
    )
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: client)


class TestDatasetGrouping:
    def test_cancelled_index_status_survives_canonicalization(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "run-cancelled", dataset_id="dataset.cancelled")
        service = _service(tmp_path)
        service._build_index.insert_or_replace(
            run_id="run-cancelled",
            status="cancelled",
            started_at="2025-01-01T00:00:00+00:00",
            finished_at="2025-01-01T00:05:00+00:00",
            dataset_id="dataset.cancelled",
        )

        detail = dispatch(service, "GET", "/datasets/dataset.cancelled", None)
        assert detail.status_code == 200
        assert detail.body["status"] == "cancelled"

        history = dispatch(service, "GET", "/datasets/dataset.cancelled/runs", None)
        assert history.status_code == 200
        (run,) = cast(list[dict[str, object]], history.body["runs"])
        assert run["status"] == "cancelled"

    def test_invalid_utf8_manifest_is_skipped_without_500(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "run-corrupt", dataset_id="dataset.corrupt")
        (tmp_path / "run-corrupt" / "manifest.json").write_bytes(b"\xff\xfe")
        service = _service(tmp_path)

        catalog = dispatch(service, "GET", "/datasets", None)
        assert catalog.status_code == 200
        assert catalog.body["datasets"] == []

        detail = dispatch(service, "GET", "/datasets/dataset.corrupt", None)
        assert detail.status_code == 404
        assert "UnicodeDecodeError" not in json.dumps(detail.body)
        assert str(tmp_path) not in json.dumps(detail.body)

    def test_partial_index_merges_all_canonical_filesystem_runs(self, tmp_path: Path) -> None:
        """Partial index neither hides nor duplicates canonical filesystem run."""
        for run_id, dataset_id in (
            ("run-a", "dataset.a"),
            ("run-b", "dataset.b"),
            ("run-c", "dataset.c"),
        ):
            _write_fixture_run(tmp_path, run_id, dataset_id=dataset_id)

        service = _service(tmp_path)
        service._build_index.insert_or_replace(
            run_id="run-c",
            status="ok",
            started_at="2025-01-01T00:00:00+00:00",
            finished_at="2025-01-01T00:05:00+00:00",
            dataset_id="dataset.c",
        )

        catalog = dispatch(service, "GET", "/datasets", None)
        datasets = cast(list[dict[str, object]], catalog.body["datasets"])
        assert {item["dataset_id"] for item in datasets} == {
            "dataset.a",
            "dataset.b",
            "dataset.c",
        }
        assert len(datasets) == 3

        for dataset_id in ("dataset.a", "dataset.b", "dataset.c"):
            detail = dispatch(service, "GET", f"/datasets/{dataset_id}", None)
            assert detail.status_code == 200
            assert detail.body["run_count"] == 1

        runs = dispatch(service, "GET", "/datasets/dataset.c/runs", None)
        assert [item["run_id"] for item in cast(list[dict[str, object]], runs.body["runs"])] == [
            "run-c"
        ]

    def test_more_than_500_runs_are_not_silently_truncated(self, tmp_path: Path) -> None:
        """run_count and request limit unaffected by hidden 500 cap."""
        _write_fixture_run(tmp_path, "bulk-000", dataset_id="dataset.bulk")
        snapshot = (tmp_path / "bulk-000" / "buildspec.yaml").read_bytes()
        manifest = (tmp_path / "bulk-000" / "manifest.json").read_bytes()
        for index in range(1, 505):
            run_dir = tmp_path / f"bulk-{index:03d}"
            run_dir.mkdir()
            (run_dir / "buildspec.yaml").write_bytes(snapshot)
            (run_dir / "manifest.json").write_bytes(manifest)
        _write_fixture_run(tmp_path, "sparse-000", dataset_id="dataset.sparse")

        service = _service(tmp_path)
        catalog = dispatch(service, "GET", "/datasets", None, query="limit=2")
        datasets = cast(list[dict[str, object]], catalog.body["datasets"])
        assert {item["dataset_id"] for item in datasets} == {
            "dataset.bulk",
            "dataset.sparse",
        }

        detail = dispatch(service, "GET", "/datasets/dataset.bulk", None)
        assert detail.status_code == 200
        assert detail.body["run_count"] == 505

        runs = dispatch(
            service,
            "GET",
            "/datasets/dataset.bulk/runs",
            None,
            query="limit=503",
        )
        assert runs.status_code == 200
        assert len(cast(list[object], runs.body["runs"])) == 503

    def test_two_runs_same_dataset_id_group_into_one_dataset(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "r1"})
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "r2"})

        resp = dispatch(service, "GET", "/datasets", None)
        assert resp.status_code == 200
        datasets = cast(list[dict[str, object]], resp.body["datasets"])
        assert len(datasets) == 1
        assert datasets[0]["dataset_id"] == "dataset.a"

    def test_different_dataset_id_are_separate_datasets(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "r1"})
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.b"), "run_id": "r2"})

        resp = dispatch(service, "GET", "/datasets", None)
        datasets = cast(list[dict[str, object]], resp.body["datasets"])
        ids = {d["dataset_id"] for d in datasets}
        assert ids == {"dataset.a", "dataset.b"}

    def test_latest_run_selected_by_finished_at(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "older"})
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "newer"})

        # 'newer' was built later so finished_at is more recent.
        resp = dispatch(service, "GET", "/datasets/dataset.a", None)
        assert resp.status_code == 200
        assert resp.body["latest_run_id"] == "newer"

    def test_multi_source_row_counts_are_preserved_not_collapsed(self, tmp_path: Path) -> None:
        # Use deterministic fixture not depending on actual orchestrator
        # concurrent multi-source execution
        # verify only row_count aggregation logic (Windows environment pre-existing concurrency
        # made independent of issue — reported separately).
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="dataset.multi",
            sources=_MULTI_SOURCE_SOURCES,
            row_counts={"air": 2, "fcst": 3},
        )
        service = _service(tmp_path)

        resp = dispatch(service, "GET", "/datasets/dataset.multi", None)
        assert resp.status_code == 200
        row_counts = cast(dict[str, int], resp.body["row_counts"])
        assert row_counts == {"air": 2, "fcst": 3}
        assert resp.body["total_row_count"] == 5
        sources = cast(list[dict[str, object]], resp.body["sources"])
        assert {s["alias"] for s in sources} == {"air", "fcst"}

    def test_quality_is_null_not_pass(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.q"), "run_id": "r1"})
        resp = dispatch(service, "GET", "/datasets/dataset.q", None)
        assert resp.body["quality"] is None

    def test_legacy_run_without_snapshot_excluded_from_datasets(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "r1"})

        legacy_dir = tmp_path / "legacy-run"
        legacy_dir.mkdir()
        (legacy_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "started_at": "2020-01-01T00:00:00+00:00",
                    "finished_at": "2020-01-01T00:05:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        # legacy run has no buildspec.yaml snapshot.

        # ADR 0003: legacy-run didn't go through service.build() so not yet in SQLite index
        # — rebuild_index rescans filesystem and reflects in index.
        # On Windows, open sqlite connection blocks file rename, so close existing connection
        # first, rebuild, then query with new BuilderService.
        service._build_index.close()
        rebuild_index(tmp_path)
        service2 = _service(tmp_path)

        datasets_resp = dispatch(service2, "GET", "/datasets", None)
        datasets = cast(list[dict[str, object]], datasets_resp.body["datasets"])
        assert all(d["latest_run_id"] != "legacy-run" for d in datasets)

        # legacy run still appears in /builds.
        builds_resp = dispatch(service2, "GET", "/builds", None)
        builds = cast(list[dict[str, object]], builds_resp.body["builds"])
        assert any(b["run_id"] == "legacy-run" for b in builds)

    def test_get_dataset_falls_back_to_filesystem_when_index_never_populated(
        self, tmp_path: Path
    ) -> None:
        """BuildIndex not yet populated (run exists only on filesystem,
        not yet indexed) — GET /datasets/{id} must find from canonical
        (snapshot+manifest).

        list_by_dataset() returns empty list for empty table without exception,
        so not distinguishing "not yet populated" from "truly absent" causes
        regression: fallback disabled, silent 404 returned.
        """
        _write_fixture_run(tmp_path, "r1", dataset_id="dataset.unindexed")
        resp = dispatch(_service(tmp_path), "GET", "/datasets/dataset.unindexed", None)
        assert resp.status_code == 200
        assert resp.body["latest_run_id"] == "r1"

    def test_get_dataset_not_found(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/datasets/nope", None)
        assert resp.status_code == 404

    def test_dataset_runs_not_found_for_unknown_dataset(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/datasets/nope/runs", None)
        assert resp.status_code == 404

    def test_dataset_runs_history_newest_first(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.hist"), "run_id": "r1"})
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.hist"), "run_id": "r2"})

        resp = dispatch(service, "GET", "/datasets/dataset.hist/runs", None)
        assert resp.status_code == 200
        runs = cast(list[dict[str, object]], resp.body["runs"])
        assert [r["run_id"] for r in runs] == ["r2", "r1"]
        assert runs[0]["spec_digest"] is not None

    def test_a_run_older_than_the_page_is_found_directly(self, tmp_path: Path) -> None:
        # studio#418: the list returns the newest `limit` runs; a permalink to an older
        # one must still open, not be read as invalid.
        service = _service(tmp_path)
        for run_id in ("r1", "r2", "r3"):
            dispatch(
                service, "POST", "/build", {"spec": _spec_yaml("dataset.old"), "run_id": run_id}
            )

        page = dispatch(service, "GET", "/datasets/dataset.old/runs", None, query="limit=2")
        assert [r["run_id"] for r in cast(list[dict[str, object]], page.body["runs"])] == [
            "r3",
            "r2",
        ]

        resp = dispatch(service, "GET", "/datasets/dataset.old/runs/r1", None)
        assert resp.status_code == 200
        assert resp.body["dataset_id"] == "dataset.old"
        run = cast(dict[str, object], resp.body["run"])
        assert run["run_id"] == "r1"
        # Same item shape as the list, so a client handles both alike.
        listed = cast(
            list[dict[str, object]],
            dispatch(service, "GET", "/datasets/dataset.old/runs", None).body["runs"],
        )
        assert run == next(r for r in listed if r["run_id"] == "r1")

    def test_another_datasets_run_is_not_found_here(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "ra"})
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.b"), "run_id": "rb"})

        assert dispatch(service, "GET", "/datasets/dataset.a/runs/rb", None).status_code == 404
        assert dispatch(service, "GET", "/datasets/dataset.a/runs/missing", None).status_code == 404

    def test_an_unsafe_run_id_is_rejected(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/datasets/dataset.a/runs/..", None)
        assert resp.status_code == 400

    def test_direct_run_lookup_with_a_slash_in_the_dataset_id(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _spec_yaml("dataset/with-slash"), "run_id": "r1"}
        )

        resp = dispatch(service, "GET", "/datasets/dataset%2Fwith-slash/runs/r1", None)
        assert resp.status_code == 200
        assert resp.body["dataset_id"] == "dataset/with-slash"

    def test_dataset_id_with_slash_is_percent_encoded_in_route(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(
            service,
            "POST",
            "/build",
            {"spec": _spec_yaml("dataset/with-slash"), "run_id": "r1"},
        )

        resp = dispatch(service, "GET", "/datasets/dataset%2Fwith-slash", None)
        assert resp.status_code == 200
        assert resp.body["dataset_id"] == "dataset/with-slash"

    def test_empty_dataset_id_returns_400(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/datasets/", None)
        assert resp.status_code == 400

    def test_list_datasets_respects_limit(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        for i in range(3):
            dispatch(
                service, "POST", "/build", {"spec": _spec_yaml(f"dataset.{i}"), "run_id": f"r{i}"}
            )
        resp = dispatch(service, "GET", "/datasets", None, query="limit=2")
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["datasets"])) == 2

    def test_list_datasets_rejects_invalid_limit(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/datasets", None, query="limit=0")
        assert resp.status_code == 400


class TestDatasetTotal:
    """GET /datasets `total` (#488 follow-up, additive, API 1.22.0).

    total = distinct dataset_id count after canonical grouping + ownership,
    before pagination. items.length/limit is not used as total.
    """

    def test_total_is_zero_when_no_datasets(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/datasets", None)
        assert resp.status_code == 200
        assert resp.body["datasets"] == []
        assert resp.body["total"] == 0

    def test_total_counts_distinct_datasets(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        for i in range(3):
            dispatch(
                service, "POST", "/build", {"spec": _spec_yaml(f"dataset.{i}"), "run_id": f"r{i}"}
            )
        resp = dispatch(service, "GET", "/datasets", None)
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["datasets"])) == 3
        assert resp.body["total"] == 3

    def test_total_ignores_pagination_limit(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        for i in range(5):
            dispatch(
                service, "POST", "/build", {"spec": _spec_yaml(f"dataset.{i}"), "run_id": f"r{i}"}
            )
        resp = dispatch(service, "GET", "/datasets", None, query="limit=2")
        assert resp.status_code == 200
        # Page respects limit but total reports all distinct count as-is.
        assert len(cast(list[object], resp.body["datasets"])) == 2
        assert resp.body["total"] == 5

    def test_multiple_runs_of_same_dataset_count_once(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "r1"})
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.a"), "run_id": "r2"})
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.b"), "run_id": "r3"})
        resp = dispatch(service, "GET", "/datasets", None)
        assert resp.status_code == 200
        assert resp.body["total"] == 2

    def test_legacy_run_excluded_from_total(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _spec_yaml("dataset.real"), "run_id": "r1"})

        # legacy run without buildspec.yaml snapshot — not grouping/total target.
        legacy_dir = tmp_path / "legacy-run"
        legacy_dir.mkdir()
        (legacy_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "started_at": "2020-01-01T00:00:00+00:00",
                    "finished_at": "2020-01-01T00:05:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        service._build_index.close()
        rebuild_index(tmp_path)
        service2 = _service(tmp_path)

        resp = dispatch(service2, "GET", "/datasets", None)
        assert resp.status_code == 200
        assert resp.body["total"] == 1
        assert resp.body["total"] == len(cast(list[object], resp.body["datasets"]))

    def test_total_excludes_other_users_datasets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)

        def _build_as(dataset_id: str, run_id: str, identifier: str) -> None:
            monkeypatch.setattr(
                app_module,
                "authenticate",
                lambda **_kwargs: Principal(kind="oidc", identifier=identifier),
            )
            resp = dispatch(
                service, "POST", "/build", {"spec": _spec_yaml(dataset_id), "run_id": run_id}
            )
            assert resp.status_code == 200

        _build_as("dataset.a", "r-a", "userA")
        _build_as("dataset.shared", "r-shared-a", "userA")
        _build_as("dataset.shared", "r-shared-b", "userB")
        _build_as("dataset.b", "r-b", "userB")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userA")
        )
        resp = dispatch(service, "GET", "/datasets", None)
        assert resp.status_code == 200
        # userA sees dataset.a + dataset.shared (own run) only — dataset.b excluded.
        assert {d["dataset_id"] for d in cast(list[dict[str, object]], resp.body["datasets"])} == {
            "dataset.a",
            "dataset.shared",
        }
        assert resp.body["total"] == 2

    def test_paginated_response_does_not_render_full_summary_past_the_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if datasets >> limit, expensive full summary only runs on page candidates.

        Post-#488 review: prevent regression of counting total across entire catalog
        via ``build_dataset_summary`` (re-parse snapshot+manifest + stage artifact probe).
        Datasets outside page counted via lightweight ``dataset_summary_renderable`` only.
        """
        service = _service(tmp_path)
        for i in range(6):
            dispatch(
                service, "POST", "/build", {"spec": _spec_yaml(f"dataset.{i}"), "run_id": f"r{i}"}
            )

        import kpubdata_builder.service.datasets as datasets_module

        summary_calls: list[str] = []
        renderable_calls: list[str] = []
        real_summary = datasets_module.build_dataset_summary
        real_renderable = datasets_module.dataset_summary_renderable

        def _spy_summary(output_root: Path, record: RunRecord, **kwargs: object) -> object:
            summary_calls.append(record.run_id)
            return real_summary(output_root, record)

        def _spy_renderable(output_root: Path, record: RunRecord) -> bool:
            renderable_calls.append(record.run_id)
            return real_renderable(output_root, record)

        monkeypatch.setattr(datasets_module, "build_dataset_summary", _spy_summary)
        monkeypatch.setattr(datasets_module, "dataset_summary_renderable", _spy_renderable)

        resp = dispatch(service, "GET", "/datasets", None, query="limit=1")
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["datasets"])) == 1
        assert resp.body["total"] == 6
        # full summary for page (limit=1) candidate only, rest light validation.
        assert len(summary_calls) == 1
        assert len(renderable_calls) == 5


class TestDatasetOwnership:
    """488 semantics D: other user runs excluded from grouping/latest even with same dataset_id."""

    def _build_as(
        self, service: BuilderService, dataset_id: str, run_id: str, identifier: str
    ) -> None:
        original = app_module.authenticate
        app_module.authenticate = lambda **_kwargs: Principal(kind="oidc", identifier=identifier)  # type: ignore[assignment]
        try:
            resp = dispatch(
                service, "POST", "/build", {"spec": _spec_yaml(dataset_id), "run_id": run_id}
            )
            assert resp.status_code == 200
        finally:
            app_module.authenticate = original

    def test_other_user_run_excluded_from_dataset_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared", "r-a", "userA")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userB")
        )
        resp = dispatch(service, "GET", "/datasets", None)
        assert resp.status_code == 200
        assert resp.body["datasets"] == []

        detail = dispatch(service, "GET", "/datasets/dataset.shared", None)
        assert detail.status_code == 404

    def test_another_users_run_is_forbidden_not_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # studio#418: the server, not the screen, refuses a run the caller does not own,
        # and says so differently from a run that does not exist.
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared", "r-a", "userA")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userB")
        )
        assert (
            dispatch(service, "GET", "/datasets/dataset.shared/runs/r-a", None).status_code == 403
        )
        assert (
            dispatch(service, "GET", "/datasets/dataset.shared/runs/nope", None).status_code == 404
        )

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userA")
        )
        assert (
            dispatch(service, "GET", "/datasets/dataset.shared/runs/r-a", None).status_code == 200
        )

    def test_latest_selection_does_not_leak_other_users_newer_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Newer run from other user with same dataset_id must not be selected as latest."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared2", "r-a-old", "userA")
        self._build_as(service, "dataset.shared2", "r-b-new", "userB")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userA")
        )
        resp = dispatch(service, "GET", "/datasets/dataset.shared2", None)
        assert resp.status_code == 200
        assert resp.body["latest_run_id"] == "r-a-old"
        assert resp.body["run_count"] == 1

    def test_dev_principal_sees_all_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared3", "r-a", "userA")
        self._build_as(service, "dataset.shared3", "r-b", "userB")

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: Principal(kind="dev"))
        resp = dispatch(service, "GET", "/datasets/dataset.shared3", None)
        assert resp.status_code == 200
        assert resp.body["run_count"] == 2


class TestGroupingLogic:
    """Pure function unit test for dataset grouping/latest selection (#488 semantics C)."""

    def _record(self, run_id: str, dataset_id: str, finished_at: str | None) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            dataset_id=dataset_id,
            status="ok",
            started_at=finished_at,
            finished_at=finished_at,
            spec_digest=None,
            created_by=None,
        )

    def test_is_more_recent_compares_finished_at(self) -> None:
        older = self._record("a", "d", "2025-01-01T00:00:00Z")
        newer = self._record("b", "d", "2025-01-02T00:00:00Z")
        assert is_more_recent(newer, older)
        assert not is_more_recent(older, newer)

    def test_tie_break_is_deterministic_by_run_id(self) -> None:
        same_time = "2025-01-01T00:00:00Z"
        a = self._record("run-a", "d", same_time)
        z = self._record("run-z", "d", same_time)
        # run_id descending tiebreak: "run-z" > "run-a"
        assert is_more_recent(z, a)
        assert not is_more_recent(a, z)
        assert pick_latest([a, z]).run_id == "run-z"
        assert pick_latest([z, a]).run_id == "run-z"

    def test_finished_at_present_beats_none(self) -> None:
        with_time = self._record("a", "d", "2025-01-01T00:00:00Z")
        without_time = self._record("b", "d", None)
        assert is_more_recent(with_time, without_time)
        assert pick_latest([without_time, with_time]).run_id == "a"

    def test_group_latest_by_dataset_picks_one_per_dataset(self) -> None:
        records = [
            self._record("a1", "dataset.a", "2025-01-01T00:00:00Z"),
            self._record("a2", "dataset.a", "2025-01-02T00:00:00Z"),
            self._record("b1", "dataset.b", "2025-01-01T00:00:00Z"),
        ]
        latest = group_latest_by_dataset(records)
        assert set(latest) == {"dataset.a", "dataset.b"}
        assert latest["dataset.a"].run_id == "a2"
        assert latest["dataset.b"].run_id == "b1"


class TestFilterOwnership:
    """datasets_service.filter_ownership — ownership shared by dataset/quality/stage
    pure function unit test of filter (#505: canonical owner_id first, legacy fallback)."""

    def _record(
        self, run_id: str, *, created_by: str | None, owner_id: str | None = None
    ) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            dataset_id="d",
            status="ok",
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:00:00Z",
            spec_digest=None,
            created_by=created_by,
            owner_id=owner_id,
        )

    def test_owner_id_match_wins_over_different_label(self) -> None:
        record = self._record("a", created_by="oidc:old-name", owner_id="oidc:canonical-abc")
        principal = Principal(kind="oidc", identifier="new-name", owner_id="oidc:canonical-abc")
        result = filter_ownership([record], principal, enforce=True)
        assert [r.run_id for r in result] == ["a"]

    def test_owner_id_mismatch_denied_despite_matching_label(self) -> None:
        record = self._record("a", created_by="oidc:userA", owner_id="oidc:real-owner")
        impostor = Principal(kind="oidc", identifier="userA", owner_id="oidc:different")
        assert filter_ownership([record], impostor, enforce=True) == []

    def test_legacy_record_without_owner_id_falls_back_to_label(self) -> None:
        record = self._record("a", created_by="oidc:userA", owner_id=None)
        principal = Principal(kind="oidc", identifier="userA")
        result = filter_ownership([record], principal, enforce=True)
        assert [r.run_id for r in result] == ["a"]

    def test_ambiguous_record_with_no_owner_info_fails_closed(self) -> None:
        record = self._record("a", created_by=None, owner_id=None)
        principal = Principal(kind="oidc", identifier="userA", owner_id="oidc:abc")
        assert filter_ownership([record], principal, enforce=True) == []

    def test_dev_and_service_principal_bypass_filter(self) -> None:
        record = self._record("a", created_by="oidc:userA", owner_id="oidc:canonical-abc")
        assert filter_ownership([record], Principal(kind="dev"), enforce=True) == [record]
        assert filter_ownership([record], Principal(kind="service"), enforce=True) == [record]

    def test_enforce_false_returns_all_records(self) -> None:
        record = self._record("a", created_by=None, owner_id=None)
        principal = Principal(kind="oidc", identifier="someone-else", owner_id="oidc:xyz")
        assert filter_ownership([record], principal, enforce=False) == [record]
