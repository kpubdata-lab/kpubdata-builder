"""HTTP service façade (#36): validate/preview/build/artifacts logic and routing verification."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.http import _clear_cors_cache, make_handler
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

VALID_SPEC_YAML = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)

INVALID_SPEC_YAML = (
    # Parses OK but fails in validate_spec with unsupported exporter kind.
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: unsupported_format
    output_path: out/data.jsonl
""".strip()
    + "\n"
)


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeCatalog:
    """Mimics ``client.datasets`` — returns DatasetRef list filtered by provider."""

    def __init__(self, items: list[object] | None = None) -> None:
        self._items = items or []

    def list(self, *, provider: str | None = None) -> list[object]:
        if provider is None:
            return self._items
        return [i for i in self._items if getattr(i, "provider", None) == provider]


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(
        self,
        data: dict[str, list[dict[str, JsonValue]]],
        catalog_items: list[object] | None = None,
        auth_provider_names: tuple[str, ...] = (),
    ) -> None:
        self._data = data
        self.datasets = _FakeCatalog(catalog_items)
        self._auth_provider_names = auth_provider_names

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])

    def iter_authenticated_providers(self) -> tuple[object, ...]:
        return tuple(_FakeProvider(name) for name in self._auth_provider_names)


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name


class _CloseTrackingClient(_FakeClient):
    def __init__(
        self,
        data: dict[str, list[dict[str, JsonValue]]],
        catalog_items: list[object] | None = None,
    ) -> None:
        super().__init__(data, catalog_items=catalog_items)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)


class TestVersion:
    def test_version_reports_api_contract_version(self, tmp_path: Path) -> None:
        # #209: Meta endpoint announcing contract version.
        from kpubdata_builder.service import API_CONTRACT_VERSION

        resp = _service(tmp_path).version()
        assert resp.status_code == 200
        assert resp.body["api_version"] == API_CONTRACT_VERSION
        assert resp.body["service"] == "kpubdata-builder"

    def test_version_route(self, tmp_path: Path) -> None:
        from kpubdata_builder.service import API_CONTRACT_VERSION

        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200
        assert resp.body["api_version"] == API_CONTRACT_VERSION


class TestValidate:
    def test_valid_spec_returns_200(self, tmp_path: Path) -> None:
        from kpubdata_builder.service import API_CONTRACT_VERSION

        resp = _service(tmp_path).validate(VALID_SPEC_YAML)
        assert resp.status_code == 200
        assert resp.body["status"] == "valid"
        assert resp.body["dataset_id"] == "dataset.sample"
        # #209: Contract version in response so consumers can verify compatibility.
        assert resp.body["api_version"] == API_CONTRACT_VERSION

    def test_invalid_spec_returns_400(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).validate(INVALID_SPEC_YAML)
        assert resp.status_code == 400
        assert resp.body["status"] == "invalid"


class TestPreview:
    def test_returns_schema_and_sample(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=1)
        assert resp.status_code == 200
        previews = resp.body["previews"]
        assert isinstance(previews, list)
        assert previews[0]["source_key"] == "datago.air_quality"

    def test_preview_writes_no_files(self, tmp_path: Path) -> None:
        _service(tmp_path).preview(VALID_SPEC_YAML)
        # Exclude SQLite index files (#309, ADR 0003)
        files = [p.name for p in tmp_path.iterdir() if not p.name.startswith("_builds")]
        assert files == []

    def test_preview_closes_request_client(self, tmp_path: Path) -> None:
        client = _CloseTrackingClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.preview(VALID_SPEC_YAML)

        assert resp.status_code == 200
        assert client.close_calls == 1

    def test_returns_source_sample_and_diff_fields(self, tmp_path: Path) -> None:
        # #497: New fields included alongside existing fields (sample/total_rows/statistics).
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=2)
        assert resp.status_code == 200
        preview = resp.body["previews"][0]
        assert preview["sample_mode"] == "first"
        assert preview["diff_available"] is True
        assert isinstance(preview["source_sample"], list)
        assert isinstance(preview["diffs"], list)
        assert preview["transform_summary"] == {"changed_cells": 0, "changed_rows": 0}
        assert preview["diff_truncated"] is False

    def test_random_sample_mode_is_reproducible_through_service(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air_quality": [{"id": str(i)} for i in range(50)]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        first = service.preview(VALID_SPEC_YAML, limit=5, sample_mode="random", seed=3)
        second = service.preview(VALID_SPEC_YAML, limit=5, sample_mode="random", seed=3)

        assert first.status_code == second.status_code == 200
        assert (
            first.body["previews"][0]["source_sample"]
            == second.body["previews"][0]["source_sample"]
        )
        assert first.body["previews"][0]["sample_mode"] == "random"

    def test_wide_dataset_diffs_are_truncated_over_the_wire(self, tmp_path: Path) -> None:
        # #497 sample/diff memory limit: row count alone cannot restrict diff
        # item count; in real service responses, diffs respect limits and set
        # diff_truncated=true so clients don't mistake it for complete diff.
        from kpubdata_builder.pipeline import MAX_PREVIEW_DIFF_ITEMS

        column_count = MAX_PREVIEW_DIFF_ITEMS + 50
        columns = [f"c{i}" for i in range(column_count)]
        spec_yaml = (
            "dataset_id: dataset.wide\n"
            "title: Wide Dataset\n"
            "description: many columns\n"
            "sources:\n"
            "  - provider: datago\n"
            "    dataset: air_quality\n"
            "    schema:\n"
            "      casts:\n" + "".join(f"        {c}: int\n" for c in columns) + "exports:\n"
            "  - kind: jsonl\n"
            "    output_path: out/data.jsonl\n"
        )
        client = _FakeClient({"datago.air_quality": [dict.fromkeys(columns, "1")]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.preview(spec_yaml, limit=1)

        assert resp.status_code == 200
        preview = resp.body["previews"][0]
        assert preview["diff_available"] is True
        assert len(preview["diffs"]) == MAX_PREVIEW_DIFF_ITEMS
        assert preview["diff_truncated"] is True
        assert preview["transform_summary"]["changed_cells"] == column_count


class TestPreviewLimitGuard:
    def test_preview_direct_call_rejects_zero_limit(self, tmp_path: Path) -> None:
        # #225: Direct calls to BuilderService.preview() also return 400 if limit<1.
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=0)
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_direct_call_rejects_negative_limit(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=-5)
        assert resp.status_code == 400

    def test_preview_direct_call_rejects_limit_above_max(self, tmp_path: Path) -> None:
        # #497: New limit cap (1000) — previously unlimited (behavioral tightening).
        from kpubdata_builder.service.app import MAX_PREVIEW_LIMIT

        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=MAX_PREVIEW_LIMIT + 1)
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_direct_call_accepts_limit_at_max(self, tmp_path: Path) -> None:
        from kpubdata_builder.service.app import MAX_PREVIEW_LIMIT

        resp = _service(tmp_path).preview(VALID_SPEC_YAML, limit=MAX_PREVIEW_LIMIT)
        assert resp.status_code == 200

    def test_preview_direct_call_rejects_invalid_sample_mode(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(VALID_SPEC_YAML, sample_mode="shuffle")
        assert resp.status_code == 400
        assert "sample_mode" in str(resp.body.get("error", ""))

    def test_preview_direct_call_rejects_non_int_seed(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).preview(
            VALID_SPEC_YAML, sample_mode="random", seed=cast(int, "7")
        )
        assert resp.status_code == 400
        assert "seed" in str(resp.body.get("error", ""))

    def test_preview_direct_call_rejects_bool_seed(self, tmp_path: Path) -> None:
        # bool is int subtype but seed meaningless, so reject.
        resp = _service(tmp_path).preview(
            VALID_SPEC_YAML, sample_mode="random", seed=cast(int, True)
        )
        assert resp.status_code == 400
        assert "seed" in str(resp.body.get("error", ""))


class TestBuild:
    def test_build_runs_and_reports_manifest(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).build(VALID_SPEC_YAML, run_id="run1")
        assert resp.status_code == 200
        assert resp.body["status"] == "ok"
        assert resp.body["run_id"] == "run1"
        assert (tmp_path / "run1" / "manifest.json").exists()

    def test_build_closes_request_client_when_source_fails(self, tmp_path: Path) -> None:
        client = _CloseTrackingClient({})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.build(VALID_SPEC_YAML, run_id="run1")

        assert resp.status_code == 502
        assert client.close_calls == 1

    def test_manifest_route_returns_written_manifest_json(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1")

        resp = dispatch(service, "GET", "/builds/run1/manifest", None)

        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 200
        assert resp.body["build_id"] == "run1"
        assert resp.body["schema_version"] == "1.0.0"

    def test_manifest_route_strips_persisted_internal_owner_id(self, tmp_path: Path) -> None:
        """owner_id remains in persisted manifest but not exposed on HTTP wire (#505)."""
        service = _service(tmp_path)
        service.build(
            VALID_SPEC_YAML,
            run_id="run1",
            created_by="oidc:abcdef12",
            owner_id="oidc:canonical-owner",
        )

        persisted = json.loads((tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8"))
        assert persisted["owner_id"] == "oidc:canonical-owner"

        resp = dispatch(service, "GET", "/builds/run1/manifest", None)

        assert resp.status_code == 200
        assert "owner_id" not in resp.body
        assert resp.body["created_by"] == "oidc:abcdef12"

    def test_manifest_route_returns_404_for_missing_run(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/nope/manifest", None)

        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 404


def _file_source_spec_yaml(upload_id: str) -> str:
    return (
        f"""
dataset_id: dataset.uploaded
title: Uploaded Trades
description: file source build (#498)
sources:
  - kind: file
    upload_id: {upload_id}
    format: csv
    encoding: utf-8
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n"
    )


class TestUploads:
    """kind="file" source (#498) — POST /uploads isolates uploads by owner_id
    Verify end-to-end flow from BuildSpec through build/preview."""

    def test_create_upload_then_build_end_to_end(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        principal = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")

        created = service.create_upload(
            b"id,amount\n1,1000\n2,2500\n",
            format="csv",
            encoding="utf-8",
            original_filename="trades.csv",
            principal=principal,
        )
        assert created.status_code == 200
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        result = service.build(
            _file_source_spec_yaml(upload_id),
            run_id="upload-run",
            owner_id=principal.owner_id,
            principal=principal,
        )

        assert result.status_code == 200
        assert result.body["status"] == "ok"

    def test_build_rejects_upload_owned_by_another_principal(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        owner = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")
        other = Principal(kind="oidc", identifier="u2", owner_id="oidc:owner-2")

        created = service.create_upload(
            b"id\n1\n", format="csv", encoding="utf-8", original_filename=None, principal=owner
        )
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        result = service.build(
            _file_source_spec_yaml(upload_id),
            run_id="run-other-owner",
            owner_id=other.owner_id,
            principal=other,
        )

        assert result.status_code == 502
        outcomes = cast(list[dict[str, JsonValue]], result.body["outcomes"])
        assert outcomes[0]["status"] == "failed"
        assert "not found" in cast(str, outcomes[0]["error"])

    def test_get_and_delete_upload_round_trip(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        principal = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")
        created = service.create_upload(
            b"id\n1\n", format="csv", encoding="utf-8", original_filename=None, principal=principal
        )
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        fetched = service.get_upload(upload_id, principal=principal)
        assert fetched.status_code == 200
        assert fetched.body["upload_id"] == upload_id
        assert "content" not in fetched.body

        deleted = service.delete_upload(upload_id, principal=principal)
        assert deleted.status_code == 200
        assert deleted.body == {"upload_id": upload_id, "deleted": True}

        missing = service.get_upload(upload_id, principal=principal)
        assert missing.status_code == 404

    def test_get_upload_hides_existence_from_other_principal(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        owner = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")
        other = Principal(kind="oidc", identifier="u2", owner_id="oidc:owner-2")
        created = service.create_upload(
            b"id\n1\n", format="csv", encoding="utf-8", original_filename=None, principal=owner
        )
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        resp = service.get_upload(upload_id, principal=other)

        assert resp.status_code == 404

    def test_create_upload_rejects_corrupt_content(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        principal = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")

        resp = service.create_upload(
            b"not json",
            format="json",
            encoding="utf-8",
            original_filename=None,
            principal=principal,
        )

        assert resp.status_code == 400

    def test_create_upload_rejects_unsupported_format(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        principal = Principal(kind="oidc", identifier="u1", owner_id="oidc:owner-1")

        resp = service.create_upload(
            b"data", format="xlsx", encoding="utf-8", original_filename=None, principal=principal
        )

        assert resp.status_code == 400

    def test_preview_without_file_source_never_touches_upload_store(self, tmp_path: Path) -> None:
        """Preview without file source doesn't create uploads.sqlite3 (deferred)."""
        _service(tmp_path).preview(VALID_SPEC_YAML)

        assert not (tmp_path / ".service" / "uploads.sqlite3").exists()


class TestArtifacts:
    def test_lists_artifacts_after_build(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1")

        resp = service.artifacts("run1")
        assert resp.status_code == 200
        files = resp.body["files"]
        assert isinstance(files, list)
        assert any("manifest.json" in f for f in files)

    def test_missing_run_returns_404(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).artifacts("nope")
        assert resp.status_code == 404


class TestListBuilds:
    def test_empty_when_no_runs(self, tmp_path: Path) -> None:
        resp = _service(tmp_path).list_builds()
        assert resp.status_code == 200
        assert resp.body["builds"] == []

    def test_lists_run_after_build(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1")

        resp = service.list_builds()
        assert resp.status_code == 200
        builds = resp.body["builds"]
        assert isinstance(builds, list)
        assert len(builds) == 1
        assert builds[0]["run_id"] == "run1"  # type: ignore[index]
        assert builds[0]["status"] == "ok"  # type: ignore[index]

    def test_skips_dirs_without_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "no-manifest-dir").mkdir()
        resp = _service(tmp_path).list_builds()
        assert resp.status_code == 200
        assert resp.body["builds"] == []

    def test_dispatch_get_builds_returns_200(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run2")
        resp = dispatch(service, "GET", "/builds", None)
        assert resp.status_code == 200
        builds = resp.body["builds"]
        assert isinstance(builds, list)
        assert any(b["run_id"] == "run2" for b in builds)  # type: ignore[index,union-attr]

    def test_dispatch_limit_guard(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds", {"limit": 0})
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_dispatch_get_builds_query_limit(self, tmp_path: Path) -> None:
        # Must support ?limit=N query parameter (#252).
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run_q1")
        service.build(VALID_SPEC_YAML, run_id="run_q2")
        resp = dispatch(service, "GET", "/builds", None, query="limit=1")
        assert resp.status_code == 200
        builds = resp.body["builds"]
        assert isinstance(builds, list)
        assert len(builds) == 1

    def test_dispatch_get_builds_query_limit_guard(self, tmp_path: Path) -> None:
        # If query limit not positive integer, return 400 (#252).
        resp = dispatch(_service(tmp_path), "GET", "/builds", None, query="limit=0")
        assert resp.status_code == 400
        resp = dispatch(_service(tmp_path), "GET", "/builds", None, query="limit=abc")
        assert resp.status_code == 400


class TestDispatch:
    def test_routes_post_validate(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "POST", "/validate", {"spec": VALID_SPEC_YAML})
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 200

    def test_unknown_route_returns_404(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/nope", None)
        assert resp.status_code == 404

    def test_build_route(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "r2"}
        )
        assert resp.status_code == 200
        assert resp.body["run_id"] == "r2"

    def test_artifacts_route(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "r3"})
        resp = dispatch(service, "GET", "/artifacts/r3", None)
        assert resp.status_code == 200

    def test_preview_rejects_non_integer_limit(self, tmp_path: Path) -> None:
        # If client sends limit as wrong type, return 400, not default silently.
        resp = dispatch(
            _service(tmp_path), "POST", "/preview", {"spec": VALID_SPEC_YAML, "limit": "5"}
        )
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_rejects_non_positive_limit(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path), "POST", "/preview", {"spec": VALID_SPEC_YAML, "limit": 0}
        )
        assert resp.status_code == 400

    def test_preview_rejects_limit_above_max(self, tmp_path: Path) -> None:
        # #497: New cap (1000) — previous clients sending more will break
        # (behavioral tightening; previously unlimited).
        from kpubdata_builder.service.app import MAX_PREVIEW_LIMIT

        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "limit": MAX_PREVIEW_LIMIT + 1},
        )
        assert resp.status_code == 400
        assert "limit" in str(resp.body.get("error", ""))

    def test_preview_accepts_limit_at_max(self, tmp_path: Path) -> None:
        from kpubdata_builder.service.app import MAX_PREVIEW_LIMIT

        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "limit": MAX_PREVIEW_LIMIT},
        )
        assert resp.status_code == 200

    def test_preview_rejects_invalid_sample_mode(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "sample_mode": "shuffle"},
        )
        assert resp.status_code == 400
        assert "sample_mode" in str(resp.body.get("error", ""))

    def test_preview_rejects_non_string_sample_mode(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "sample_mode": 1},
        )
        assert resp.status_code == 400
        assert "sample_mode" in str(resp.body.get("error", ""))

    def test_preview_rejects_non_int_seed(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "sample_mode": "random", "seed": "7"},
        )
        assert resp.status_code == 400
        assert "seed" in str(resp.body.get("error", ""))

    def test_preview_rejects_bool_seed(self, tmp_path: Path) -> None:
        # bool is int subtype but reject for seed.
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "sample_mode": "random", "seed": True},
        )
        assert resp.status_code == 400
        assert "seed" in str(resp.body.get("error", ""))

    def test_preview_dispatch_passes_sample_mode_and_seed_through(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air_quality": [{"id": str(i)} for i in range(20)]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = dispatch(
            service,
            "POST",
            "/preview",
            {"spec": VALID_SPEC_YAML, "limit": 3, "sample_mode": "random", "seed": 5},
        )

        assert resp.status_code == 200
        assert resp.body["previews"][0]["sample_mode"] == "random"
        assert len(resp.body["previews"][0]["source_sample"]) == 3

    def test_preview_dispatch_defaults_sample_mode_to_first_when_omitted(
        self, tmp_path: Path
    ) -> None:
        # Existing clients without sample_mode/seed must work as before.
        resp = dispatch(
            _service(tmp_path), "POST", "/preview", {"spec": VALID_SPEC_YAML, "limit": 1}
        )
        assert resp.status_code == 200
        assert resp.body["previews"][0]["sample_mode"] == "first"

    def test_build_rejects_non_string_run_id(self, tmp_path: Path) -> None:
        # Non-string run_id must return 400, not silently auto-generate (#185).
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": 123}
        )
        assert resp.status_code == 400
        assert "run_id" in str(resp.body.get("error", ""))

    def test_build_rejects_blank_run_id(self, tmp_path: Path) -> None:
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "   "}
        )
        assert resp.status_code == 400

    def test_build_rejects_unsafe_run_id_with_400(self, tmp_path: Path) -> None:
        # Unsafe path run_id returns structured 400, not 500/disconnect (#200).
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "../bad"}
        )
        assert resp.status_code == 400
        assert "run_id" in str(resp.body.get("error", ""))


class TestApiKeyAuth:
    """API key auth (#248, #321, ADR 0006): X-API-Key verification, fail-closed policy."""

    def test_auth_required_when_env_and_dev_mode_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ADR 0006 fail-closed: deny auth when dev-mode unset + API key unset (401).
        monkeypatch.delenv("KPUBDATA_BUILDER_API_KEY", raising=False)
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 401
        assert resp.body["error"]  # Specific reason deferred to auth implementation

    def test_auth_skipped_in_dev_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dev-mode skips auth even without API key (local dev convenience).
        monkeypatch.delenv("KPUBDATA_BUILDER_API_KEY", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200

    def test_auth_skipped_in_dev_mode_variant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dev-mode="1" also skips auth.
        monkeypatch.delenv("KPUBDATA_BUILDER_API_KEY", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "1")
        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200

    def test_rejects_missing_api_key_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        resp = dispatch(_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 401
        assert resp.body["error"]  # Specific reason deferred to auth implementation

    def test_rejects_wrong_api_key_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        resp = dispatch(_service(tmp_path), "GET", "/version", None, api_key="wrong")
        assert resp.status_code == 401

    def test_accepts_matching_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        resp = dispatch(_service(tmp_path), "GET", "/version", None, api_key="secret")
        assert resp.status_code == 200

    def test_build_route_requires_api_key_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even expensive endpoints like /build must be uniformly protected.
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        resp = dispatch(
            _service(tmp_path), "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "auth1"}
        )
        assert resp.status_code == 401


class TestBuildFailureResponseCode:
    def test_failed_build_returns_502(self, tmp_path: Path) -> None:
        # If source fetch fails, status=failed + 502 — manifest stays with partial policy.
        missing_source_yaml = VALID_SPEC_YAML.replace("air_quality", "missing")
        resp = _service(tmp_path).build(missing_source_yaml, run_id="run1")

        assert resp.status_code == 502
        assert resp.body["status"] == "failed"
        assert (tmp_path / "run1" / "manifest.json").exists()


@pytest.fixture(autouse=True)
def clear_cors_cache() -> None:
    """Clear CORS cache before each test (#322)."""
    _clear_cors_cache()
    yield


@pytest.fixture()
def http_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterable[tuple[str, HTTPServer, threading.Thread]]:
    """Start actual HTTPServer on random port to verify adapter-level behavior."""
    # Test sets dev-mode to skip auth (#321, ADR 0006).
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    service = _service(tmp_path)
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        yield base_url, server, thread
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@pytest.fixture()
def http_server_with_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterable[tuple[str, HTTPServer, threading.Thread]]:
    """HTTPServer with auth enabled (dev-mode unset, API key set)."""
    monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
    service = _service(tmp_path)
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        yield base_url, server, thread
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


class TestPreviewWireSerialization:
    """POST /preview validates actual wire JSON serialization (#497, contract section 6).

    dict returned by ``BuilderService.preview()`` contains Python
    objects directly (same as #440+ existing ``sample`` field pattern).
    Check actual HTTP response bytes: ``source_sample``, ``sample`` and ``diffs`` are
    wire-encoded by ``tabular/wire.py`` (#735) — dates ISO 8601, Decimals and
    out-of-range integers as exact decimal text.
    """

    def _post_preview(
        self, service: BuilderService, spec_yaml: str, **body_extra: object
    ) -> dict[str, object]:
        server = HTTPServer(("127.0.0.1", 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        try:
            req = urllib.request.Request(
                f"{base_url}/preview",
                data=json.dumps({"spec": spec_yaml, **body_extra}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.0) as response:
                return cast(dict[str, object], json.loads(response.read()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_null_int_float_bool_string_date_and_datetime_survive_the_wire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        # No casts declared; raw record carries Python type values that kpubdata
        # Mimics common case where provider returns typed values — records_to_dataframe
        # These types inferred and carried as-is.
        row: dict[str, JsonValue] = {
            "id": "1",
            "n": None,
            "count": 3,
            "ratio": 1.5,
            "active": True,
            "label": "seoul",
            "d": date(2025, 1, 1),  # type: ignore[dict-item]
            "ts": datetime(2025, 1, 1, 12, 30, 0),  # type: ignore[dict-item]
        }
        client = _FakeClient({"datago.air_quality": [row]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        body = self._post_preview(service, VALID_SPEC_YAML, limit=1)

        preview = cast(dict[str, object], cast(list[object], body["previews"])[0])
        source_row = cast(dict[str, object], cast(list[object], preview["source_sample"])[0])
        assert source_row["n"] is None
        assert source_row["count"] == 3
        assert source_row["ratio"] == 1.5
        assert source_row["active"] is True
        assert source_row["label"] == "seoul"
        # Dates and datetimes are ISO 8601 in source_sample and sample alike — the same
        # rule as /query and the silver stage sample (#735). Before it, /preview sent
        # str() with a space separator while the other two paths sent ISO.
        assert source_row["d"] == "2025-01-01"
        assert source_row["ts"] == "2025-01-01T12:30:00"

        transformed_row = cast(dict[str, object], cast(list[object], preview["sample"])[0])
        assert transformed_row["d"] == "2025-01-01"
        assert transformed_row["ts"] == "2025-01-01T12:30:00"

    def test_timezone_aware_datetime_survives_the_wire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # test_silver.py::test_serializes_timezone_aware_datetime_values_as_iso_strings and
        # Same pattern (carry aware datetime directly, let polars normalize to UTC).
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        kst = timezone(timedelta(hours=9))
        row: dict[str, JsonValue] = {
            "id": "1",
            "tz": datetime(2025, 1, 1, 21, 30, 0, tzinfo=kst),  # type: ignore[dict-item]
        }
        client = _FakeClient({"datago.air_quality": [row]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        body = self._post_preview(service, VALID_SPEC_YAML, limit=1)

        preview = cast(dict[str, object], cast(list[object], body["previews"])[0])
        source_row = cast(dict[str, object], cast(list[object], preview["source_sample"])[0])
        transformed_row = cast(dict[str, object], cast(list[object], preview["sample"])[0])
        # source_sample exposes bronze raw record as-is, preserving original KST offset
        # Preserved; Silver (transformed) normalized by polars to UTC — KST 21:30 and
        # UTC 12:30 is same instant so no diff (values equal), but representations differ
        # Verify each is ISO 8601 with its own offset (#735).
        assert source_row["tz"] == "2025-01-01T21:30:00+09:00"
        assert transformed_row["tz"] == "2025-01-01T12:30:00+00:00"

    def test_diff_before_after_carry_wire_correct_types(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When actual diff item is created by declared cast, before(str)/after(int)
        # Each in its actual JSON type on wire (same shape as #497 diff item examples).
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        spec_yaml = (
            """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
    schema:
      casts:
        v: int
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
            + "\n"
        )
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": "128000"}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        body = self._post_preview(service, spec_yaml, limit=1)

        preview = cast(dict[str, object], cast(list[object], body["previews"])[0])
        diffs = cast(list[dict[str, object]], preview["diffs"])
        assert len(diffs) == 1
        assert diffs[0]["before"] == "128000"
        assert diffs[0]["after"] == 128000
        assert diffs[0]["transform"] == "cast:int"


class TestHttpAdapter:
    def test_unsafe_run_id_returns_400_not_500(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Unsafe path run_id must return 400 from adapter, not 500/disconnect (#200).
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "../bad"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 400

    def test_malformed_json_body_returns_400(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/validate",
            data=b"not-json{{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 400
        body = cast(dict[str, object], json.loads(exc_info.value.read()))
        assert "invalid JSON body" in str(body.get("error", ""))

    def test_unknown_path_returns_404(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        base_url, _, _ = http_server
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/nope", timeout=2.0)
        assert exc_info.value.code == 404

    def test_non_object_json_body_returns_400(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Valid but non-object JSON (scalar) must return 400, not TypeError (#183).
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/validate",
            data=b"1",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 400
        body = cast(dict[str, object], json.loads(exc_info.value.read()))
        assert "object" in str(body.get("error", ""))

    def test_query_string_is_ignored_in_routing(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Even with query string, routing uses only path component (#184).
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/validate?x=1",
            data=json.dumps({"spec": VALID_SPEC_YAML}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200

    def test_query_string_does_not_corrupt_run_id(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Query params like ?download=1 must not leak into run_id (#184).
        base_url, _, _ = http_server
        build_req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "run1"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(build_req, timeout=2.0) as response:
            assert response.status == 200
        with urllib.request.urlopen(f"{base_url}/artifacts/run1?download=1", timeout=2.0) as resp:
            assert resp.status == 200
            body = cast(dict[str, object], json.loads(resp.read()))
        assert body["run_id"] == "run1"

    def test_oversized_body_returns_413(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # If declared Content-Length exceeds limit, reject with 413 without reading (#186).
        import http.client

        base_url, _, _ = http_server
        host_port = base_url.removeprefix("http://")
        host, port = host_port.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=2.0)
        try:
            conn.putrequest("POST", "/validate")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(100 * 1024 * 1024))
            conn.endheaders()  # No body sent — handler rejects on headers alone.
            response = conn.getresponse()
            assert response.status == 413
        finally:
            conn.close()

    def test_valid_post_validate_round_trips(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Verify adapter passes normal request to dispatch and serializes JSON response.
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/validate",
            data=json.dumps({"spec": VALID_SPEC_YAML}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200
            body = cast(dict[str, object], json.loads(response.read()))
        assert body["status"] == "valid"

    def test_options_preflight_returns_204_with_cors(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # CORS preflight (OPTIONS) must return 204 + allow headers (#254).
        base_url, _, _ = http_server
        req = urllib.request.Request(f"{base_url}/build", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert response.headers["Access-Control-Allow-Methods"] == (
                "GET, POST, PUT, DELETE, OPTIONS"
            )
            assert (
                response.headers["Access-Control-Allow-Headers"]
                == "Content-Type, X-API-Key, Authorization, X-Provider-Key, X-Publish-Credential"
            )
            assert response.headers["Access-Control-Max-Age"] == "86400"

    def test_response_includes_cors_header(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Same-origin requests (no Origin header) must include CORS headers (#322).
        base_url, _, _ = http_server
        with urllib.request.urlopen(f"{base_url}/version", timeout=2.0) as response:
            # If same-origin, return `*`.
            assert response.headers["Access-Control-Allow-Origin"] == "*"

    def test_cors_responses_always_vary_on_origin(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # CORS headers vary by Origin, so Vary: Origin always needed —
        # Without it, caching proxy could reuse one origin's response for another.
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server

        allowed = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(allowed, timeout=2.0) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
            assert response.headers["Vary"] == "Origin"

        # Rejected origin responses must include Vary to prevent cache mixing.
        denied = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://evil.example"}
        )
        with urllib.request.urlopen(denied, timeout=2.0) as response:
            assert "Access-Control-Allow-Origin" not in response.headers
            assert response.headers["Vary"] == "Origin"

        # Preflight is same.
        preflight = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://evil.example"}, method="OPTIONS"
        )
        with urllib.request.urlopen(preflight, timeout=2.0) as response:
            assert response.status == 204
            assert response.headers["Vary"] == "Origin"

    def test_cors_default_denied_when_no_env(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Cross-origin requests with env unset must have no CORS headers (#322 default-deny).
        # Clear env explicitly and test
        monkeypatch.delenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", raising=False)
        base_url, _, _ = http_server
        # Request with Origin header (treated as cross-origin)
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            # default-deny so no CORS headers
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_cors_file_download_respects_allowlist(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # File responses (_write_file) must also follow CORS allowlist (#382).
        # Previously _write_file didn't pass Origin, treated as same-origin
        # Sent Access-Control-Allow-Origin: * even to unapproved origins.
        monkeypatch.delenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", raising=False)
        base_url, _, _ = http_server
        build_req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "run1"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(build_req, timeout=5.0) as response:
            assert response.status == 200
        # Cross-origin file download request
        req = urllib.request.Request(
            f"{base_url}/artifacts/run1/manifest.json",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200
            # default-deny: origins not in allowlist get no CORS headers
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_cors_file_download_allowed_origin_echoed(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # File download from approved origin must echo that origin (#382).
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server
        build_req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "run1"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(build_req, timeout=5.0) as response:
            assert response.status == 200
        req = urllib.request.Request(
            f"{base_url}/artifacts/run1/manifest.json",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"

    def test_cors_allowed_origins_configurable(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Must allow setting allowed Origins via KPUBDATA_BUILDER_ALLOWED_ORIGINS (#322).
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server
        # Request with Origin header
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"

    def test_cors_exposes_the_headers_a_page_has_to_read(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Without Expose-Headers a cross-origin page cannot read a download's file name,
        # the request id, or Retry-After (#995).
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            exposed = {
                name.strip()
                for name in response.headers["Access-Control-Expose-Headers"].split(",")
            }
            assert {"Content-Disposition", "X-Request-ID", "Retry-After"} <= exposed
            assert response.headers["X-Request-ID"]

    def test_cors_exposes_nothing_to_a_disallowed_origin(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server
        req = urllib.request.Request(f"{base_url}/version", headers={"Origin": "https://evil.test"})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.headers["Access-Control-Expose-Headers"] is None

    def test_cors_multiple_origins_configurable(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Must allow comma-separated Origins (#322).
        monkeypatch.setenv(
            "KPUBDATA_BUILDER_ALLOWED_ORIGINS",
            "http://localhost:5173,https://studio.example.com",
        )
        base_url, _, _ = http_server
        # Request from first origin
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
        # Request from second origin
        req = urllib.request.Request(
            f"{base_url}/version", headers={"Origin": "https://studio.example.com"}
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "https://studio.example.com"

    def test_cors_rejects_disallowed_origin(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Origin not in allowlist must get no CORS headers (#322).
        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "http://localhost:5173")
        base_url, _, _ = http_server
        # Request from unapproved origin
        req = urllib.request.Request(f"{base_url}/version", headers={"Origin": "http://evil.com"})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            # Unapproved origin so no CORS headers
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_missing_api_key_returns_401_when_configured(
        self,
        http_server_with_auth: tuple[str, HTTPServer, threading.Thread],
    ) -> None:
        # Adapter must pass X-API-Key header to dispatch (#248).
        base_url, _, _ = http_server_with_auth
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/version", timeout=2.0)
        assert exc_info.value.code == 401

    def test_valid_api_key_header_is_accepted(
        self,
        http_server_with_auth: tuple[str, HTTPServer, threading.Thread],
    ) -> None:
        # http_server_with_auth fixture already sets API key, no monkeypatch needed
        base_url, _, _ = http_server_with_auth
        req = urllib.request.Request(f"{base_url}/version", headers={"X-API-Key": "secret"})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200

    def test_healthz_accessible_without_api_key(
        self,
        http_server_with_auth: tuple[str, HTTPServer, threading.Thread],
    ) -> None:
        # /healthz is exposed unauthenticated outside auth gate (#372).
        # Probe can't carry credentials, so return only 200 + {"status":"ok"} without key.
        base_url, _, _ = http_server_with_auth
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=2.0) as response:
            assert response.status == 200
            request_id = response.headers["X-Request-ID"]
            body = cast(dict[str, object], json.loads(response.read()))
        assert body["status"] == "ok"
        assert "request_id" not in body
        assert request_id
        # Version/service metadata must not leak.
        assert "api_version" not in body
        assert "service" not in body

    @pytest.mark.parametrize("stage", ["bronze", "silver", "gold"])
    def test_stage_detail_wire_response_conforms_to_openapi(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        stage: str,
    ) -> None:
        base_url, _, _ = http_server
        build_req = urllib.request.Request(
            f"{base_url}/build",
            data=json.dumps({"spec": VALID_SPEC_YAML, "run_id": "wire-stage-detail"}).encode(
                "utf-8"
            ),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(build_req, timeout=5.0) as response:
            assert response.status == 200

        url = f"{base_url}/builds/wire-stage-detail/stages/{stage}?source=datago.air_quality"
        with urllib.request.urlopen(url, timeout=2.0) as response:
            assert response.status == 200
            status_code = response.status
            request_id = response.headers["X-Request-ID"]
            body = cast(dict[str, object], json.loads(response.read()))

        assert "request_id" not in body
        assert request_id
        contract_path = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
        contract = cast(dict[str, Any], yaml.safe_load(contract_path.read_text(encoding="utf-8")))
        schema = response_schema(contract, "/builds/{run_id}/stages/{stage}", "GET", status_code)
        assert schema is not None
        assert validate(body, schema, contract) == []


class TestHttpUploads:
    """POST /uploads (#498) real socket roundtrip — binary body send·query parse·limit."""

    def test_create_get_delete_upload_round_trip_over_http(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/uploads?format=csv&filename=trades.csv",
            data=b"id,amount\n1,1000\n",
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200
            created = cast(dict[str, object], json.loads(response.read()))
        upload_id = created["upload_id"]
        assert isinstance(upload_id, str) and upload_id.startswith("upl_")
        assert created["original_filename"] == "trades.csv"

        with urllib.request.urlopen(f"{base_url}/uploads/{upload_id}", timeout=2.0) as response:
            assert response.status == 200
            fetched = cast(dict[str, object], json.loads(response.read()))
        assert fetched["upload_id"] == upload_id
        assert "content" not in fetched

        delete_req = urllib.request.Request(f"{base_url}/uploads/{upload_id}", method="DELETE")
        with urllib.request.urlopen(delete_req, timeout=2.0) as response:
            assert response.status == 200
            deleted = cast(dict[str, object], json.loads(response.read()))
        assert deleted["upload_id"] == upload_id
        assert deleted["deleted"] is True

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/uploads/{upload_id}", timeout=2.0)
        assert exc_info.value.code == 404

    def test_create_upload_over_configured_limit_returns_413(
        self,
        http_server: tuple[str, HTTPServer, threading.Thread],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_MAX_UPLOAD_BYTES", "10")
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/uploads?format=csv",
            data=b"a,b\n" * 10,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 413

    def test_create_upload_missing_format_returns_400(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/uploads",
            data=b"id,amount\n1,1000\n",
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 400

    def test_create_upload_body_is_not_parsed_as_json(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # POST /uploads body accepts CSV/binary as-is — unlike other endpoints
        # Otherwise no JSON parse attempt (#498). This body is invalid JSON but
        # (raw text) should succeed as format=csv since it's valid CSV.
        base_url, _, _ = http_server
        req = urllib.request.Request(
            f"{base_url}/uploads?format=csv",
            data=b"a,b\n1,2\n",
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            assert response.status == 200


class TestHttpRobustness:
    """#218 (JSON 500 handler) and #219 (DoS hardening) verification."""

    def test_dispatch_exception_returns_json_500(
        self, tmp_path: Path, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # Even if dispatch() raises, don't disconnect; return JSON 500 (#218).
        # Patch dispatch to artificially raise exception.
        import unittest.mock

        base_url, _, _ = http_server

        with unittest.mock.patch(
            "kpubdata_builder.service.http.dispatch",
            side_effect=RuntimeError("boom"),
        ):
            req = urllib.request.Request(
                f"{base_url}/validate",
                data=json.dumps({"spec": VALID_SPEC_YAML}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=2.0)
        assert exc_info.value.code == 500
        body = cast(dict[str, object], json.loads(exc_info.value.read()))
        assert body.get("error") == "internal server error"
        # Internal exception message ("boom") must not leak to client.
        assert "boom" not in json.dumps(body)

    def test_make_handler_has_socket_timeout(self, tmp_path: Path) -> None:
        # Handler class must have timeout set so slow clients don't starve threads
        # (#219). BaseHTTPRequestHandler.timeout being None means unlimited.
        from kpubdata_builder.service.http import _SOCKET_TIMEOUT_SECONDS, make_handler

        handler_cls = make_handler(_service(tmp_path))
        assert handler_cls.timeout is not None
        assert handler_cls.timeout == _SOCKET_TIMEOUT_SECONDS
        assert handler_cls.timeout > 0

    def test_serve_uses_bounded_threading_http_server(self, tmp_path: Path) -> None:
        # serve() must use BoundedThreadingHTTPServer so slow clients don't starve server
        # Without stopping all (#219), concurrent threads capped (#253).
        import contextlib
        import unittest.mock
        from http.server import ThreadingHTTPServer

        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, serve

        created_servers: list[object] = []
        original_init = ThreadingHTTPServer.__init__

        def capturing_init(self: object, *args: object, **kwargs: object) -> None:
            created_servers.append(self)
            original_init(self, *args, **kwargs)  # type: ignore[misc]

        with (
            unittest.mock.patch.object(ThreadingHTTPServer, "__init__", capturing_init),
            unittest.mock.patch.object(
                ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt
            ),
            unittest.mock.patch.object(ThreadingHTTPServer, "server_close"),
            contextlib.suppress(KeyboardInterrupt),
        ):
            serve(_service(tmp_path), host="127.0.0.1", port=0)

        assert len(created_servers) == 1
        assert isinstance(created_servers[0], BoundedThreadingHTTPServer)

    def test_serve_passes_max_workers_to_executor(self, tmp_path: Path) -> None:
        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler

        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(_service(tmp_path)), max_workers=3
        )
        try:
            assert server._executor._max_workers == 3
        finally:
            server.server_close()

    def test_bounded_server_limits_concurrent_processing(self, tmp_path: Path) -> None:
        # Concurrent threads must be capped at max_workers (#253): more than workers
        # Many simultaneous clients but actual throughput never exceeds max_workers.
        import time
        import unittest.mock

        from kpubdata_builder.service.app import ServiceResponse
        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler

        max_workers = 2
        num_clients = 4
        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(_service(tmp_path)), max_workers=max_workers
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"

        lock = threading.Lock()
        concurrent_count = 0
        max_seen = 0
        release = threading.Event()

        def _slow_dispatch(*args: object, **kwargs: object) -> ServiceResponse:
            nonlocal concurrent_count, max_seen
            with lock:
                concurrent_count += 1
                max_seen = max(max_seen, concurrent_count)
            release.wait(timeout=5.0)
            with lock:
                concurrent_count -= 1
            return ServiceResponse(200, {"service": "kpubdata-builder", "api_version": "1.0.0"})

        results: list[int] = []

        def _get() -> None:
            with urllib.request.urlopen(f"{base_url}/version", timeout=5.0) as resp:
                results.append(resp.status)

        try:
            with unittest.mock.patch(
                "kpubdata_builder.service.http.dispatch", side_effect=_slow_dispatch
            ):
                client_threads = [threading.Thread(target=_get) for _ in range(num_clients)]
                for t in client_threads:
                    t.start()

                # Actively wait until worker pool fills to limit (max_workers).
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with lock:
                        if concurrent_count >= max_workers:
                            break
                    time.sleep(0.02)

                with lock:
                    observed = max_seen

                release.set()
                for t in client_threads:
                    t.join(timeout=5.0)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1.0)

        assert observed == max_workers
        assert len(results) == num_clients

    def test_bounded_server_rejects_connections_past_the_pending_limit(
        self, tmp_path: Path
    ) -> None:
        """Limiting threads alone leaves queue unbounded.

        ``ThreadPoolExecutor`` task queue unbounded; after workers fill
        Incoming connections stacked with socket open — even 10 threads
        File descriptors grew with connections.
        """
        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler

        class _FakeSocket:
            def __init__(self) -> None:
                self.sent = b""
                self.closed = False

            def sendall(self, data: bytes) -> None:
                self.sent += data

            def shutdown(self, how: int) -> None:
                return

            def close(self) -> None:
                self.closed = True

        # 1 processing + 1 waiting = accept up to 2 connections.
        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(_service(tmp_path)),
            max_workers=1,
            max_pending_requests=1,
        )
        started = threading.Event()
        release = threading.Event()

        def _blocking(request: object, client_address: object) -> None:
            started.set()
            release.wait(timeout=5.0)

        server.process_request_thread = _blocking  # type: ignore[method-assign]

        running, pending, rejected = _FakeSocket(), _FakeSocket(), _FakeSocket()
        try:
            server.process_request(running, ("10.0.0.1", 1))  # type: ignore[arg-type]
            assert started.wait(timeout=5.0)
            server.process_request(pending, ("10.0.0.2", 2))  # type: ignore[arg-type]
            server.process_request(rejected, ("10.0.0.3", 3))  # type: ignore[arg-type]

            # Two accepted but no response yet — handler still processing.
            assert running.sent == b""
            assert pending.sent == b""
            # Third gets immediate 503 and disconnects.
            assert rejected.sent.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
            assert b"Retry-After: 1" in rejected.sent
            assert b"Connection: close" in rejected.sent
            assert rejected.closed
        finally:
            release.set()
            server.server_close()

    def test_bounded_server_admits_again_once_requests_drain(self, tmp_path: Path) -> None:
        """Without resetting reject counter, server hangs once then closes permanently."""
        import time

        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler

        class _FakeSocket:
            def __init__(self) -> None:
                self.sent = b""

            def sendall(self, data: bytes) -> None:
                self.sent += data

            def shutdown(self, how: int) -> None:
                return

            def close(self) -> None:
                return

        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(_service(tmp_path)),
            max_workers=1,
            max_pending_requests=0,
        )
        server.process_request_thread = lambda *_a: None  # type: ignore[method-assign]
        try:
            for _ in range(5):
                sock = _FakeSocket()
                deadline = time.monotonic() + 5.0
                while server._inflight > 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                server.process_request(sock, ("10.0.0.1", 1))  # type: ignore[arg-type]
                assert sock.sent == b"", "drain 된 뒤에는 다시 받아야 한다"
        finally:
            server.server_close()

    def test_overloaded_response_is_a_well_formed_http_message(self) -> None:
        """Responses written directly to socket bypass handler, no one catches format errors."""
        from kpubdata_builder.service.http import _OVERLOADED_RESPONSE

        head, _, body = _OVERLOADED_RESPONSE.partition(b"\r\n\r\n")
        headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:])
        assert int(headers[b"Content-Length"]) == len(body)
        assert json.loads(body) == {"error": "server overloaded", "code": "server_overloaded"}

    @staticmethod
    def _overloaded_headers(allowed: frozenset[str]) -> dict[bytes, bytes]:
        from kpubdata_builder.service.http import _overloaded_response

        head, _, body = _overloaded_response(allowed).partition(b"\r\n\r\n")
        headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:])
        assert int(headers[b"Content-Length"]) == len(body)
        assert json.loads(body) == {"error": "server overloaded", "code": "server_overloaded"}
        return headers

    def test_overloaded_response_has_no_cors_header_without_an_allowed_origin(self) -> None:
        """Default-deny stays default-deny (#995)."""
        headers = self._overloaded_headers(frozenset())

        assert not [name for name in headers if name.startswith(b"Access-Control-")]

    def test_overloaded_response_names_the_one_allowed_origin(self) -> None:
        """The request is not read, so the only origin it could be for is the one allowed."""
        headers = self._overloaded_headers(frozenset({"https://studio.example.com"}))

        assert headers[b"Access-Control-Allow-Origin"] == b"https://studio.example.com"
        assert headers[b"Access-Control-Allow-Credentials"] == b"true"
        assert headers[b"Vary"] == b"Origin"
        assert headers[b"Access-Control-Expose-Headers"] == b"Retry-After"
        assert headers[b"Retry-After"] == b"1"

    def test_overloaded_response_is_readable_from_any_of_several_origins(self) -> None:
        headers = self._overloaded_headers(
            frozenset({"http://localhost:5173", "https://studio.example.com"})
        )

        assert headers[b"Access-Control-Allow-Origin"] == b"*"
        assert b"Access-Control-Allow-Credentials" not in headers
        assert headers[b"Access-Control-Expose-Headers"] == b"Retry-After"

    def test_overloaded_response_drops_cors_for_an_origin_it_cannot_write(self) -> None:
        headers = self._overloaded_headers(frozenset({"https://a.example\r\nX-Injected: 1"}))

        assert not [name for name in headers if name.startswith(b"Access-Control-")]
        assert b"X-Injected" not in headers

    def test_a_rejected_connection_gets_the_cors_headers_of_the_deployment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The socket path uses the configured origins, not the constant without them."""
        from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler

        monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "https://studio.example.com")
        sent: list[bytes] = []

        class _Socket:
            def sendall(self, data: bytes) -> None:
                sent.append(data)

            def close(self) -> None:
                return None

            def shutdown(self, _how: int) -> None:
                return None

        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(_service(tmp_path)), max_workers=1
        )
        try:
            server._reject(_Socket(), ("10.0.0.1", 1))  # type: ignore[arg-type]
        finally:
            server.server_close()

        assert b"Access-Control-Allow-Origin: https://studio.example.com\r\n" in sent[0]

    def test_oversized_body_content_length_returns_413_http(
        self, http_server: tuple[str, HTTPServer, threading.Thread]
    ) -> None:
        # If Content-Length > _MAX_BODY_BYTES, reject with 413 without reading (#219).
        import http.client

        base_url, _, _ = http_server
        host_port = base_url.removeprefix("http://")
        host, port = host_port.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=2.0)
        try:
            conn.putrequest("POST", "/validate")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(20 * 1024 * 1024))  # 20 MiB > 10 MiB limit
            conn.endheaders()  # No body sent — handler rejects on headers alone.
            response = conn.getresponse()
            assert response.status == 413
            resp_body = cast(dict[str, object], json.loads(response.read()))
            assert "too large" in str(resp_body.get("error", ""))
        finally:
            conn.close()

    def test_body_read_timeout_returns_json_400(self, tmp_path: Path) -> None:
        # If rfile.read() raises TimeoutError, return JSON 400, not disconnect (#219).
        import io

        handler_cls = make_handler(_service(tmp_path))

        captured: list[tuple[int, dict[str, object]]] = []

        class _PatchedHandler(handler_cls):  # type: ignore[valid-type]
            def _write(self, status_code: int, body: dict[str, object]) -> None:  # type: ignore[override]
                captured.append((status_code, body))

        slow_rfile = io.BytesIO(b"")

        def _timeout_read(n: int) -> bytes:
            raise TimeoutError("timed out")

        slow_rfile.read = _timeout_read  # type: ignore[method-assign]

        h = object.__new__(_PatchedHandler)
        h.rfile = slow_rfile
        h.headers = {"Content-Length": "10"}  # type: ignore[assignment]
        h._dispatch("POST")

        assert len(captured) == 1
        status, body = captured[0]
        assert status == 400
        assert "timed out" in str(body.get("error", ""))

    def test_truncated_body_returns_json_400(self, tmp_path: Path) -> None:
        # Body shorter than Content-Length (EOF) must return JSON 400, not disconnect (#219).
        import io

        handler_cls = make_handler(_service(tmp_path))

        captured: list[tuple[int, dict[str, object]]] = []

        class _PatchedHandler(handler_cls):  # type: ignore[valid-type]
            def _write(self, status_code: int, body: dict[str, object]) -> None:  # type: ignore[override]
                captured.append((status_code, body))

        # Content-Length is 10 but only 5 bytes actually sent.
        truncated_rfile = io.BytesIO(b"hello")

        h = object.__new__(_PatchedHandler)
        h.rfile = truncated_rfile
        h.headers = {"Content-Length": "10"}  # type: ignore[assignment]
        h._dispatch("POST")

        assert len(captured) == 1
        status, body = captured[0]
        assert status == 400
        assert "incomplete" in str(body.get("error", ""))


class TestArtifactFileServing:
    """Test artifact file serving functionality (#323)."""

    def test_serves_existing_file(self, tmp_path: Path) -> None:
        from kpubdata_builder.service import FileResponse

        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "manifest.json")
        assert isinstance(resp, FileResponse)
        assert resp.status_code == 200
        assert resp.filename == "manifest.json"
        assert resp.file_path.exists()

    def test_returns_404_for_nonexistent_file(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "nonexistent.csv")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 404
        assert "not found" in str(resp.body.get("error", ""))

    def test_returns_404_for_nonexistent_run(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        resp = service.serve_artifact_file("nope", "manifest.json")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 404
        assert "run not found" in str(resp.body.get("error", ""))

    def test_blocks_path_traversal_with_dot_dot(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "../run2/manifest.json")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 400
        assert "safe" in str(resp.body.get("error", "")).lower()

    def test_blocks_path_traversal_with_absolute_path(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "/etc/passwd")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 400
        assert "safe" in str(resp.body.get("error", "")).lower()

    def test_returns_400_for_directory_not_file(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        # out directory exists since build created it
        (tmp_path / "run1" / "subdir").mkdir()

        resp = service.serve_artifact_file("run1", "subdir")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 400
        assert "not a file" in str(resp.body.get("error", ""))

    def test_serve_artifact_file_route(self, tmp_path: Path) -> None:
        from kpubdata_builder.service import FileResponse

        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = dispatch(service, "GET", "/artifacts/run1/manifest.json", None)
        assert isinstance(resp, FileResponse)
        assert resp.status_code == 200
        assert resp.filename == "manifest.json"

    def test_serves_nested_relative_path(self, tmp_path: Path) -> None:
        """GET /artifacts/{run_id} returns run directory relative paths (with slashes)
        serve_artifact_file must also receive as-is (#323 follow-up). Previously entire file_path
        validated as single segment, so 'silver/air/table.parquet' entirely 400."""
        from kpubdata_builder.service import FileResponse

        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        nested = tmp_path / "run1" / "silver" / "air" / "table.parquet"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_bytes(b"PAR1nested")

        listed = service.artifacts("run1")
        assert isinstance(listed, ServiceResponse)
        # wire list must always use POSIX "/" (no OS separator or output_root prefix).
        for wire_path in listed.body["files"]:
            assert "\\" not in wire_path
            assert not wire_path.startswith("/")
            assert str(tmp_path) not in wire_path
        assert "silver/air/table.parquet" in set(listed.body["files"])

        resp = service.serve_artifact_file("run1", "silver/air/table.parquet")
        assert isinstance(resp, FileResponse)
        assert resp.status_code == 200
        assert resp.filename == "table.parquet"
        assert resp.file_path.read_bytes() == b"PAR1nested"

        route_resp = dispatch(service, "GET", "/artifacts/run1/silver/air/table.parquet", None)
        assert isinstance(route_resp, FileResponse)
        assert route_resp.status_code == 200

    def test_blocks_backslash_in_nested_path(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        resp = service.serve_artifact_file("run1", "silver\\..\\..\\secret.txt")
        assert isinstance(resp, ServiceResponse)
        assert resp.status_code == 400

    def test_blocks_percent_encoded_traversal(self, tmp_path: Path) -> None:
        """Percent-encoded traversal/delimiters decoded then re-validated and blocked."""
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run2"})

        for encoded in (
            "%2e%2e/run2/manifest.json",  # ../run2/manifest.json
            "silver%2f..%2f..%2fmanifest.json",  # silver/../../manifest.json
            "silver%5c..%5c..%5cmanifest.json",  # silver\..\..\manifest.json
            "%252e%252e/run2/manifest.json",  # double-encoded ..
            "..%2f..%2fetc%2fpasswd",
        ):
            resp = service.serve_artifact_file("run1", encoded)
            assert isinstance(resp, ServiceResponse), encoded
            assert resp.status_code == 400, encoded

    def test_blocks_cross_run_and_double_slash(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run2"})

        # Other run files: only accessible via '..', so blocked.
        assert service.serve_artifact_file("run1", "../run2/manifest.json").status_code == 400
        # double slash → empty component
        assert service.serve_artifact_file("run1", "silver//table.parquet").status_code == 400

    def test_mime_type_detection(self, tmp_path: Path) -> None:
        from kpubdata_builder.service.http import _get_mime_type

        # Explicit mapping
        assert _get_mime_type(tmp_path / "data.parquet") == "application/vnd.apache.parquet"
        assert _get_mime_type(tmp_path / "data.csv") == "text/csv"
        assert _get_mime_type(tmp_path / "data.json") == "application/json"
        assert _get_mime_type(tmp_path / "data.txt") == "text/plain"

        # mimetypes library (fallback)
        assert _get_mime_type(tmp_path / "data.html") == "text/html"
        assert _get_mime_type(tmp_path / "data.xml") == "application/xml"

        # Unknown extension → default
        assert _get_mime_type(tmp_path / "data.unknown") == "application/octet-stream"
        assert _get_mime_type(tmp_path / "data") == "application/octet-stream"


class TestOwnershipEnforcement:
    """ENFORCE_OWNERSHIP regression: ownership must be enforced even in index fallback (#433).

    Ownership filter only in SQLite index branch of list_builds, not filesystem fallback
    Regression test: even with ENFORCE_OWNERSHIP=true, other's run_id exposed bug.
    ADR 0003 designed fallback as normal mode, not exception case.
    """

    def _build_as(self, service: BuilderService, run_id: str, created_by: str) -> None:
        """Build records created_by explicitly (injected for test simplicity)."""
        self._build_with_manifest_fields(service, run_id, created_by=created_by)

    def _build_with_manifest_fields(
        self, service: BuilderService, run_id: str, **fields: object
    ) -> None:
        """After build, overwrite arbitrary manifest.json fields (created_by/owner_id etc) (#505).

        Setting owner_id to None completely removes key from manifest
        legacy(#505 pre-) mimic manifest shape.
        """
        dispatch(
            service,
            "POST",
            "/build",
            {"spec": VALID_SPEC_YAML, "run_id": run_id},
        )
        mpath = service._output_root / run_id / "manifest.json"
        data = json.loads(mpath.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        mpath.write_text(json.dumps(data), encoding="utf-8")

    def test_fallback_filters_other_owners_when_index_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When index empty, fallback must not expose other user's runs (#433)."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "runA", "oidc:userA")
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        user_b = Principal(kind="oidc", identifier="userB")
        resp = service.list_builds(principal=user_b)

        assert resp.status_code == 200
        builds = cast(list[dict[str, object]], resp.body["builds"])
        run_ids = [cast(str, b["run_id"]) for b in builds]
        assert "runA" not in run_ids, (
            "폴백 경로가 다른 사용자의 run_id를 노출함 — 소유권 필터 누락 (#433)"
        )

    def test_fallback_filters_other_owners_when_index_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if index lookup fails with exception, fallback must enforce ownership (#433).

        When list_builds raises due to SQLite lock contention etc., ENFORCE_OWNERSHIP+
        If oidc combo, other's run must not leak via fallback (fail-closed).
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "runA", "oidc:userA")

        def _raise(_limit: int) -> list:
            raise RuntimeError("simulated sqlite lock contention")

        monkeypatch.setattr(service._build_index, "list_builds", _raise)

        user_b = Principal(kind="oidc", identifier="userB")
        resp = service.list_builds(principal=user_b)

        assert resp.status_code == 200
        builds = cast(list[dict[str, object]], resp.body["builds"])
        run_ids = [cast(str, b["run_id"]) for b in builds]
        assert "runA" not in run_ids

    def test_owner_sees_own_run_in_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Owner can see own runs even in fallback (positive regression)."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "runA", "oidc:userA")
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        user_a = Principal(kind="oidc", identifier="userA")
        resp = service.list_builds(principal=user_a)

        builds = cast(list[dict[str, object]], resp.body["builds"])
        run_ids = [cast(str, b["run_id"]) for b in builds]
        assert "runA" in run_ids


def _build_run_ids(resp: ServiceResponse) -> list[str]:
    builds = cast(list[dict[str, object]], resp.body["builds"])
    return [cast(str, b["run_id"]) for b in builds]


class TestStableOwnerIdOwnership:
    """Ownership judgment based on canonical owner_id (#505) — /builds list/detail."""

    def _build_with_manifest_fields(
        self, service: BuilderService, run_id: str, **fields: object
    ) -> None:
        dispatch(
            service,
            "POST",
            "/build",
            {"spec": VALID_SPEC_YAML, "run_id": run_id},
        )
        mpath = service._output_root / run_id / "manifest.json"
        data = json.loads(mpath.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        mpath.write_text(json.dumps(data), encoding="utf-8")

    def test_owner_id_match_wins_even_with_different_display_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If owner_id matches, owner recognized even if display label (created_by) differs (#505).

        Even if display identity changes (future profile name updates etc), persistent owner
        Verify at response list level that identity completion condition holds.
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(
            service, "runA", created_by="oidc:old-display-name", owner_id="oidc:canonical-abc"
        )
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        # label differs from manifest created_by, but owner_id is same — still owner.
        renamed_principal = Principal(
            kind="oidc", identifier="new-display-name", owner_id="oidc:canonical-abc"
        )
        resp = service.list_builds(principal=renamed_principal)
        run_ids = _build_run_ids(resp)
        assert "runA" in run_ids

    def test_owner_id_mismatch_denied_even_with_matching_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """New records with owner_id reject if owner_id mismatch even with same label (#505).

        Even when label happens same due to legacy truncation (first 8 chars before sub) collision
        canonical owner_id takes priority so ownership doesn't mix.
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(
            service, "runA", created_by="oidc:userA", owner_id="oidc:canonical-real-owner"
        )
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        # label (identifier) is "userA", same as original owner, but owner_id differs.
        impostor = Principal(kind="oidc", identifier="userA", owner_id="oidc:different-owner")
        resp = service.list_builds(principal=impostor)
        run_ids = _build_run_ids(resp)
        assert "runA" not in run_ids

    def test_legacy_run_without_owner_id_falls_back_to_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runs without owner_id (#505 pre-) remain accessible via created_by/label."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(service, "runA", created_by="oidc:userA", owner_id=None)
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        # Authenticated principal without owner_id (e.g. legacy) accesses own runs via label.
        user_a = Principal(kind="oidc", identifier="userA")
        resp = service.list_builds(principal=user_a)
        run_ids = _build_run_ids(resp)
        assert "runA" in run_ids

    def test_ambiguous_record_with_no_owner_info_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Records with neither owner_id nor created_by not treated as "anyone accessible".

        Regression test for requirement: forbid "no owner field so anyone can access" fallback.
        """
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(service, "runA", created_by=None, owner_id=None)
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        any_user = Principal(kind="oidc", identifier="userA", owner_id="oidc:canonical-abc")
        resp = service.list_builds(principal=any_user)
        run_ids = _build_run_ids(resp)
        assert "runA" not in run_ids

    def test_builds_response_does_not_leak_owner_id_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """owner_id is internal to ownership judgment; not in /builds wire (#505)."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_with_manifest_fields(
            service, "runA", created_by="oidc:userA", owner_id="oidc:canonical-abc"
        )
        monkeypatch.setattr(service._build_index, "list_builds", lambda limit: [])

        user_a = Principal(kind="oidc", identifier="userA", owner_id="oidc:canonical-abc")
        resp = service.list_builds(principal=user_a)
        builds = cast(list[dict[str, object]], resp.body["builds"])
        assert builds
        assert "owner_id" not in builds[0]


class _FakeCatalogRef:
    """Mimics DatasetRef — exposes only catalog() accessible attributes.

    Defaults same as real DatasetRef (dataset without metadata — #490
    null/empty serialization rule verification used as-is).
    """

    def __init__(
        self,
        provider: str,
        dataset_key: str,
        name: str,
        *,
        service_key: bool = False,
        description: str | None = None,
        tags: tuple[str, ...] = (),
        source_url: str | None = None,
        representation: object = None,
        operations: frozenset[object] = frozenset(),
        query_support: object = None,
        raw_metadata_extra: dict[str, object] | None = None,
        license: object = None,  # noqa: A002 - mirrors DatasetRef.license
    ) -> None:
        from kpubdata.core.models import Representation

        self.provider = provider
        self.dataset_key = dataset_key
        self.name = name
        self.description = description
        self.tags = tags
        self.source_url = source_url
        self.representation = representation or Representation.API_JSON
        self.operations = operations
        self.query_support = query_support
        self.raw_metadata: dict[str, object] = (
            {"service_key_param": "serviceKey"} if service_key else {}
        )
        if raw_metadata_extra:
            self.raw_metadata.update(raw_metadata_extra)
        if license is not None:
            self.license = license


class TestCatalog:
    """Dynamic catalog provider lookup (#436). ADR 0011 — no hardcoding."""

    def _service_with_catalog(
        self,
        tmp_path: Path,
        refs: list[object],
        *,
        auth_provider_names: tuple[str, ...] = (),
    ) -> BuilderService:
        client = _FakeClient({}, catalog_items=refs, auth_provider_names=auth_provider_names)
        return BuilderService(output_root=tmp_path, client_factory=lambda: client)

    def _service_with_lazy_provider_catalog(
        self,
        tmp_path: Path,
        *,
        missing_provider: str | None = None,
        missing_module: str = "pandas",
    ) -> BuilderService:
        refs = {
            "datago": [_FakeCatalogRef("datago", "air_quality", "대기오염")],
            "krx": [_FakeCatalogRef("krx", "stock", "주식")],
        }

        class Registry:
            def __iter__(self) -> Iterable[str]:
                return iter(("datago", "krx"))

            def get(self, name: str) -> object:
                if name == missing_provider:
                    raise ModuleNotFoundError(
                        f"No module named '{missing_module}'", name=missing_module
                    )
                return type("Adapter", (), {"requires_api_key": name == "datago"})()

        class Catalog:
            @staticmethod
            def list(*, provider: str) -> list[object]:
                return refs[provider]

        class Client:
            _registry = Registry()
            datasets = Catalog()

            def close(self) -> None:
                return None

        return BuilderService(output_root=tmp_path, client_factory=Client)

    def test_catalog_skips_only_krx_when_optional_pandas_is_missing(self, tmp_path: Path) -> None:
        response = self._service_with_lazy_provider_catalog(
            tmp_path, missing_provider="krx"
        ).catalog()

        assert response.status_code == 200
        providers = cast(list[dict[str, object]], response.body["providers"])
        assert [provider["name"] for provider in providers] == ["datago"]

    def test_catalog_does_not_hide_other_provider_missing_module(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Isolate krx's pandas omission only; other import failures must surface.

        What's missing now logged - not in response body.
        except clause catches any exception from upstream client, but message contains
        request URL which may be mixed in; data.go.kr series sends API key as query param.
        """
        import logging

        with caplog.at_level(logging.ERROR):
            response = self._service_with_lazy_provider_catalog(
                tmp_path, missing_provider="datago", missing_module="internal_datago"
            ).catalog()

        assert response.status_code == 502
        assert response.body == {"error": "catalog unavailable"}
        # Not swallowed — diagnostic info goes to logs.
        assert "internal_datago" in caplog.text

    def test_catalog_keeps_krx_when_optional_dependency_is_available(self, tmp_path: Path) -> None:
        response = self._service_with_lazy_provider_catalog(tmp_path).catalog()

        assert response.status_code == 200
        providers = cast(list[dict[str, object]], response.body["providers"])
        assert [provider["name"] for provider in providers] == ["datago", "krx"]

    def test_catalog_groups_datasets_by_provider(self, tmp_path: Path) -> None:
        refs = [
            _FakeCatalogRef("datago", "air_quality", "대기오염", service_key=True),
            _FakeCatalogRef("datago", "village_fcst", "단기예보"),
            _FakeCatalogRef("bok", "base_rate", "기준금리"),
            _FakeCatalogRef("krx", "stock", "주식"),
        ]
        resp = self._service_with_catalog(tmp_path, refs, auth_provider_names=("bok",)).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        names = [cast(str, p["name"]) for p in providers]
        assert "datago" in names
        assert "bok" in names

        datago = next(p for p in providers if p["name"] == "datago")
        datago_datasets = cast(list[dict[str, object]], datago["datasets"])
        assert len(datago_datasets) == 2
        aq = next(d for d in datago_datasets if d["name"] == "air_quality")
        assert aq["requires_service_key"] is True
        vf = next(d for d in datago_datasets if d["name"] == "village_fcst")
        assert vf["requires_service_key"] is False

        bok = next(p for p in providers if p["name"] == "bok")
        bok_datasets = cast(list[dict[str, object]], bok["datasets"])
        assert bok_datasets[0]["requires_service_key"] is True
        krx = next(p for p in providers if p["name"] == "krx")
        krx_datasets = cast(list[dict[str, object]], krx["datasets"])
        assert krx_datasets[0]["requires_service_key"] is False

    def test_catalog_includes_unlisted_providers(self, tmp_path: Path) -> None:
        """Provider not in hardcoded 8 must appear via dynamic lookup (#436)."""
        refs = [_FakeCatalogRef("newprovider", "new_ds", "새 데이터셋")]
        resp = self._service_with_catalog(tmp_path, refs).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        names = [cast(str, p["name"]) for p in providers]
        assert "newprovider" in names

    def test_catalog_empty_when_no_datasets(self, tmp_path: Path) -> None:
        resp = self._service_with_catalog(tmp_path, []).catalog()
        assert resp.status_code == 200
        assert resp.body["providers"] == []

    def test_catalog_serializes_discovery_metadata(self, tmp_path: Path) -> None:
        """DatasetRef discovery metadata serialized as allowlist (#490)."""
        from kpubdata.core.capability import PaginationMode, QuerySupport
        from kpubdata.core.models import Operation, Representation

        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            description="측정소별 대기오염 물질 농도",
            tags=("environment", "air"),
            source_url="https://www.data.go.kr/data/15073861/openapi",
            representation=Representation.API_JSON,
            operations=frozenset({Operation.GET, Operation.LIST}),
            query_support=QuerySupport(
                pagination=PaginationMode.OFFSET,
                filterable_fields=frozenset({"station_name"}),
                sortable_fields=frozenset(),
                time_range=True,
                max_page_size=1000,
            ),
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["description"] == "측정소별 대기오염 물질 농도"
        assert dataset["tags"] == ["air", "environment"]
        assert dataset["source_url"] == "https://www.data.go.kr/data/15073861/openapi"
        assert dataset["representation"] == "api_json"
        assert dataset["operations"] == ["get", "list"]
        query_support = cast(dict[str, object], dataset["query_support"])
        assert query_support["pagination"] == "offset"
        assert query_support["filterable_fields"] == ["station_name"]
        assert query_support["sortable_fields"] == []
        assert query_support["time_range"] is True
        assert query_support["max_page_size"] == 1000

    def test_catalog_metadata_less_dataset_serializes_null_and_empty(self, tmp_path: Path) -> None:
        """Dataset without metadata serializes as null/empty, response doesn't break (#490)."""
        resp = self._service_with_catalog(
            tmp_path, [_FakeCatalogRef("datago", "air_quality", "대기오염")]
        ).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["description"] is None
        assert dataset["tags"] == []
        assert dataset["source_url"] is None
        assert dataset["representation"] == "api_json"
        assert dataset["operations"] == []
        assert dataset["query_support"] is None
        assert dataset["requires_service_key"] is False
        assert dataset["request_parameters"] == []
        assert dataset["application"] is None
        assert dataset["quota"] is None

    @pytest.mark.parametrize(
        ("terms", "expected"),
        [
            (SimpleNamespace(quota="개발계정 일 10,000건"), "개발계정 일 10,000건"),
            (SimpleNamespace(quota=None), None),
            (SimpleNamespace(quota="  "), None),
            (SimpleNamespace(quota=10000), None),
            (None, None),
        ],
        ids=["declared", "undeclared", "blank", "not-text", "no-licence-attribute"],
    )
    def test_catalog_passes_the_declared_quota_through_verbatim(
        self, tmp_path: Path, terms: object, expected: str | None
    ) -> None:
        """The spec licence's quota, unparsed; anything else is null, not 0 (#778)."""
        ref = _FakeCatalogRef("datago", "air_quality", "대기오염", license=terms)

        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        providers = cast(list[dict[str, object]], resp.body["providers"])
        assert cast(dict[str, object], providers[0]["datasets"][0])["quota"] == expected

    def test_catalog_serializes_application_when_declared(self, tmp_path: Path) -> None:
        """Pass raw_metadata.application as-is (usage guide, no secret)."""
        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            service_key=True,
            raw_metadata_extra={
                "application": {
                    "required": True,
                    "url": "https://www.data.go.kr/data/15073861/openapi.do",
                },
            },
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["application"] == {
            "required": True,
            "url": "https://www.data.go.kr/data/15073861/openapi.do",
        }

    def test_catalog_rejects_non_http_application_url(self, tmp_path: Path) -> None:
        """application.url exposed only if http(s) (arbitrary schemes blocked)."""
        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            raw_metadata_extra={
                "application": {"required": True, "url": "javascript:alert(1)"},
            },
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["application"] is None

    def test_catalog_serializes_request_parameters_without_secrets(self, tmp_path: Path) -> None:
        """Serialize raw_metadata.request_parameters as secret-free allowlist."""
        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            service_key=True,
            raw_metadata_extra={
                "request_parameters": [
                    {
                        "name": "sidoName",
                        "required": True,
                        "description": "조회할 시·도",
                        "example": "서울",
                        "internal_hint": "leak me",
                    },
                    # service_key_param / secret-like names are excluded.
                    {"name": "serviceKey", "required": True},
                    {"name": "apiKey", "required": True},
                    # Drop items without name.
                    {"required": True},
                    "not-a-dict",
                ],
            },
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert dataset["request_parameters"] == [
            {
                "name": "sidoName",
                "required": True,
                "description": "조회할 시·도",
                "example": "서울",
            }
        ]
        assert "internal_hint" not in json.dumps(resp.body, ensure_ascii=False)
        assert "leak me" not in json.dumps(resp.body, ensure_ascii=False)

    def test_catalog_never_exposes_raw_metadata_or_secrets(self, tmp_path: Path) -> None:
        """raw_metadata and secret-like values never exposed in response (#490)."""
        ref = _FakeCatalogRef(
            "datago",
            "air_quality",
            "대기오염",
            service_key=True,
            raw_metadata_extra={
                "internal_note": "provider private",
                "api_key": "sk-secret-value",
                "service_key": "raw-secret",
                "endpoint_template": "/openapi/{serviceKey}",
            },
        )
        resp = self._service_with_catalog(tmp_path, [ref]).catalog()

        assert resp.status_code == 200
        serialized = json.dumps(resp.body, ensure_ascii=False)
        assert "internal_note" not in serialized
        assert "provider private" not in serialized
        assert "sk-secret-value" not in serialized
        assert "raw-secret" not in serialized
        assert "endpoint_template" not in serialized
        # Only allowlist field exists.
        providers = cast(list[dict[str, object]], resp.body["providers"])
        dataset = cast(dict[str, object], providers[0]["datasets"][0])
        assert set(dataset) == {
            "name",
            "title",
            "description",
            "tags",
            "source_url",
            "representation",
            "operations",
            "query_support",
            "requires_service_key",
            "request_parameters",
            "application",
            "quota",
        }
        # service_key_param presence passed only as requires_service_key boolean.
        assert dataset["requires_service_key"] is True
        assert "service_key_param" not in serialized

    def test_catalog_closes_request_client(self, tmp_path: Path) -> None:
        client = _CloseTrackingClient(
            {}, catalog_items=[_FakeCatalogRef("datago", "air_quality", "대기오염")]
        )
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.catalog()

        assert resp.status_code == 200
        assert client.close_calls == 1

    def test_catalog_closes_request_client_when_catalog_fails(self, tmp_path: Path) -> None:
        class _BrokenCatalogClient:
            close_calls = 0

            @property
            def datasets(self) -> object:
                raise RuntimeError("catalog failed")

            def dataset(self, source_key: str) -> _FakeDataset:
                raise KeyError(source_key)

            def close(self) -> None:
                self.close_calls += 1

        client = _BrokenCatalogClient()
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = service.catalog()

        assert resp.status_code == 502
        assert client.close_calls == 1

    def test_catalog_returns_502_when_client_raises(self, tmp_path: Path) -> None:
        class _BrokenClient:
            @property
            def datasets(self) -> object:
                raise RuntimeError("client init failed")

        service = BuilderService(output_root=tmp_path, client_factory=lambda: _BrokenClient())
        resp = service.catalog()
        assert resp.status_code == 502


class TestRunIdRouteValidation:
    """/artifacts/{run_id} route validates run_id before ownership check (#439).

    _read_manifest_created_by assembles path from URL-sourced run_id without validation,
    Unsafe segments like "../" validated by validate_path_segment before _check_ownership
    must be reached.
    """

    def test_unsafe_run_id_returns_400_before_ownership(self, tmp_path: Path) -> None:
        """Unsafe run_id (``..``) returns 400 before _check_ownership (#439)."""
        resp = dispatch(_service(tmp_path), "GET", "/artifacts/../bad", None)
        assert resp.status_code == 400
        err = str(resp.body.get("error", "")).lower()
        assert "run_id" in err or "safe" in err

    def test_blank_run_id_returns_400(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/artifacts/", None)
        assert resp.status_code == 400
        assert "run_id" in str(resp.body.get("error", "")).lower()

    def test_safe_run_id_still_reaches_ownership_check(self, tmp_path: Path) -> None:
        """Safe run_id passes validate then → artifacts (or ownership check) (#439 positive)."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1")
        # ENFORCE_OWNERSHIP off (default) → 200
        resp = dispatch(service, "GET", "/artifacts/run1", None)
        assert resp.status_code == 200


class TestBuildSpecSnapshot:
    """GET /builds/{run_id}/spec query·security·legacy policy (#487)."""

    def test_owner_reads_snapshot_and_index_digest_matches(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        build = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "spec-run"})
        assert build.status_code == 200

        response = dispatch(service, "GET", "/builds/spec-run/spec", None)

        assert response.status_code == 200
        snapshot = (tmp_path / "spec-run" / "buildspec.yaml").read_bytes()
        expected = f"sha256:{hashlib.sha256(snapshot).hexdigest()}"
        assert response.body == {
            "run_id": "spec-run",
            "spec": snapshot.decode("utf-8"),
            "spec_digest": expected,
        }
        entry = service._build_index.get("spec-run")
        assert entry is not None
        assert entry.spec_digest == expected

    def test_unknown_and_legacy_run_return_404(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert dispatch(service, "GET", "/builds/unknown/spec", None).status_code == 404

        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "manifest.json").write_text("{}", encoding="utf-8")
        response = dispatch(service, "GET", "/builds/legacy/spec", None)
        assert response.status_code == 404
        assert "unavailable" in str(response.body["error"])

    @pytest.mark.parametrize("run_id", ["..", "../escape", "bad%2Fsegment"])
    def test_invalid_run_id_returns_400(self, tmp_path: Path, run_id: str) -> None:
        response = dispatch(_service(tmp_path), "GET", f"/builds/{run_id}/spec", None)
        assert response.status_code == 400

    def test_another_owner_receives_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        build = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "owned"})
        assert build.status_code == 200

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        response = dispatch(service, "GET", "/builds/owned/spec", None)
        assert response.status_code == 404

        # If ownership denied, never reach snapshot reader.
        monkeypatch.setattr(Path, "read_bytes", lambda _path: pytest.fail("snapshot read leaked"))
        response = dispatch(service, "GET", "/builds/owned/spec", None)
        assert response.status_code == 404

    def test_unknown_run_returns_404_before_ownership(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )

        response = dispatch(_service(tmp_path), "GET", "/builds/unknown/spec", None)

        assert response.status_code == 404


class TestFileResponseStreaming:
    """File responses not loaded entirely into memory (#653 follow-up).

    When ``read_bytes()`` read all at once, one response consumed file-size memory
    Used. Served build artifacts (parquet/jsonl) have no size limit,
    A few concurrent downloads could crash process.
    """

    def _put_artifact(self, tmp_path: Path, name: str, payload: bytes) -> None:
        run_dir = tmp_path / "run-stream"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_bytes(payload)

    def test_a_file_larger_than_one_chunk_arrives_byte_exact(
        self, http_server: tuple[str, HTTPServer, threading.Thread], tmp_path: Path
    ) -> None:
        import os

        from kpubdata_builder.service.http import _FILE_CHUNK_BYTES

        payload = os.urandom(_FILE_CHUNK_BYTES * 3 + 977)
        self._put_artifact(tmp_path, "big.parquet", payload)
        base_url, _, _ = http_server

        with urllib.request.urlopen(
            f"{base_url}/artifacts/run-stream/big.parquet", timeout=10.0
        ) as response:
            body = response.read()
            assert int(response.headers["Content-Length"]) == len(payload)

        assert body == payload

    def test_the_whole_file_is_never_read_into_memory(
        self, http_server: tuple[str, HTTPServer, threading.Thread], tmp_path: Path
    ) -> None:
        """Even if ``read_bytes`` blocked, download works — proof it reads in chunks."""
        import unittest.mock

        payload = b"x" * 5000
        self._put_artifact(tmp_path, "data.jsonl", payload)
        base_url, _, _ = http_server

        def _forbidden(self: Path) -> bytes:
            raise AssertionError("파일 응답이 전체를 한 번에 읽었다")

        with (
            unittest.mock.patch.object(Path, "read_bytes", _forbidden),
            urllib.request.urlopen(
                f"{base_url}/artifacts/run-stream/data.jsonl", timeout=10.0
            ) as response,
        ):
            assert response.read() == payload

    def test_an_empty_file_is_served_as_empty(
        self, http_server: tuple[str, HTTPServer, threading.Thread], tmp_path: Path
    ) -> None:
        """Length 0 never enters read loop — must finish without blocking."""
        self._put_artifact(tmp_path, "empty.csv", b"")
        base_url, _, _ = http_server

        with urllib.request.urlopen(
            f"{base_url}/artifacts/run-stream/empty.csv", timeout=10.0
        ) as response:
            assert response.read() == b""
            assert response.headers["Content-Length"] == "0"


class TestFileContentTypeCharset:
    """Don't declare character encoding on binary."""

    def test_binary_types_get_no_charset(self) -> None:
        from kpubdata_builder.service.http import _content_type_header

        # Previously added to all file responses, even parquet got charset.
        assert _content_type_header("application/vnd.apache.parquet") == (
            "application/vnd.apache.parquet"
        )
        assert _content_type_header("application/octet-stream") == "application/octet-stream"

    def test_textual_types_keep_charset(self) -> None:
        from kpubdata_builder.service.http import _content_type_header

        assert _content_type_header("text/csv") == "text/csv; charset=utf-8"
        assert _content_type_header("application/json") == "application/json; charset=utf-8"
        assert _content_type_header("application/x-ndjson") == (
            "application/x-ndjson; charset=utf-8"
        )
        assert _content_type_header("application/geo+json") == (
            "application/geo+json; charset=utf-8"
        )
        assert _content_type_header("text/yaml") == "text/yaml; charset=utf-8"
