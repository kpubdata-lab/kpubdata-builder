"""Quality History/Detail API tests (#486).

``GET /datasets/{dataset_id}/quality/history`` and ``GET /builds/{run_id}/quality``
#488 reuse dataset→run lookup helper/ownership semantics to correctly aggregate/expose.
Verify.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
import yaml

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue, parse_spec
from kpubdata_builder.spec.serializer import write_buildspec_snapshot

_SPEC_YAML = (
    "dataset_id: {dataset_id}\n"
    "title: Fixture\n"
    "description: fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "    alias: air\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: out/data.jsonl\n"
)


def _write_fixture_run(
    tmp_path: Path,
    run_id: str,
    *,
    dataset_id: str,
    quality_results: dict[str, list[dict[str, object]]] | None = None,
    row_counts: dict[str, int] | None = None,
    errors: tuple[str, ...] = (),
    started_at: str = "2025-01-01T00:00:00+00:00",
    finished_at: str = "2025-01-01T00:05:00+00:00",
    created_by: str | None = None,
    include_quality_key: bool = True,
    inputs: tuple[str, ...] | None = None,
) -> None:
    """Deterministic fixture that records canonical snapshot + manifest without actual pipeline.

    Reuse the same pattern (_write_fixture_run) from test_dataset_api.py — #488's
    dataset→run lookup works only with filesystem source of truth (snapshot+manifest),
    so quality aggregation logic can verify with same minimal fixture.
    """
    spec_yaml = _SPEC_YAML.format(dataset_id=dataset_id)
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
    if inputs is not None:
        manifest["inputs"] = list(inputs)
    if include_quality_key:
        manifest["quality_results"] = quality_results or {}
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

    def list(self, **_params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


_PASS = {
    "source_key": "air",
    "category": "row_count",
    "rule": "min_rows",
    "column": None,
    "status": "pass",
    "actual": 10,
    "threshold": 1,
    "affected_rows": None,
    "evaluated_rows": None,
    "detail": None,
}


def _result(status: str, **overrides: object) -> dict[str, object]:
    return {**_PASS, "status": status, **overrides}


class TestDatasetQualityHistoryAggregation:
    def test_pass_warn_fail_counts(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={
                "air": [
                    _result("pass"),
                    _result("warn", rule="max_null_ratio"),
                    _result("fail", rule="max_duplicate_rate"),
                ]
            },
        )
        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        assert resp.status_code == 200
        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["pass_count"] == 1
        assert entry["warn_count"] == 1
        assert entry["fail_count"] == 1
        assert entry["evaluated_checks"] == 3
        assert entry["rule_pass_rate"] == pytest.approx(1 / 3)

    def test_denominator_zero_yields_null_pass_rate(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a", quality_results={})

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["evaluated_checks"] == 0
        assert entry["rule_pass_rate"] is None

    def test_legacy_run_without_quality_results_field(self, tmp_path: Path) -> None:
        """legacy run without manifest.quality_results field is represented as 0/None."""
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a", include_quality_key=False)

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["evaluated_checks"] == 0
        assert entry["rule_pass_rate"] is None
        assert entry["pass_count"] == 0

    def test_legacy_run_never_interpreted_as_all_pass(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a", include_quality_key=False)

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["rule_pass_rate"] != 1.0

    def test_partial_failed_run_included_with_structured_results(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            errors=["air: quality check failed"],
            quality_results={"air": [_result("fail")]},
        )

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["status"] == "failed"
        assert entry["fail_count"] == 1

    def test_multi_source_aggregates_across_sources(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={
                "air": [_result("pass")],
                "fcst": [_result("pass"), _result("fail")],
            },
        )

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["evaluated_checks"] == 3
        assert entry["pass_count"] == 2
        assert entry["fail_count"] == 1

    def test_validated_rows_uses_row_counts_total_not_evaluated_rows_sum(
        self, tmp_path: Path
    ) -> None:
        """validated_rows uses sum of row_counts, evaluated_rows by number of rules
        does not double-sum (#486 #16)."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            row_counts={"air": 100, "fcst": 50},
            quality_results={
                "air": [
                    _result("pass", evaluated_rows=100),
                    _result("pass", rule="max_null_ratio", evaluated_rows=100),
                ],
                "fcst": [_result("pass", evaluated_rows=50)],
            },
        )

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        # naive sum of evaluated_rows would be 250; correct validated_rows is 150.
        assert entry["validated_rows"] == 150

    def test_validated_rows_ignores_boolean_row_count_values(self, tmp_path: Path) -> None:
        """corrupted manifest's bool row_count value is not summed as 1/0 (#486)."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            row_counts={"air": 100},
        )
        run_dir = tmp_path / "r1"
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["row_counts"]["fcst"] = True
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        # bool True must not add 1 to the total.
        assert entry["validated_rows"] == 100

    def test_quality_fail_build_preserves_validated_rows(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        spec_yaml = (
            _SPEC_YAML.format(dataset_id="d.failed")
            + "quality:\n"
            + "  min_rows: 10\n"
            + "  min_rows_severity: fail\n"
        )

        build = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": "failed"})
        history = dispatch(service, "GET", "/datasets/d.failed/quality/history", None)

        assert build.status_code == 502
        assert history.status_code == 200
        (entry,) = cast(list[dict[str, object]], history.body["runs"])
        assert entry["status"] == "failed"
        assert entry["fail_count"] == 1
        assert entry["validated_rows"] == 1

    def test_history_newest_first(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path, "r1", dataset_id="d.a", finished_at="2025-01-01T00:00:00+00:00"
        )
        _write_fixture_run(
            tmp_path, "r2", dataset_id="d.a", finished_at="2025-06-01T00:00:00+00:00"
        )

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        runs = cast(list[dict[str, object]], resp.body["runs"])
        assert [r["run_id"] for r in runs] == ["r2", "r1"]


class TestDatasetQualityHistoryRequestHandling:
    def test_not_found_for_unknown_dataset(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/datasets/nope/quality/history", None)
        assert resp.status_code == 404

    def test_rejects_invalid_limit(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a")
        resp = dispatch(
            _service(tmp_path), "GET", "/datasets/d.a/quality/history", None, query="limit=0"
        )
        assert resp.status_code == 400

    def test_respects_limit(self, tmp_path: Path) -> None:
        for i in range(3):
            _write_fixture_run(
                tmp_path,
                f"r{i}",
                dataset_id="d.a",
                finished_at=f"2025-01-0{i + 1}T00:00:00+00:00",
            )
        resp = dispatch(
            _service(tmp_path), "GET", "/datasets/d.a/quality/history", None, query="limit=2"
        )
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["runs"])) == 2

    def test_default_limit_is_30(self, tmp_path: Path) -> None:
        for i in range(35):
            _write_fixture_run(
                tmp_path,
                f"r{i:03d}",
                dataset_id="d.a",
                finished_at=f"2025-01-01T00:{i:02d}:00+00:00",
            )
        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["runs"])) == 30


class TestDatasetQualityHistoryOwnership:
    def _build_as(
        self, service: BuilderService, dataset_id: str, run_id: str, identifier: str
    ) -> None:
        original = app_module.authenticate
        app_module.authenticate = lambda **_kwargs: Principal(kind="oidc", identifier=identifier)  # type: ignore[assignment]
        try:
            spec_yaml = _SPEC_YAML.format(dataset_id=dataset_id)
            resp = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": run_id})
            assert resp.status_code == 200
        finally:
            app_module.authenticate = original

    def test_other_user_run_excluded_from_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared", "r-a", "userA")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userB")
        )
        resp = dispatch(service, "GET", "/datasets/dataset.shared/quality/history", None)
        assert resp.status_code == 404

    def test_same_dataset_id_different_users_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared2", "r-a", "userA")
        self._build_as(service, "dataset.shared2", "r-b", "userB")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userA")
        )
        resp = dispatch(service, "GET", "/datasets/dataset.shared2/quality/history", None)
        assert resp.status_code == 200
        runs = cast(list[dict[str, object]], resp.body["runs"])
        assert [r["run_id"] for r in runs] == ["r-a"]

    def test_dev_principal_sees_all_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared3", "r-a", "userA")
        self._build_as(service, "dataset.shared3", "r-b", "userB")

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: Principal(kind="dev"))
        resp = dispatch(service, "GET", "/datasets/dataset.shared3/quality/history", None)
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["runs"])) == 2


class TestBuildQualityDetail:
    def test_returns_quality_results_and_schema_drift_from_manifest(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={"air": [_result("pass")]},
        )
        run_dir = tmp_path / "r1"
        manifest = cast(dict[str, object], json.loads((run_dir / "manifest.json").read_text()))
        manifest["schema_drift"] = {
            "air": [{"kind": "column_added", "column": "new_col", "detail": "new column"}]
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["run_id"] == "r1"
        assert resp.body["availability"] == "available"
        assert resp.body["evaluated_checks"] == 1
        quality_results = cast(dict[str, object], resp.body["quality_results"])
        assert quality_results["air"][0]["status"] == "pass"  # type: ignore[index]
        schema_drift = cast(dict[str, object], resp.body["schema_drift"])
        assert schema_drift["air"][0]["kind"] == "column_added"  # type: ignore[index]

    def test_exposes_structured_threshold_and_detail(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        spec_yaml = (
            _SPEC_YAML.format(dataset_id="d.range")
            + "quality:\n"
            + "  range:\n"
            + "    - column: v\n"
            + "      min: 0\n"
            + "      max: 100\n"
        )

        build = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": "range"})
        resp = dispatch(service, "GET", "/builds/range/quality", None)

        assert build.status_code == 200
        assert resp.status_code == 200
        quality_results = cast(dict[str, list[dict[str, object]]], resp.body["quality_results"])
        range_result = next(r for r in quality_results["air"] if r["rule"] == "range")
        assert range_result["threshold"] == {"min": 0, "max": 100}
        assert range_result["detail"] is None

    def test_legacy_manifest_without_quality_fields_returns_empty(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a", include_quality_key=False)

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "unavailable"
        assert resp.body["evaluated_checks"] == 0
        assert resp.body["quality_results"] == {}
        assert resp.body["schema_drift"] == {}

    def test_404_for_unknown_run(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/nope/quality", None)
        assert resp.status_code == 404

    def test_403_for_non_owner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userA")
        )
        resp = dispatch(
            service,
            "POST",
            "/build",
            {"spec": _SPEC_YAML.format(dataset_id="d.owned"), "run_id": "r-owned"},
        )
        assert resp.status_code == 200

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userB")
        )
        resp = dispatch(service, "GET", "/builds/r-owned/quality", None)
        assert resp.status_code == 403


class TestBuildQualityAvailability:
    """Verify ``availability``/``evaluated_checks`` distinguish 4 states (#514).

    Empty ``quality_results`` alone cannot distinguish "0 evaluations" from "never computed" —
    this class verifies that distinction (available/partial/unavailable, evaluated_checks
    0 vs >0) is correctly judged per manifest.inputs (known source) coverage.
    """

    def test_available_with_zero_evaluated_checks(self, tmp_path: Path) -> None:
        """All known sources covered but zero checks evaluated."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={"air": []},
            inputs=("air",),
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "available"
        assert resp.body["evaluated_checks"] == 0

    def test_available_with_evaluated_checks(self, tmp_path: Path) -> None:
        """All known sources covered, at least one check evaluated."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={"air": [_result("pass"), _result("warn")]},
            inputs=("air",),
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "available"
        assert resp.body["evaluated_checks"] == 2

    def test_partial_when_a_known_source_is_missing_from_quality_results(
        self, tmp_path: Path
    ) -> None:
        """One source's quality result completely absent in multi-source run (partial)."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={"air": [_result("pass")]},
            inputs=("air", "traffic"),
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "partial"
        assert resp.body["evaluated_checks"] == 1

    def test_unavailable_when_no_known_source_has_quality_results(self, tmp_path: Path) -> None:
        """New manifest writer writes quality_results={} even if no quality computed,"""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={},
            inputs=("air", "traffic"),
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "unavailable"
        assert resp.body["evaluated_checks"] == 0

    def test_unavailable_for_legacy_manifest_without_quality_results_field(
        self, tmp_path: Path
    ) -> None:
        """legacy run without quality_results field (pre-#486)."""
        _write_fixture_run(
            tmp_path, "r1", dataset_id="d.a", include_quality_key=False, inputs=("air",)
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "unavailable"
        assert resp.body["evaluated_checks"] == 0


class TestQualitySummary:
    """GET /quality/summary — last 24h cross-run quality aggregate (#486 follow-up, API 1.22.0).

    Verify WARN/PASS/FAIL run count aggregation, 24h boundary, unevaluated run exclusion,
    ownership filtering.
    Time basis is service-internal ``datetime.now``, so fixture uses relative timestamp from
    actual current time.
    """

    @staticmethod
    def _ago(hours: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def test_empty_is_available_with_zero_counts(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.status_code == 200
        assert resp.body["window"] == "24h"
        assert resp.body["availability"] == "available"
        assert resp.body["total_runs"] == 0
        assert resp.body["evaluated_runs"] == 0
        assert resp.body["warn_runs"] == 0
        assert resp.body["fail_runs"] == 0
        assert resp.body["pass_runs"] == 0

    def test_warn_run_counted_once_despite_multiple_warn_checks(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            finished_at=self._ago(1),
            quality_results={
                "air": [
                    _result("warn", rule="max_null_ratio"),
                    _result("warn", rule="max_null_ratio", column="pm25"),
                    _result("pass"),
                ]
            },
        )
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.body["total_runs"] == 1
        assert resp.body["evaluated_runs"] == 1
        assert resp.body["warn_runs"] == 1
        assert resp.body["fail_runs"] == 0
        assert resp.body["pass_runs"] == 0

    def test_pass_only_run(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            finished_at=self._ago(2),
            quality_results={"air": [_result("pass"), _result("pass", rule="max_null_ratio")]},
        )
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.body["evaluated_runs"] == 1
        assert resp.body["pass_runs"] == 1
        assert resp.body["warn_runs"] == 0
        assert resp.body["fail_runs"] == 0

    def test_fail_only_run_not_in_warn(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            finished_at=self._ago(2),
            quality_results={"air": [_result("fail"), _result("pass")]},
        )
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.body["evaluated_runs"] == 1
        assert resp.body["fail_runs"] == 1
        assert resp.body["warn_runs"] == 0
        assert resp.body["pass_runs"] == 0

    def test_warn_and_fail_run_appears_in_both(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            finished_at=self._ago(2),
            quality_results={"air": [_result("warn"), _result("fail")]},
        )
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.body["evaluated_runs"] == 1
        assert resp.body["warn_runs"] == 1
        assert resp.body["fail_runs"] == 1
        assert resp.body["pass_runs"] == 0

    def test_unavailable_and_zero_check_runs_are_not_evaluated(self, tmp_path: Path) -> None:
        # legacy: quality_results field itself absent.
        _write_fixture_run(
            tmp_path,
            "r-legacy",
            dataset_id="d.a",
            finished_at=self._ago(1),
            include_quality_key=False,
        )
        # available but zero checks evaluated.
        _write_fixture_run(
            tmp_path,
            "r-empty",
            dataset_id="d.b",
            finished_at=self._ago(1),
            quality_results={},
        )
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.body["total_runs"] == 2
        assert resp.body["evaluated_runs"] == 0
        assert resp.body["warn_runs"] == 0
        assert resp.body["fail_runs"] == 0
        assert resp.body["pass_runs"] == 0

    def test_24h_boundary_excludes_older_run(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r-in",
            dataset_id="d.a",
            finished_at=self._ago(1),
            quality_results={"air": [_result("warn")]},
        )
        _write_fixture_run(
            tmp_path,
            "r-out",
            dataset_id="d.b",
            finished_at=self._ago(25),
            quality_results={"air": [_result("warn")]},
        )
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.body["total_runs"] == 1
        assert resp.body["warn_runs"] == 1

    def test_ownership_filtering_excludes_other_users_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        _write_fixture_run(
            tmp_path,
            "r-a",
            dataset_id="d.a",
            created_by="oidc:userA",
            finished_at=self._ago(1),
            quality_results={"air": [_result("warn")]},
        )
        _write_fixture_run(
            tmp_path,
            "r-b",
            dataset_id="d.b",
            created_by="oidc:userB",
            finished_at=self._ago(1),
            quality_results={"air": [_result("warn")]},
        )
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="userA"),
        )
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.body["total_runs"] == 1
        assert resp.body["warn_runs"] == 1

    def test_rejects_unsupported_window(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None, query="window=7d")
        assert resp.status_code == 400

    def test_healthy_index_does_not_read_historical_manifests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Do not re-read historical manifest when many old runs + few recent runs.

        #486 follow-up review: nominally 24h endpoint reads all-history manifest per request;
        prevent snapshot re-parsing regression. Narrow candidates by canonical manifest.json mtime,
        then re-verify only those candidates as source of truth.
        """
        from kpubdata_builder.store import rebuild_index

        old_cutoff = datetime.now(timezone.utc) - timedelta(hours=200)
        for i in range(6):
            _write_fixture_run(
                tmp_path,
                f"old-{i}",
                dataset_id=f"d.old{i}",
                # Default finished_at (2025-01-01) is outside 24h window — historical run.
                quality_results={"air": [_result("warn")]},
            )
            # Set manifest.json mtime to past like actual old run.
            aged = old_cutoff.timestamp()
            os.utime(tmp_path / f"old-{i}" / "manifest.json", (aged, aged))
        _write_fixture_run(
            tmp_path,
            "recent",
            dataset_id="d.recent",
            finished_at=self._ago(1),
            started_at=self._ago(1),
            quality_results={"air": [_result("warn")]},
        )
        rebuild_index(tmp_path)

        import kpubdata_builder.service.datasets as datasets_module

        read_run_ids: list[str] = []
        real_read_manifest = datasets_module.read_manifest

        def _spy_read_manifest(output_root: Path, run_id: str) -> object:
            read_run_ids.append(run_id)
            return real_read_manifest(output_root, run_id)

        monkeypatch.setattr(datasets_module, "read_manifest", _spy_read_manifest)

        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.status_code == 200
        assert resp.body["availability"] == "available"
        assert resp.body["total_runs"] == 1
        assert resp.body["warn_runs"] == 1
        # historical run's manifest is never read during request processing.
        assert not any(rid.startswith("old-") for rid in read_run_ids)

    def test_empty_index_falls_back_to_filesystem_scan(self, tmp_path: Path) -> None:
        """If BuildIndex is empty (not yet built), maintain existing filesystem fallback."""
        _write_fixture_run(
            tmp_path,
            "recent",
            dataset_id="d.recent",
            finished_at=self._ago(1),
            started_at=self._ago(1),
            quality_results={"air": [_result("warn")]},
        )
        # rebuild_index not called — index is empty.
        resp = dispatch(_service(tmp_path), "GET", "/quality/summary", None)
        assert resp.status_code == 200
        assert resp.body["availability"] == "available"
        assert resp.body["total_runs"] == 1
        assert resp.body["warn_runs"] == 1

    def test_stale_index_timestamp_does_not_drop_in_window_run(self, tmp_path: Path) -> None:
        """Even if BuildIndex row is stale (outside window), if canonical manifest is inside"""
        _write_fixture_run(
            tmp_path,
            "r-fresh",
            dataset_id="d.a",
            started_at=self._ago(1),
            finished_at=self._ago(1),
            quality_results={"air": [_result("warn")]},
        )
        service = _service(tmp_path)
        stale = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        older = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        # Deliberately seed derived index with stale rows + other rows outside window.
        service._build_index.insert_or_replace(
            run_id="r-fresh",
            status="ok",
            started_at=stale,
            finished_at=stale,
            dataset_id="d.a",
        )
        service._build_index.insert_or_replace(
            run_id="r-other-old",
            status="ok",
            started_at=older,
            finished_at=older,
            dataset_id="d.b",
        )
        resp = dispatch(service, "GET", "/quality/summary", None)
        assert resp.status_code == 200
        assert resp.body["availability"] == "available"
        assert resp.body["total_runs"] == 1
        assert resp.body["warn_runs"] == 1

    def test_run_absent_from_index_still_counted(self, tmp_path: Path) -> None:
        """A run absent from the index is still counted when the canonical manifest is in window.

        Even if index write is lost and a specific run is completely absent from
        index (other runs present), if the canonical manifest is inside the
        window it is included in the 24h aggregate (ADR 0003 fallback)."""
        _write_fixture_run(
            tmp_path,
            "r-unindexed",
            dataset_id="d.a",
            started_at=self._ago(2),
            finished_at=self._ago(2),
            quality_results={"air": [_result("warn")]},
        )
        service = _service(tmp_path)
        service._build_index.insert_or_replace(
            run_id="r-indexed-old",
            status="ok",
            started_at=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
            finished_at=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
            dataset_id="d.b",
        )
        resp = dispatch(service, "GET", "/quality/summary", None)
        assert resp.status_code == 200
        assert resp.body["total_runs"] == 1
        assert resp.body["warn_runs"] == 1
