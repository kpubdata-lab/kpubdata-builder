"""HTTP transport layer integration tests (#256).

tests/integration/test_studio_contract.py validates the Builder↔Studio contract by calling
dispatch(service, method, path, body) directly, but the actual HTTP transport layer
(service/http.py) is not included in that test path. This file starts a real
ThreadingHTTPServer and sends real HTTP requests via urllib.request to verify that http.py's
logic (Content-Length parsing, body size limits, JSON parsing/validation, query string
separation, JSON 500 on exception, Content-Type header etc.) works correctly alongside
dispatch()'s contract.

Endpoints under test (normal paths):
    - GET  /version
    - POST /validate
    - POST /preview
    - POST /build
    - GET  /builds/{run_id}/manifest
    - GET  /artifacts/{run_id}
    - GET  /builds

Endpoints under test (error paths):
    - empty body GET request
    - oversized body -> 413
    - malformed JSON -> 400
    - non-object JSON (array) -> 400
    - nonexistent path -> 404

Data is supplied by in-test fake source client (dataset(key).list(**params).items).
No actual network calls are made.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from http.server import HTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import quote

import pytest

from kpubdata_builder.service import API_CONTRACT_VERSION, BuilderService
from kpubdata_builder.service.http import _MAX_BODY_BYTES, make_handler
from kpubdata_builder.spec import JsonValue

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


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


@pytest.fixture()
def http_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterable[str]:
    """Start real HTTPServer on arbitrary port to verify round-trip via http.py."""
    # Tests set dev-mode to skip auth (#321, ADR 0006).
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    server = HTTPServer(("127.0.0.1", 0), make_handler(_service(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def _post(base_url: str, path: str, payload: dict[str, JsonValue]) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, cast(dict[str, object], json.loads(resp.read()))
    except urllib.error.HTTPError as exc:
        return exc.code, cast(dict[str, object], json.loads(exc.read()))


class TestVersionRoundTrip:
    def test_get_version_returns_200(self, http_server: str) -> None:
        with urllib.request.urlopen(f"{http_server}/version", timeout=5.0) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
            body = cast(dict[str, object], json.loads(resp.read()))
        assert body["service"] == "kpubdata-builder"
        assert body["api_version"] == API_CONTRACT_VERSION


class TestValidateRoundTrip:
    def test_post_validate_returns_200(self, http_server: str) -> None:
        status, body = _post(http_server, "/validate", {"spec": VALID_SPEC_YAML})
        assert status == 200
        assert body["status"] == "valid"
        assert body["dataset_id"] == "dataset.sample"


class TestPreviewRoundTrip:
    def test_post_preview_returns_200(self, http_server: str) -> None:
        status, body = _post(http_server, "/preview", {"spec": VALID_SPEC_YAML, "limit": 1})
        assert status == 200
        previews = body["previews"]
        assert isinstance(previews, list)
        assert previews[0]["source_key"] == "datago.air_quality"  # type: ignore[index]


class TestBuildRoundTrip:
    def test_post_build_returns_200_and_writes_manifest(
        self, http_server: str, tmp_path: Path
    ) -> None:
        status, body = _post(http_server, "/build", {"spec": VALID_SPEC_YAML, "run_id": "http-run"})
        assert status == 200
        assert body["status"] == "ok"
        assert body["run_id"] == "http-run"
        assert (tmp_path / "http-run" / "manifest.json").exists()

    def test_get_build_manifest_returns_json_after_build(self, http_server: str) -> None:
        _post(http_server, "/build", {"spec": VALID_SPEC_YAML, "run_id": "http-manifest"})

        with urllib.request.urlopen(
            f"{http_server}/builds/http-manifest/manifest", timeout=5.0
        ) as resp:
            assert resp.status == 200
            body = cast(dict[str, object], json.loads(resp.read()))

        assert body["build_id"] == "http-manifest"
        assert body["schema_version"] == "1.0.0"


class TestArtifactsRoundTrip:
    def test_get_artifacts_returns_200_after_build(self, http_server: str) -> None:
        _post(http_server, "/build", {"spec": VALID_SPEC_YAML, "run_id": "http-art"})

        with urllib.request.urlopen(f"{http_server}/artifacts/http-art", timeout=5.0) as resp:
            assert resp.status == 200
            body = cast(dict[str, object], json.loads(resp.read()))
        assert body["run_id"] == "http-art"
        files = body["files"]
        assert isinstance(files, list)
        assert any("manifest.json" in f for f in files)  # type: ignore[operator]


def _http_get_bytes(url: str) -> tuple[int, bytes]:
    """Send HTTP GET and return (status, raw body bytes). HTTPError normalized to status."""
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _canonical_to_url(base_url: str, run_id: str, canonical: str) -> str:
    """Convert canonical run-relative POSIX path to URL identical to Studio downloader:
    keep "/" separators, percent-encode segments only."""
    encoded = "/".join(quote(seg, safe="") for seg in canonical.split("/"))
    return f"{base_url}/artifacts/{quote(run_id, safe='')}/{encoded}"


class TestArtifactDownloadRoundTrip:
    """Individual artifact file download through real HTTP boundary (#323 follow-up).

    Previous regression: Studio used manifest.outputs (= output_root absolute storage path with OS
    separators) directly as file_path, browser encoded "\\" to "%5C" -> Builder returned 400.
    Canonical identity is the run-relative POSIX path given by GET /artifacts/{run_id}.
    """

    def _build(self, base_url: str, run_id: str) -> list[str]:
        status, body = _post(base_url, "/build", {"spec": VALID_SPEC_YAML, "run_id": run_id})
        assert status == 200, body
        s, listing = _http_get_bytes(f"{base_url}/artifacts/{run_id}")
        assert s == 200
        files = cast(list[str], json.loads(listing)["files"])
        assert files
        return files

    def test_listing_exposes_only_posix_run_relative_paths(
        self, http_server: str, tmp_path: Path
    ) -> None:
        files = self._build(http_server, "http-dl-1")
        for wire in files:
            assert "\\" not in wire, wire
            assert not wire.startswith("/"), wire
            assert str(tmp_path) not in wire, wire
            assert "dist" not in wire.split("/")[0] or wire.split("/")[0] in {
                "bronze",
                "silver",
                "gold",
            }
        # Build creates nested artifacts.
        assert any("/" in f for f in files)

    def test_downloads_nested_artifact_by_canonical_path(
        self, http_server: str, tmp_path: Path
    ) -> None:
        files = self._build(http_server, "http-dl-2")
        nested = next(f for f in files if "/" in f and f.endswith((".jsonl", ".parquet", ".json")))

        status, payload = _http_get_bytes(_canonical_to_url(http_server, "http-dl-2", nested))
        assert status == 200
        on_disk = (tmp_path / "http-dl-2").joinpath(*nested.split("/")).read_bytes()
        assert payload == on_disk

        # Top-level files (manifest.json) also received same way.
        url = _canonical_to_url(http_server, "http-dl-2", "manifest.json")
        status, payload = _http_get_bytes(url)
        assert status == 200
        assert payload == (tmp_path / "http-dl-2" / "manifest.json").read_bytes()

    def test_raw_storage_style_path_with_encoded_backslash_is_rejected(
        self, http_server: str
    ) -> None:
        self._build(http_server, "http-dl-3")
        # Form that manifest.outputs gave: output_root dir name + "\\" separator (%5C).
        bad = (
            f"{http_server}/artifacts/http-dl-3/"
            "dist-new-user-preview%5Chttp-dl-3%5Cbronze%5Cdatago.air_quality%5Craw_records.jsonl"
        )
        status, _ = _http_get_bytes(bad)
        assert status in (400, 404)
        assert status != 200

    def test_http_traversal_requests_are_blocked(self, http_server: str) -> None:
        self._build(http_server, "http-dl-4")
        _post(http_server, "/build", {"spec": VALID_SPEC_YAML, "run_id": "http-dl-victim"})

        for suffix in (
            "../http-dl-victim/manifest.json",
            "%2e%2e/http-dl-victim/manifest.json",
            "silver%2f..%2f..%2fmanifest.json",
            "silver%5c..%5c..%5cmanifest.json",
            "..%2f..%2fetc%2fpasswd",
            "silver//table.parquet",
        ):
            status, _ = _http_get_bytes(f"{http_server}/artifacts/http-dl-4/{suffix}")
            assert status in (400, 403, 404), suffix
            assert status != 200, suffix

    def test_cannot_reach_other_run_via_listing(self, http_server: str, tmp_path: Path) -> None:
        files_a = self._build(http_server, "http-dl-a")
        self._build(http_server, "http-dl-b")
        # Requesting run B with run A's canonical path still resolves within A only
        # (no cross-access).
        nested = next(f for f in files_a if "/" in f)
        status, _ = _http_get_bytes(_canonical_to_url(http_server, "http-dl-b", nested))
        # B also has the same relative path file so 200, but content must be B's.
        assert status == 200
        on_disk_b = (tmp_path / "http-dl-b").joinpath(*nested.split("/")).read_bytes()
        _, payload = _http_get_bytes(_canonical_to_url(http_server, "http-dl-b", nested))
        assert payload == on_disk_b


class TestBuildsRoundTrip:
    def test_get_builds_returns_200_after_build(self, http_server: str) -> None:
        _post(http_server, "/build", {"spec": VALID_SPEC_YAML, "run_id": "http-list"})

        with urllib.request.urlopen(f"{http_server}/builds", timeout=5.0) as resp:
            assert resp.status == 200
            body = cast(dict[str, object], json.loads(resp.read()))
        builds = body["builds"]
        assert isinstance(builds, list)
        assert any(b["run_id"] == "http-list" for b in builds)  # type: ignore[index,union-attr]


class TestEmptyBodyRequest:
    def test_get_with_no_body_succeeds(self, http_server: str) -> None:
        # GET request must be processed normally even without Content-Length/body.
        req = urllib.request.Request(f"{http_server}/version", method="GET")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            assert resp.status == 200


class TestOversizedBodyRequest:
    def test_oversized_body_returns_413(self, http_server: str) -> None:
        host_port = http_server.removeprefix("http://")
        host, port = host_port.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5.0)
        try:
            conn.putrequest("POST", "/validate")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(_MAX_BODY_BYTES + 1))
            conn.endheaders()  # Do not send body - handler rejects after seeing headers only.
            response = conn.getresponse()
            assert response.status == 413
            body = cast(dict[str, object], json.loads(response.read()))
            assert "too large" in str(body.get("error", ""))
        finally:
            conn.close()


class TestInvalidJsonRequest:
    def test_malformed_json_returns_400(self, http_server: str) -> None:
        req = urllib.request.Request(
            f"{http_server}/validate",
            data=b"not-json{{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5.0)
        assert exc_info.value.code == 400
        body = cast(dict[str, object], json.loads(exc_info.value.read()))
        assert "invalid JSON body" in str(body.get("error", ""))


class TestNonObjectJsonRequest:
    def test_array_json_body_returns_400(self, http_server: str) -> None:
        req = urllib.request.Request(
            f"{http_server}/validate",
            data=b"[1, 2, 3]",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5.0)
        assert exc_info.value.code == 400
        body = cast(dict[str, object], json.loads(exc_info.value.read()))
        assert "object" in str(body.get("error", ""))


class TestUnknownPathRequest:
    def test_unknown_path_returns_404(self, http_server: str) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{http_server}/nope", timeout=5.0)
        assert exc_info.value.code == 404
