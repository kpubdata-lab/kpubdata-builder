"""Studio↔Builder contract integration tests (#226).

External UIs like Studio serialize specs (snake_case mappings) and flow them through actual
BuilderService via dispatch layer, fixing the real wire response shape Studio depends on.

Endpoints under test:
    - POST /validate (200)
    - POST /build SUCCESS (200)
    - POST /build FAILURE (502)
    - GET  /artifacts/{run_id} (200)

Data is supplied by in-test fake source client (dataset(key).list(**params).items).
No actual network calls are made.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from kpubdata_builder.service import API_CONTRACT_VERSION, BuilderService, dispatch
from kpubdata_builder.spec import JsonValue


class _FakeResult:
    """Fake satisfying the SourceClient Protocol's DatasetResult part (items)."""

    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    """Fake satisfying the dataset(key).list(**params) part."""

    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    """Fake structurally satisfying the SourceClient Protocol that builder depends on."""

    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _studio_spec(*, dataset_value: str = "air_quality") -> dict[str, JsonValue]:
    """Create spec mapping in Studio-serialized form (snake_case).

    Includes sources/exports/metadata and optional fields (params, alias, options) to
    intentionally faithfully reproduce the wire form Studio sends.
    """
    return {
        "dataset_id": "dataset.studio_sample",
        "title": "Studio Sample Dataset",
        "description": "A dataset assembled via the Studio UI.",
        "sources": [
            {
                "provider": "datago",
                "dataset": dataset_value,
                "params": {"region": "seoul", "year": 2024},
                "alias": "aq",
            }
        ],
        "exports": [
            {
                "kind": "jsonl",
                "output_path": "out/data.jsonl",
                "options": {"ensure_ascii": False},
            }
        ],
        "metadata": {"owner": "studio", "license": "CC-BY-4.0"},
    }


def _serialize(spec: dict[str, JsonValue]) -> str:
    """Convert Studio-serialized spec mapping to wire form (YAML string) builder receives."""
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


class TestValidateContract:
    def test_validate_returns_valid_envelope(self, tmp_path: Path) -> None:
        spec_yaml = _serialize(_studio_spec())
        resp = dispatch(_service(tmp_path), "POST", "/validate", {"spec": spec_yaml})

        assert resp.status_code == 200
        assert resp.body == {
            "status": "valid",
            "dataset_id": "dataset.studio_sample",
            "api_version": API_CONTRACT_VERSION,
        }


class TestBuildSuccessContract:
    def test_build_success_envelope(self, tmp_path: Path) -> None:
        spec_yaml = _serialize(_studio_spec())
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/build",
            {"spec": spec_yaml, "run_id": "studio-run"},
        )

        assert resp.status_code == 200
        body = resp.body
        # Top-level wire shape fixed: status/manifest/outcomes/run_id/api_version.
        assert body["status"] == "ok"
        assert body["run_id"] == "studio-run"
        assert body["api_version"] == API_CONTRACT_VERSION
        assert isinstance(body["manifest"], str)
        assert body["manifest"].endswith("manifest.json")

        outcomes = body["outcomes"]
        assert isinstance(outcomes, list)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert isinstance(outcome, dict)
        # With alias, outcome source_key becomes alias (not provider.dataset).
        assert outcome["source_key"] == "aq"
        assert outcome["status"] == "ok"
        assert outcome["error"] is None
        assert isinstance(outcome["stages_completed"], list)

        # Verify manifest file was actually recorded.
        assert (tmp_path / "studio-run" / "manifest.json").exists()


class TestBuildFailureContract:
    def test_build_failure_envelope(self, tmp_path: Path) -> None:
        # Point to nonexistent source to trigger fetch failure.
        failing_spec = _studio_spec(dataset_value="missing")
        spec_yaml = _serialize(failing_spec)
        resp = dispatch(
            _service(tmp_path),
            "POST",
            "/build",
            {"spec": spec_yaml, "run_id": "studio-fail"},
        )

        assert resp.status_code == 502
        body = resp.body
        assert body["status"] == "failed"
        assert body["run_id"] == "studio-fail"
        assert body["api_version"] == API_CONTRACT_VERSION

        outcomes = body["outcomes"]
        assert isinstance(outcomes, list)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert isinstance(outcome, dict)
        # Fetch attempts provider.dataset(datago.missing) but outcome source_key is
        # tagged with alias ("aq") (_output_source_key policy).
        assert outcome["source_key"] == "aq"
        assert outcome["status"] == "failed"
        assert isinstance(outcome["error"], str)
        assert outcome["error"]

        # #226: Top-level human-readable error summary derives from first failed outcome error.
        assert isinstance(body["error"], str)
        assert body["error"] == outcome["error"]


class TestArtifactsContract:
    def test_artifacts_lists_files_after_build(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        spec_yaml = _serialize(_studio_spec())
        dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": "studio-art"})

        resp = dispatch(service, "GET", "/artifacts/studio-art", None)

        assert resp.status_code == 200
        body = resp.body
        assert body["run_id"] == "studio-art"
        files = body["files"]
        assert isinstance(files, list)
        assert all(isinstance(f, str) for f in files)
        assert any("manifest.json" in f for f in files)
