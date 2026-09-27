"""stages.bronze.resolve: verify source kind resolver produces identical BronzeArtifact (#498)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from kpubdata_builder.ingestion import IngestionError
from kpubdata_builder.ingestion.url_fetch import FetchResult
from kpubdata_builder.spec import JsonValue, SourceRef
from kpubdata_builder.stages._path_safety import validate_path_segment
from kpubdata_builder.stages.bronze.resolve import (
    build_bronze_artifact_for_source,
    sanitize_endpoint_identity,
    source_identity,
)
from kpubdata_builder.uploads import SQLiteUploadRepository
from kpubdata_builder.uploads.store import generate_upload_id


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
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


# --- source_identity / sanitize_endpoint_identity ---------------------------------


def test_source_identity_for_public_api_is_unchanged() -> None:
    source = SourceRef(provider="datago", dataset="air_quality")

    assert source_identity(source) == ("datago", "air_quality")


def test_source_identity_for_file_uses_upload_id_not_path() -> None:
    source = SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="csv")

    assert source_identity(source) == ("file", "upl_" + "a" * 32)


def test_source_identity_for_url_is_a_safe_path_segment() -> None:
    """url identity cannot include :,/ etc — must always be safe as path segment.

    alias가 없으면 이 값이 그대로 bronze/silver/gold 출력 디렉터리 세그먼트로
    쓰이므로(orchestrator._fetch_source_key), colon/slash가 남으면
    validate_path_segment가 거부한다(#498).
    """
    source = SourceRef(kind="url", endpoint="https://example.org:8443/data?token=secret&x=1#frag")

    provider, dataset = source_identity(source)

    assert provider == "url"
    assert dataset.startswith("example-org-")
    assert ":" not in dataset
    assert "/" not in dataset
    assert "secret" not in dataset
    validate_path_segment(dataset, field_name="dataset")  # raises on failure


def test_source_identity_for_url_is_stable_and_distinguishes_different_paths() -> None:
    same_again = source_identity(SourceRef(kind="url", endpoint="https://example.org/data"))
    different_path = source_identity(SourceRef(kind="url", endpoint="https://example.org/other"))

    assert source_identity(SourceRef(kind="url", endpoint="https://example.org/data")) == same_again
    assert same_again != different_path


def test_sanitize_endpoint_identity_drops_query_string() -> None:
    assert (
        sanitize_endpoint_identity("https://example.org/data?api_key=shh")
        == "https://example.org/data"
    )


def test_sanitize_endpoint_identity_defaults_empty_path_to_slash() -> None:
    assert sanitize_endpoint_identity("https://example.org") == "https://example.org/"


# --- build_bronze_artifact_for_source: public_api (existing path unchanged) --------------


def test_public_api_source_delegates_to_existing_client_path() -> None:
    source = SourceRef(provider="datago", dataset="air_quality", params={"page": 1})
    client = _FakeClient({"datago.air_quality": [{"id": "1"}]})

    artifact = build_bronze_artifact_for_source(source, client=client)

    assert artifact.source_key == "datago.air_quality"
    assert artifact.raw_records == ({"id": "1"},)
    assert artifact.fetch_params == {"page": 1}


def test_unknown_source_kind_is_rejected_fail_closed() -> None:
    """Unknown kind not implicitly handled like public_api (#538 review).

    validate_spec이 loader를 거치지 않은 BuildSpec도 이미 거부하지만, resolver
    자신도 "그 외는 public_api" implicit fallback을 두지 않고 독립적으로
    fail-closed해야 한다 — resolver를 직접 호출하는 다른 경로(테스트, 향후
    호출자)가 validate_spec을 우회해도 안전하도록.
    """
    source = SourceRef(kind="ftp", provider="datago", dataset="air_quality")

    with pytest.raises(IngestionError, match="unsupported source kind"):
        build_bronze_artifact_for_source(source, client=_FakeClient({}))


# --- build_bronze_artifact_for_source: file ---------------------------------------


def _upload_repo(tmp_path: Path) -> SQLiteUploadRepository:
    return SQLiteUploadRepository(tmp_path / "uploads.sqlite3")


def test_file_source_reads_and_parses_upload(tmp_path: Path) -> None:
    repo = _upload_repo(tmp_path)
    metadata = repo.put(
        "owner-1", content=b"id,v\n1,10\n", format="csv", encoding="utf-8", original_filename=None
    )
    source = SourceRef(kind="file", upload_id=metadata.upload_id, format="csv", encoding="utf-8")

    artifact = build_bronze_artifact_for_source(
        source, client=_FakeClient({}), upload_repository=repo, owner_id="owner-1"
    )

    assert artifact.source_key == f"file.{metadata.upload_id}"
    assert artifact.raw_records == ({"id": 1, "v": 10},)
    assert artifact.fetch_params == {
        "upload_id": metadata.upload_id,
        "format": "csv",
        "encoding": "utf-8",
    }
    # No filesystem paths left in provenance.
    assert artifact.provenance is not None
    assert "\\" not in str(artifact.provenance.fetch_params)
    assert "/" not in str(artifact.provenance.fetch_params.get("upload_id", ""))


def test_file_source_without_repository_raises_ingestion_error() -> None:
    source = SourceRef(kind="file", upload_id=generate_upload_id(), format="csv")

    with pytest.raises(IngestionError, match="upload store"):
        build_bronze_artifact_for_source(
            source, client=_FakeClient({}), upload_repository=None, owner_id="owner-1"
        )


def test_file_source_without_owner_id_raises_ingestion_error(tmp_path: Path) -> None:
    repo = _upload_repo(tmp_path)
    source = SourceRef(kind="file", upload_id=generate_upload_id(), format="csv")

    with pytest.raises(IngestionError, match="authenticated"):
        build_bronze_artifact_for_source(
            source, client=_FakeClient({}), upload_repository=repo, owner_id=None
        )


def test_file_source_unknown_upload_id_raises_ingestion_error(tmp_path: Path) -> None:
    repo = _upload_repo(tmp_path)
    source = SourceRef(kind="file", upload_id=generate_upload_id(), format="csv")

    with pytest.raises(IngestionError, match="not found"):
        build_bronze_artifact_for_source(
            source, client=_FakeClient({}), upload_repository=repo, owner_id="owner-1"
        )


def test_file_source_owned_by_another_principal_raises_not_found(tmp_path: Path) -> None:
    """Referencing another owner's upload_id appears as not found regardless of existence."""
    repo = _upload_repo(tmp_path)
    metadata = repo.put(
        "owner-1", content=b"id\n1\n", format="csv", encoding="utf-8", original_filename=None
    )
    source = SourceRef(kind="file", upload_id=metadata.upload_id, format="csv")

    with pytest.raises(IngestionError, match="not found"):
        build_bronze_artifact_for_source(
            source, client=_FakeClient({}), upload_repository=repo, owner_id="owner-2"
        )


def test_file_source_format_mismatch_with_stored_upload_raises(tmp_path: Path) -> None:
    repo = _upload_repo(tmp_path)
    metadata = repo.put(
        "owner-1", content=b"id\n1\n", format="csv", encoding="utf-8", original_filename=None
    )
    # BuildSpec declares json but upload stored as csv.
    source = SourceRef(kind="file", upload_id=metadata.upload_id, format="json", encoding="utf-8")

    with pytest.raises(IngestionError, match="does not match"):
        build_bronze_artifact_for_source(
            source, client=_FakeClient({}), upload_repository=repo, owner_id="owner-1"
        )


def test_file_source_encoding_mismatch_with_stored_upload_raises(tmp_path: Path) -> None:
    """If format matches but encoding differs, reject silently without reinterpreting (#498)."""
    repo = _upload_repo(tmp_path)
    metadata = repo.put(
        "owner-1", content=b"id\n1\n", format="csv", encoding="utf-8", original_filename=None
    )
    # BuildSpec matches upload with format=csv but declares encoding euc-kr.
    source = SourceRef(kind="file", upload_id=metadata.upload_id, format="csv", encoding="euc-kr")

    with pytest.raises(IngestionError, match="does not match"):
        build_bronze_artifact_for_source(
            source, client=_FakeClient({}), upload_repository=repo, owner_id="owner-1"
        )


# --- build_bronze_artifact_for_source: url ----------------------------------------


def test_url_source_uses_safe_fetch_and_declared_format(monkeypatch: pytest.MonkeyPatch) -> None:
    import kpubdata_builder.stages.bronze.resolve as resolve_module

    def _fake_fetch(url: str, *, max_bytes: int) -> FetchResult:
        assert url == "https://example.org/data.json"
        return FetchResult(content=b'[{"id": 1}]', content_type="application/json", final_url=url)

    monkeypatch.setattr(resolve_module, "safe_fetch_get", _fake_fetch)
    source = SourceRef(kind="url", endpoint="https://example.org/data.json", format="json")

    artifact = build_bronze_artifact_for_source(source, client=_FakeClient({}))

    assert artifact.source_key.startswith("url.example-org-")
    assert artifact.raw_records == ({"id": 1},)
    assert artifact.fetch_params == {"endpoint": "https://example.org/data.json", "method": "GET"}


def test_url_source_infers_format_from_content_type_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kpubdata_builder.stages.bronze.resolve as resolve_module

    def _fake_fetch(url: str, *, max_bytes: int) -> FetchResult:
        return FetchResult(content=b"a,b\n1,2\n", content_type="text/csv", final_url=url)

    monkeypatch.setattr(resolve_module, "safe_fetch_get", _fake_fetch)
    source = SourceRef(kind="url", endpoint="https://example.org/data")

    artifact = build_bronze_artifact_for_source(source, client=_FakeClient({}))

    assert artifact.raw_records == ({"a": 1, "b": 2},)


def test_url_source_defaults_to_json_when_format_and_content_type_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kpubdata_builder.stages.bronze.resolve as resolve_module

    def _fake_fetch(url: str, *, max_bytes: int) -> FetchResult:
        return FetchResult(content=b'[{"id": 1}]', content_type="", final_url=url)

    monkeypatch.setattr(resolve_module, "safe_fetch_get", _fake_fetch)
    source = SourceRef(kind="url", endpoint="https://example.org/data")

    artifact = build_bronze_artifact_for_source(source, client=_FakeClient({}))

    assert artifact.raw_records == ({"id": 1},)


def test_url_source_provenance_excludes_query_string(monkeypatch: pytest.MonkeyPatch) -> None:
    import kpubdata_builder.stages.bronze.resolve as resolve_module

    def _fake_fetch(url: str, *, max_bytes: int) -> FetchResult:
        return FetchResult(content=b'[{"id": 1}]', content_type="application/json", final_url=url)

    monkeypatch.setattr(resolve_module, "safe_fetch_get", _fake_fetch)
    source = SourceRef(
        kind="url", endpoint="https://example.org/data?api_key=super-secret", format="json"
    )

    artifact = build_bronze_artifact_for_source(source, client=_FakeClient({}))

    assert "super-secret" not in str(artifact.fetch_params)
    assert artifact.provenance is not None
    assert "super-secret" not in str(artifact.provenance.fetch_params)
