"""Bronze/Silver/Gold stage summary/detail HTTP API  (#488).

 BuilderService.build()    run  ,
ownership, path-safety, preview cap, secret  .
(partial/failed ) test_stage_reader.py  .
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
import kpubdata_builder.service.stages_api as stages_api_module
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

VALID_SPEC_YAML = (
    "dataset_id: dataset.stage\n"
    "title: Stage Fixture\n"
    "description: fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "    alias: air\n"
    "    params:\n"
    "      api_key: SUPER-SECRET-API-KEY\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: out/data.jsonl\n"
    "    options:\n"
    "      kaggle_key: SUPER-SECRET-EXPORT-KEY\n"
)

# Silver validation       spec.
SILVER_FAILURE_SPEC_YAML = VALID_SPEC_YAML.replace(
    "    alias: air\n",
    "    alias: air\n    schema:\n      required: [does_not_exist]\n",
)

# fetch   spec (FakeClient  dataset).
FETCH_FAILURE_SPEC_YAML = VALID_SPEC_YAML.replace("dataset: air_quality", "dataset: missing")


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


def _service(tmp_path: Path, *, rows: int = 2) -> BuilderService:
    records = [{"id": str(i), "v": i * 10} for i in range(1, rows + 1)]
    client = _FakeClient({"datago.air_quality": records})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


def _build(service: BuilderService, run_id: str, spec_yaml: str = VALID_SPEC_YAML) -> int:
    resp = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": run_id})
    return resp.status_code


class TestListRunStages:
    def test_full_pipeline_reports_completed(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200

        resp = dispatch(service, "GET", "/builds/r1/stages", None)
        assert resp.status_code == 200
        sources = cast(list[dict[str, object]], resp.body["sources"])
        assert len(sources) == 1
        entry = sources[0]
        assert entry["source_key"] == "air"
        for stage in ("bronze", "silver", "gold"):
            stage_state = cast(dict[str, object], entry[stage])
            assert stage_state["status"] == "completed"
            assert stage_state["available"] is True

    def test_source_fetch_failure_bronze_failed_rest_not_run(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", FETCH_FAILURE_SPEC_YAML) == 502

        resp = dispatch(service, "GET", "/builds/r1/stages", None)
        assert resp.status_code == 200
        (entry,) = cast(list[dict[str, object]], resp.body["sources"])
        assert cast(dict[str, object], entry["bronze"])["status"] == "failed"
        assert cast(dict[str, object], entry["silver"])["status"] == "not_run"
        assert cast(dict[str, object], entry["gold"])["status"] == "not_run"

    def test_bronze_success_silver_validation_failure(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", SILVER_FAILURE_SPEC_YAML) == 502

        resp = dispatch(service, "GET", "/builds/r1/stages", None)
        (entry,) = cast(list[dict[str, object]], resp.body["sources"])
        assert cast(dict[str, object], entry["bronze"])["status"] == "completed"
        assert cast(dict[str, object], entry["silver"])["status"] == "failed"
        assert cast(dict[str, object], entry["gold"])["status"] == "not_run"

    def test_legacy_manifest_without_inputs_returns_empty_sources(self, tmp_path: Path) -> None:
        """inputs     manifest     ."""
        run_dir = tmp_path / "legacy"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

        resp = dispatch(_service(tmp_path), "GET", "/builds/legacy/stages", None)
        assert resp.status_code == 200
        assert resp.body["sources"] == []

    def test_invalid_utf8_manifest_returns_404_without_internal_details(
        self, tmp_path: Path
    ) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        (tmp_path / "r1" / "manifest.json").write_bytes(b"\xff\xfe")

        resp = dispatch(service, "GET", "/builds/r1/stages", None)
        assert resp.status_code == 404
        body_text = json.dumps(resp.body)
        assert "UnicodeDecodeError" not in body_text
        assert str(tmp_path) not in body_text

    def test_unknown_run_returns_404(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/nope/stages", None)
        assert resp.status_code == 404

    def test_unsafe_run_id_returns_400(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/../escape/stages", None)
        assert resp.status_code == 400


class TestStageDetailRouting:
    def test_invalid_stage_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(service, "GET", "/builds/r1/stages/platinum", None, query="source=air")
        assert resp.status_code == 400
        assert "stage" in str(resp.body.get("error", "")).lower()

    def test_missing_source_query_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(service, "GET", "/builds/r1/stages/bronze", None)
        assert resp.status_code == 400
        assert "source" in str(resp.body.get("error", "")).lower()

    def test_unknown_source_returns_404(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(
            service, "GET", "/builds/r1/stages/bronze", None, query="source=does-not-exist"
        )
        assert resp.status_code == 404

    def test_path_traversal_source_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(
            service, "GET", "/builds/r1/stages/bronze", None, query="source=../../etc/passwd"
        )
        assert resp.status_code == 400

    def test_path_traversal_run_id_returns_400(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path), "GET", "/builds/../escape/stages/bronze", None, query="source=air"
        )
        assert resp.status_code == 400

    def test_unknown_run_returns_404(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path), "GET", "/builds/nope/stages/bronze", None, query="source=air"
        )
        assert resp.status_code == 404

    def test_invalid_limit_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(
            service, "GET", "/builds/r1/stages/silver", None, query="source=air&limit=0"
        )
        assert resp.status_code == 400
        resp2 = dispatch(
            service, "GET", "/builds/r1/stages/silver", None, query="source=air&limit=abc"
        )
        assert resp2.status_code == 400
        resp3 = dispatch(
            service, "GET", "/builds/r1/stages/silver", None, query="source=air&limit=999999"
        )
        assert resp3.status_code == 400


class TestBronzeDetail:
    def test_bronze_detail_fields(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(service, "GET", "/builds/r1/stages/bronze", None, query="source=air")
        assert resp.status_code == 200
        assert resp.body["provider"] == "datago"
        assert resp.body["dataset"] == "air_quality"
        assert resp.body["record_count"] == 2
        assert resp.body["fetched_at"] is not None

    def test_bronze_detail_never_exposes_fetch_params_secret(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(service, "GET", "/builds/r1/stages/bronze", None, query="source=air")
        body_text = json.dumps(resp.body)
        assert "SUPER-SECRET-API-KEY" not in body_text
        assert "fetch_params" not in resp.body
        assert "provenance" not in resp.body
        assert str(tmp_path) not in body_text


class TestSilverDetail:
    def test_silver_detail_fields(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(service, "GET", "/builds/r1/stages/silver", None, query="source=air")
        assert resp.status_code == 200
        assert resp.body["row_count"] == 2
        assert isinstance(resp.body["schema"], list)
        assert isinstance(resp.body["statistics"], dict)
        assert isinstance(resp.body["validation"], dict)
        assert isinstance(resp.body["sample"], list)

    def test_silver_preview_hard_cap(self, tmp_path: Path) -> None:
        service = _service(tmp_path, rows=8)
        _build(service, "r1")

        capped = dispatch(
            service, "GET", "/builds/r1/stages/silver", None, query="source=air&limit=2"
        )
        assert capped.status_code == 200
        assert len(cast(list[object], capped.body["sample"])) == 2

        # persist  DEFAULT_PREVIEW_LIMIT(5) , limit
        #  5    (parquet   ).
        generous = dispatch(
            service, "GET", "/builds/r1/stages/silver", None, query="source=air&limit=100"
        )
        assert generous.status_code == 200
        assert len(cast(list[object], generous.body["sample"])) <= 5

    def test_silver_detail_no_absolute_path_leak(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(service, "GET", "/builds/r1/stages/silver", None, query="source=air")
        assert str(tmp_path) not in json.dumps(resp.body)

    def test_invalid_utf8_sidecar_returns_unavailable_without_internal_details(
        self, tmp_path: Path
    ) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        (tmp_path / "r1" / "silver" / "air" / "stats.json").write_bytes(b"\xff\xfe")

        resp = dispatch(service, "GET", "/builds/r1/stages/silver", None, query="source=air")
        assert resp.status_code == 200
        assert resp.body["status"] == "unavailable"
        assert resp.body["available"] is False
        assert resp.body["statistics"] is None
        assert resp.body["sample"] == []
        body_text = json.dumps(resp.body)
        assert str(tmp_path) not in body_text
        assert "UnicodeDecodeError" not in body_text


class TestGoldDetail:
    def test_gold_detail_fields(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(service, "GET", "/builds/r1/stages/gold", None, query="source=air")
        assert resp.status_code == 200
        assert resp.body["row_count"] == 2
        assert resp.body["columns"] == ["id", "v"]
        assert resp.body["exports"] == [{"kind": "jsonl"}]
        assert resp.body["sample"] is None
        assert resp.body["sample_available"] is False

    def test_gold_detail_never_exposes_export_options_or_path(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "r1")
        resp = dispatch(service, "GET", "/builds/r1/stages/gold", None, query="source=air")
        body_text = json.dumps(resp.body)
        assert "SUPER-SECRET-EXPORT-KEY" not in body_text
        assert "output_path" not in resp.body
        assert "options" not in resp.body
        assert str(tmp_path) not in body_text


class TestStageOwnership:
    """ownership 403 sidecar read    (#488)."""

    def test_stages_list_403_before_sidecar_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        _build(service, "r1")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        resp = dispatch(service, "GET", "/builds/r1/stages", None)
        assert resp.status_code == 403

        # ownership   stage sidecar    . (manifest.json
        # created_by  ownership    ,  **
        # stage summary    .)
        monkeypatch.setattr(
            stages_api_module.stages_service,
            "list_run_stages",
            lambda *a, **kw: pytest.fail("stage sidecar read leaked past 403"),
        )
        resp2 = dispatch(service, "GET", "/builds/r1/stages", None)
        assert resp2.status_code == 403

    def test_stage_detail_403_before_sidecar_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        _build(service, "r1")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        resp = dispatch(service, "GET", "/builds/r1/stages/bronze", None, query="source=air")
        assert resp.status_code == 403

        monkeypatch.setattr(
            stages_api_module.stages_service,
            "stage_status_for_source",
            lambda *a, **kw: pytest.fail("stage sidecar read leaked past 403"),
        )
        resp2 = dispatch(service, "GET", "/builds/r1/stages/bronze", None, query="source=air")
        assert resp2.status_code == 403

    def test_owner_still_reaches_stage_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        _build(service, "r1")

        resp = dispatch(service, "GET", "/builds/r1/stages", None)
        assert resp.status_code == 200
