from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.spec import (
    BuildSpec,
    ColumnNullTokens,
    ExportTarget,
    SourceRef,
    load_spec,
    parse_spec,
)
from kpubdata_builder.spec.serializer import compute_spec_digest, serialize_spec_bytes


def _valid_payload() -> dict[str, object]:
    return {
        "dataset_id": "dataset.sample",
        "title": "Sample Dataset",
        "description": "Sample description",
        "sources": [{"provider": "datago", "dataset": "air_quality"}],
        "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
    }


def test_build_spec_instantiation() -> None:
    """BuildSpec can be instantiated with only minimum required fields."""
    # Verify dataclass default constructor path hasn't broken.
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )

    assert spec.dataset_id == "dataset.sample"
    assert spec.metadata == {}
    assert spec.publish is False


def test_parse_spec_converts_nested_to_typed_objects() -> None:
    """Top-level lists become tuples of SourceRef / ExportTarget."""
    spec = parse_spec(_valid_payload())

    assert isinstance(spec, BuildSpec)
    assert isinstance(spec.sources, tuple)
    assert isinstance(spec.exports, tuple)
    assert isinstance(spec.sources[0], SourceRef)
    assert isinstance(spec.exports[0], ExportTarget)
    assert spec.sources[0].provider == "datago"
    assert spec.sources[0].dataset == "air_quality"
    assert spec.exports[0].kind == "jsonl"
    assert spec.exports[0].output_path == "out/data.jsonl"


def test_parse_spec_applies_optional_defaults() -> None:
    """metadata/publish default when omitted from YAML."""
    spec = parse_spec(_valid_payload())

    assert spec.metadata == {}
    assert spec.publish is False
    assert spec.sources[0].params == {}
    assert spec.sources[0].alias == ""
    assert spec.exports[0].options == {}


def test_parse_spec_preserves_provided_optionals() -> None:
    """Provided optional fields are carried through unchanged."""
    payload = _valid_payload()
    payload["metadata"] = {
        "owner": "kpubdata",
        "public": True,
        "tags": ["air", "quality"],
        "coverage": {"year": 2026, "note": None},
    }
    payload["publish"] = True
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources[0]["params"] = {"year": 2024}
    sources[0]["alias"] = "aq"
    exports = payload["exports"]
    assert isinstance(exports, list)
    exports[0]["options"] = {"compression": "gzip"}

    spec = parse_spec(payload)

    assert spec.metadata == {
        "owner": "kpubdata",
        "public": True,
        "tags": ["air", "quality"],
        "coverage": {"year": 2026, "note": None},
    }
    assert spec.publish is True
    assert spec.sources[0].params == {"year": 2024}
    assert spec.sources[0].alias == "aq"
    assert spec.exports[0].options == {"compression": "gzip"}


@pytest.mark.parametrize("missing", ["dataset_id", "title", "description"])
def test_parse_spec_rejects_missing_required_top_level_field(missing: str) -> None:
    """Missing dataset_id/title/description raises with the field name in the message."""
    payload = _valid_payload()
    del payload[missing]

    with pytest.raises(SpecLoadError, match=missing):
        _ = parse_spec(payload)


@pytest.mark.parametrize("empty_field", ["dataset_id", "title", "description"])
def test_parse_spec_rejects_empty_required_top_level_field(empty_field: str) -> None:
    """Empty strings are rejected for required identifying fields."""
    payload = _valid_payload()
    payload[empty_field] = ""

    with pytest.raises(SpecLoadError, match=empty_field):
        _ = parse_spec(payload)


def test_parse_spec_rejects_missing_sources() -> None:
    payload = _valid_payload()
    del payload["sources"]

    with pytest.raises(SpecLoadError, match="sources"):
        _ = parse_spec(payload)


def test_parse_spec_rejects_empty_sources() -> None:
    payload = _valid_payload()
    payload["sources"] = []

    with pytest.raises(SpecLoadError, match="sources"):
        _ = parse_spec(payload)


def test_parse_spec_accepts_a_missing_exports_key() -> None:
    """A spec without exports is valid (#703).

    Requiring an export target made the common case pay for the rare one: a local
    analysis had to declare where to publish before the build could finish. A
    warehouse build that only materialises a table is a complete job.
    """
    payload = _valid_payload()
    del payload["exports"]

    spec = parse_spec(payload)

    assert spec.exports == ()


def test_parse_spec_accepts_empty_exports() -> None:
    """An explicit empty list means the same as omitting the key."""
    payload = _valid_payload()
    payload["exports"] = []

    spec = parse_spec(payload)

    assert spec.exports == ()


def test_parse_spec_still_rejects_a_non_list_exports() -> None:
    """Optional is not the same as untyped — a string is still a mistake."""
    payload = _valid_payload()
    payload["exports"] = "jsonl"

    with pytest.raises(SpecLoadError, match="exports"):
        _ = parse_spec(payload)


@pytest.mark.parametrize("missing", ["provider", "dataset"])
def test_parse_spec_rejects_source_missing_required_field(missing: str) -> None:
    """Each source must declare provider and dataset; message includes index + field."""
    payload = _valid_payload()
    sources = payload["sources"]
    assert isinstance(sources, list)
    del sources[0][missing]

    with pytest.raises(SpecLoadError, match=rf"sources\[0\]\.{missing}"):
        _ = parse_spec(payload)


@pytest.mark.parametrize("missing", ["kind", "output_path"])
def test_parse_spec_rejects_export_missing_required_field(missing: str) -> None:
    """Each export must declare kind and output_path; message includes index + field."""
    payload = _valid_payload()
    exports = payload["exports"]
    assert isinstance(exports, list)
    del exports[0][missing]

    with pytest.raises(SpecLoadError, match=rf"exports\[0\]\.{missing}"):
        _ = parse_spec(payload)


def test_load_spec_round_trips_from_yaml(tmp_path: Path) -> None:
    """A full YAML document parses into the expected BuildSpec."""
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(
        """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
metadata:
  owner: kpubdata
publish: true
sources:
  - provider: datago
    dataset: air_quality
    params:
      year: 2024
exports:
  - kind: jsonl
    output_path: out/data.jsonl
    options:
      compression: gzip
""".strip()
        + "\n",
        encoding="utf-8",
    )

    spec = load_spec(spec_path)

    assert spec.dataset_id == "dataset.sample"
    assert spec.metadata == {"owner": "kpubdata"}
    assert spec.publish is True
    assert spec.sources[0].params == {"year": 2024}
    assert spec.exports[0].options == {"compression": "gzip"}


def test_load_spec_rejects_empty_yaml(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text("", encoding="utf-8")

    with pytest.raises(SpecLoadError):
        _ = load_spec(spec_path)


def test_build_spec_from_yaml_classmethod(tmp_path: Path) -> None:
    """Deprecated alias yields same result as load_spec with DeprecationWarning.

    The alias is still public API, so it stays tested; asserting the warning
    (1) keeps it from leaking across the suite and (2) pins the contract so
    this test breaks first at removal time.
    """
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(
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
        + "\n",
        encoding="utf-8",
    )

    with pytest.warns(DeprecationWarning, match="use load_spec"):
        spec = BuildSpec.from_yaml(spec_path)

    assert spec.dataset_id == "dataset.sample"
    assert spec.sources[0].provider == "datago"


def test_parse_spec_rejects_removed_transforms_field() -> None:
    """transforms field removed (#438). Explicit error on key (prevent silent ignore)."""
    payload = _valid_payload()
    payload["transforms"] = ["normalize"]
    with pytest.raises(SpecLoadError, match="transforms"):
        _ = parse_spec(payload)


def test_parse_spec_rejects_removed_normalization_mode_field() -> None:
    """sources[].normalization_mode field removed (#438). Error on key."""
    payload = _valid_payload()
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources[0]["normalization_mode"] = "raw"
    with pytest.raises(SpecLoadError, match="normalization_mode"):
        _ = parse_spec(payload)


def test_parse_spec_rejects_removed_top_level_normalization_mode_field() -> None:
    """Top-level normalization_mode also not canonical BuildSpec field, reject (#485)."""
    payload = _valid_payload()
    payload["normalization_mode"] = "canonical"

    with pytest.raises(SpecLoadError, match="normalization_mode"):
        _ = parse_spec(payload)


def _spec_with_schema(schema: dict[str, object]) -> BuildSpec:
    payload = _valid_payload()
    payload["sources"] = [{"provider": "datago", "dataset": "air_quality", "schema": schema}]
    return parse_spec(payload)


def test_coalesce_and_zfill_round_trip_through_the_loader() -> None:
    """#620 declaration parses to SchemaContract."""
    spec = _spec_with_schema(
        {
            "coalesce": {"move_meter": ["이동거리", "이동거리(M)"]},
            "zfill": {"station_no": 5},
            "casts": {"ym": "year_month"},
        }
    )

    schema = spec.sources[0].schema
    assert schema is not None
    assert schema.coalesce == {"move_meter": ("이동거리", "이동거리(M)")}
    assert schema.zfill == {"station_no": 5}
    assert schema.casts == {"ym": "year_month"}


def test_zfill_width_must_be_an_integer() -> None:
    with pytest.raises(SpecLoadError):
        _spec_with_schema({"zfill": {"station_no": "5"}})


def test_coalesce_and_zfill_move_the_spec_digest() -> None:
    """Missing declaration from canonical mapping → digest unchanged even if transform rule changes.

    In that state, claiming R1's "same recipe means same output" leaves the
    rules that actually produced Silver outside the recipe.
    """
    base = _spec_with_schema({"casts": {"amount": "int"}})
    with_coalesce = _spec_with_schema({"casts": {"amount": "int"}, "coalesce": {"m": ["a", "b"]}})
    with_zfill = _spec_with_schema({"casts": {"amount": "int"}, "zfill": {"code": 5}})
    with_year_month = _spec_with_schema({"casts": {"amount": "year_month"}})

    digests = {
        compute_spec_digest(serialize_spec_bytes(spec))
        for spec in (base, with_coalesce, with_zfill, with_year_month)
    }
    assert len(digests) == 4


def test_coalesce_candidate_order_is_part_of_the_recipe() -> None:
    """Candidate order changes which value wins."""
    forward = _spec_with_schema({"coalesce": {"m": ["a", "b"]}})
    reversed_ = _spec_with_schema({"coalesce": {"m": ["b", "a"]}})

    assert compute_spec_digest(serialize_spec_bytes(forward)) != compute_spec_digest(
        serialize_spec_bytes(reversed_)
    )


def test_column_null_tokens_round_trip_and_move_the_digest() -> None:
    """#623 — per-column missing declaration enters recipe identity."""
    base = _spec_with_schema({"null_tokens": ["TOKEN"]})
    scoped = _spec_with_schema(
        {"null_tokens": ["TOKEN"], "column_null_tokens": {"gender": ["", "TOKEN"]}}
    )

    schema = scoped.sources[0].schema
    assert schema is not None
    rule = schema.column_null_tokens["gender"]
    assert rule.tokens == ("", "TOKEN")
    assert rule.on_absent == "error"
    assert compute_spec_digest(serialize_spec_bytes(base)) != compute_spec_digest(
        serialize_spec_bytes(scoped)
    )


def test_column_null_tokens_accepts_the_expanded_form() -> None:
    """#623 — accept both list shorthand and expanded form with on_absent."""
    spec = _spec_with_schema(
        {"column_null_tokens": {"gender": {"tokens": [""], "on_absent": "ignore"}}}
    )

    schema = spec.sources[0].schema
    assert schema is not None
    assert schema.column_null_tokens["gender"] == ColumnNullTokens(tokens=("",), on_absent="ignore")


def test_on_absent_is_part_of_the_recipe() -> None:
    """Which column is optional changes output even from same source."""
    strict = _spec_with_schema({"column_null_tokens": {"gender": [""]}})
    lenient = _spec_with_schema(
        {"column_null_tokens": {"gender": {"tokens": [""], "on_absent": "ignore"}}}
    )

    assert compute_spec_digest(serialize_spec_bytes(strict)) != compute_spec_digest(
        serialize_spec_bytes(lenient)
    )


def test_unknown_key_in_column_null_tokens_is_rejected() -> None:
    with pytest.raises(SpecLoadError):
        _spec_with_schema({"column_null_tokens": {"gender": {"token": [""]}}})
