"""Canonical source contract(#498): public_api/file/url kind parsing·validation·round-trip."""

from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from kpubdata_builder.errors import SpecLoadError, ValidationError
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef, parse_spec
from kpubdata_builder.spec.serializer import canonical_spec_mapping, serialize_spec
from kpubdata_builder.spec.validator import validate_spec


def _payload(*sources: dict[str, object]) -> dict[str, object]:
    return {
        "dataset_id": "dataset.sample",
        "title": "Sample Dataset",
        "description": "Sample description",
        "sources": list(sources),
        "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
    }


def _spec(*sources: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )


# --- loader: kind / -----------------------------------------------------


def test_source_without_kind_defaults_to_public_api() -> None:
    """kind   source  public_api (#498,  )."""
    spec = parse_spec(_payload({"provider": "datago", "dataset": "air_quality"}))

    assert spec.sources[0].kind == "public_api"
    assert spec.sources[0].provider == "datago"
    assert spec.sources[0].dataset == "air_quality"


def test_explicit_public_api_kind_parses_same_as_default() -> None:
    spec = parse_spec(
        _payload({"kind": "public_api", "provider": "datago", "dataset": "air_quality"})
    )

    assert spec.sources[0] == SourceRef(provider="datago", dataset="air_quality", kind="public_api")


def test_file_source_parses_required_fields() -> None:
    spec = parse_spec(
        _payload(
            {
                "kind": "file",
                "upload_id": "upl_" + "a" * 32,
                "format": "csv",
                "encoding": "utf-8",
                "alias": "uploaded",
            }
        )
    )
    source = spec.sources[0]

    assert source.kind == "file"
    assert source.upload_id == "upl_" + "a" * 32
    assert source.format == "csv"
    assert source.encoding == "utf-8"
    assert source.alias == "uploaded"
    assert source.provider == ""
    assert source.dataset == ""


def test_file_source_encoding_defaults_to_utf8() -> None:
    spec = parse_spec(_payload({"kind": "file", "upload_id": "upl_" + "a" * 32, "format": "csv"}))

    assert spec.sources[0].encoding == "utf-8"


def test_url_source_parses_required_fields() -> None:
    spec = parse_spec(
        _payload({"kind": "url", "endpoint": "https://example.org/data.json", "method": "GET"})
    )
    source = spec.sources[0]

    assert source.kind == "url"
    assert source.endpoint == "https://example.org/data.json"
    assert source.method == "GET"
    assert source.provider == ""
    assert source.dataset == ""


def test_url_source_method_defaults_to_get() -> None:
    spec = parse_spec(_payload({"kind": "url", "endpoint": "https://example.org/data.json"}))

    assert spec.sources[0].method == "GET"


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(SpecLoadError, match="kind"):
        parse_spec(_payload({"kind": "ftp", "endpoint": "ftp://example.org/data"}))


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "public_api", "provider": "datago", "dataset": "d", "upload_id": "upl_x"},
        {"kind": "public_api", "provider": "datago", "dataset": "d", "endpoint": "https://x"},
        {"kind": "file", "upload_id": "upl_" + "a" * 32, "format": "csv", "provider": "datago"},
        {"kind": "file", "upload_id": "upl_" + "a" * 32, "format": "csv", "endpoint": "https://x"},
        {"kind": "url", "endpoint": "https://example.org/data", "provider": "datago"},
        {"kind": "url", "endpoint": "https://example.org/data", "upload_id": "upl_x"},
    ],
)
def test_foreign_kind_fields_are_rejected(source: dict[str, object]) -> None:
    """kind field  loader   (#498)."""
    with pytest.raises(SpecLoadError, match="not valid for kind"):
        parse_spec(_payload(source))


def test_file_source_missing_upload_id_is_rejected() -> None:
    with pytest.raises(SpecLoadError):
        parse_spec(_payload({"kind": "file", "format": "csv"}))


def test_file_source_missing_format_is_rejected() -> None:
    with pytest.raises(SpecLoadError):
        parse_spec(_payload({"kind": "file", "upload_id": "upl_" + "a" * 32}))


def test_url_source_missing_endpoint_is_rejected() -> None:
    with pytest.raises(SpecLoadError):
        parse_spec(_payload({"kind": "url"}))


def test_alias_and_schema_are_common_across_kinds() -> None:
    """alias/schema  kind    (#498)."""
    spec = parse_spec(
        _payload(
            {
                "kind": "file",
                "upload_id": "upl_" + "a" * 32,
                "format": "csv",
                "alias": "my_alias",
                "schema": {"required": ["id"]},
            }
        )
    )
    source = spec.sources[0]

    assert source.alias == "my_alias"
    assert source.schema is not None
    assert source.schema.required == ("id",)


# --- validator: kind semantic  ----------------------------------------------


def test_validate_spec_accepts_valid_file_source() -> None:
    spec = _spec(
        SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="csv", encoding="utf-8")
    )
    validate_spec(spec)  # raises on failure


def test_validate_spec_accepts_valid_url_source() -> None:
    spec = _spec(SourceRef(kind="url", endpoint="https://example.org/data.json", method="GET"))
    validate_spec(spec)  # raises on failure


def test_validate_spec_rejects_unknown_kind_from_directly_constructed_sourceref() -> None:
    """loader   SourceRef   unknown kind .

    loader(YAML )  unknown kind (``test_unknown_kind_is_rejected``),
    ``SourceRef(kind="ftp", ...)`` programmatic BuildSpec
    loader   — validate_spec canonical kind
    (public_api|file|url) fail-closed    (#538 review).
    """
    spec = _spec(SourceRef(kind="ftp", provider="datago", dataset="air_quality"))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "unsupported_source_kind" in codes


def test_validate_spec_rejects_malformed_upload_id() -> None:
    spec = _spec(SourceRef(kind="file", upload_id="../../etc/passwd", format="csv"))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "invalid_upload_id" in codes


def test_validate_spec_rejects_empty_upload_id() -> None:
    spec = _spec(SourceRef(kind="file", upload_id="", format="csv"))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "empty_field" in codes


def test_validate_spec_rejects_unsupported_file_format() -> None:
    spec = _spec(SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="xlsx"))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "unsupported_source_format" in codes


def test_validate_spec_rejects_unknown_encoding() -> None:
    spec = _spec(
        SourceRef(
            kind="file", upload_id="upl_" + "a" * 32, format="csv", encoding="not-a-real-encoding"
        )
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "unknown_encoding" in codes


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.org/data",
        "ftp://example.org/data",
        "file:///etc/passwd",
    ],
)
def test_validate_spec_rejects_non_https_url_scheme(endpoint: str) -> None:
    spec = _spec(SourceRef(kind="url", endpoint=endpoint))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "unsafe_url_scheme" in codes


def test_validate_spec_rejects_url_with_userinfo() -> None:
    spec = _spec(SourceRef(kind="url", endpoint="https://user:pass@example.org/data"))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "url_userinfo_forbidden" in codes


def test_validate_spec_rejects_non_get_method() -> None:
    spec = _spec(replace(SourceRef(kind="url", endpoint="https://example.org/data"), method="POST"))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "unsupported_source_method" in codes


def test_validate_spec_rejects_unsupported_url_format() -> None:
    spec = _spec(SourceRef(kind="url", endpoint="https://example.org/data", format="parquet"))

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)
    codes = {p.code for p in exc_info.value.structured_problems or []}
    assert "unsupported_source_format" in codes


def test_validate_spec_does_not_require_provider_dataset_for_file_or_url() -> None:
    """file/url kind provider/dataset ( )  ."""
    spec = _spec(
        SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="csv"),
        SourceRef(kind="url", endpoint="https://example.org/data"),
    )
    validate_spec(spec)  # raises on failure


# --- serializer: canonical snapshot round-trip ------------------------------------


def test_canonical_mapping_only_includes_fields_for_that_kind() -> None:
    spec = _spec(SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="csv"))

    mapping = canonical_spec_mapping(spec)
    source_entry = mapping["sources"][0]

    assert set(source_entry) == {"kind", "alias", "schema", "upload_id", "format", "encoding"}


def test_public_api_source_round_trips_through_canonical_yaml() -> None:
    spec = _spec(SourceRef(provider="datago", dataset="air_quality", alias="air"))

    text = serialize_spec(spec)
    reparsed = parse_spec(yaml.safe_load(text))

    assert reparsed == spec


def test_file_source_round_trips_through_canonical_yaml() -> None:
    spec = _spec(
        SourceRef(
            kind="file",
            upload_id="upl_" + "a" * 32,
            format="csv",
            encoding="euc-kr",
            alias="uploaded",
        )
    )

    text = serialize_spec(spec)
    reparsed = parse_spec(yaml.safe_load(text))

    assert reparsed == spec


def test_url_source_round_trips_through_canonical_yaml() -> None:
    spec = _spec(
        SourceRef(
            kind="url",
            endpoint="https://example.org/data.json",
            method="GET",
            format="json",
            alias="feed",
        )
    )

    text = serialize_spec(spec)
    reparsed = parse_spec(yaml.safe_load(text))

    assert reparsed == spec


def test_mixed_kind_sources_round_trip_together() -> None:
    spec = _spec(
        SourceRef(provider="datago", dataset="air_quality", alias="air"),
        SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="csv", alias="uploaded"),
        SourceRef(kind="url", endpoint="https://example.org/data.json", alias="feed"),
    )

    text = serialize_spec(spec)
    reparsed = parse_spec(yaml.safe_load(text))

    assert reparsed == spec
    validate_spec(reparsed)  # raises on failure
