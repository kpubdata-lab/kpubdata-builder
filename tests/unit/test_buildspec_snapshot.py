"""Per-run canonical BuildSpec snapshot contract verification (#487)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

import kpubdata_builder.spec.serializer as serializer
from kpubdata_builder.spec import (
    BuildSpec,
    ExportTarget,
    SourceRef,
    compute_spec_digest,
    parse_spec,
    serialize_spec,
    serialize_spec_bytes,
    write_buildspec_snapshot,
)
from kpubdata_builder.spec.models import PiiPolicy, QualityPolicy, SchemaContract, SplitSpec


def _complete_spec() -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.contract",
        title="계약 데이터",
        description="모든 optional field",
        sources=(
            SourceRef(
                provider="datago",
                dataset="air_quality",
                params={"nested": {"z": 1, "a": [True, None]}, "page": 1},
                alias="air",
                schema=SchemaContract(
                    required=("id",), dtypes={"id": "string"}, casts={"value": "float64"}
                ),
            ),
        ),
        exports=(
            ExportTarget(
                kind="jsonl",
                output_path="out/data.jsonl",
                options={"indent": 2, "flags": ["a", "b"]},
            ),
        ),
        metadata={"coverage": {"year": 2026, "note": None}, "public": True},
        publish=False,
        splits=SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, seed=7),
        pii=PiiPolicy(mode="warn", allow_columns=("contact_hint",)),
        license="CC-BY-4.0",
        quality=QualityPolicy(
            max_duplicate_rate=0.01, max_null_ratio={"value": 0.05}, min_rows=100
        ),
    )


def _parse_yaml(text: str) -> BuildSpec:
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict)
    return parse_spec(cast(dict[str, object], raw))


def test_minimal_and_complete_spec_round_trip() -> None:
    minimal = BuildSpec(
        dataset_id="d",
        title="t",
        description="desc",
        sources=(SourceRef(provider="p", dataset="s"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )

    assert _parse_yaml(serialize_spec(minimal)) == minimal
    assert _parse_yaml(serialize_spec(_complete_spec())) == _complete_spec()


def test_serialization_is_deterministic_for_nested_mapping_order() -> None:
    first = _complete_spec()
    second = replace(first, metadata={"public": True, "coverage": {"note": None, "year": 2026}})

    assert first == second
    assert serialize_spec_bytes(first) == serialize_spec_bytes(second)
    assert compute_spec_digest(serialize_spec_bytes(first)) == compute_spec_digest(
        serialize_spec_bytes(second)
    )


def test_secret_redaction_is_exact_and_recursive() -> None:
    spec = _complete_spec()
    source = spec.sources[0]
    redacted = replace(
        spec,
        sources=(
            replace(
                source,
                params={
                    "api_key": "plain-api-secret",
                    "apiKey": "plain-camel-api-secret",
                    "service_key": "plain-service-secret",
                    "nested": [
                        {
                            "serviceKey": "plain-camel-service-secret",
                            "access_token": "plain-access-token",
                        },
                        {"refresh_token": "plain-refresh-token"},
                    ],
                    "monkey": "keep-me",
                    "token_count": 3,
                },
            ),
        ),
        exports=(
            replace(
                spec.exports[0],
                options={
                    "client_secret": "plain-client-secret",
                    "Authorization": "plain-authorization",
                },
            ),
        ),
        metadata={
            "password": "plain-password",
            "HF_TOKEN": "plain-hf-token",
            "KAGGLE_KEY": "plain-kaggle-key",
            "KPUBDATA_DATAGO_API_KEY": "plain-datago-key",
            "KPUBDATA_BUILDER_API_KEY": "plain-builder-key",
            "bearer_token": "plain-bearer-token",
            "key": "business-key",
        },
    )

    text = serialize_spec(redacted)

    assert "plain-api-secret" not in text
    plaintext_secrets = (
        "plain-api-secret",
        "plain-camel-api-secret",
        "plain-service-secret",
        "plain-camel-service-secret",
        "plain-access-token",
        "plain-refresh-token",
        "plain-client-secret",
        "plain-authorization",
        "plain-password",
        "plain-hf-token",
        "plain-kaggle-key",
        "plain-datago-key",
        "plain-builder-key",
        "plain-bearer-token",
    )
    assert all(secret not in text for secret in plaintext_secrets)
    assert text.count("<redacted>") == len(plaintext_secrets)
    assert "keep-me" in text
    assert "business-key" in text
    assert "token_count: 3" in text
    # credential-bearing snapshot intentionally does not provide identical round-trip with original.
    assert _parse_yaml(text) != redacted


def test_specs_differing_only_by_secret_share_redacted_snapshot_digest() -> None:
    first = replace(
        _complete_spec(),
        metadata={"api_key": "first-secret", "label": "same-public-value"},
    )
    second = replace(
        first,
        metadata={"api_key": "second-secret", "label": "same-public-value"},
    )

    first_payload = serialize_spec_bytes(first)
    second_payload = serialize_spec_bytes(second)

    assert first_payload == second_payload
    assert compute_spec_digest(first_payload) == compute_spec_digest(second_payload)


def test_snapshot_bytes_and_digest_are_identical(tmp_path: Path) -> None:
    path, digest = write_buildspec_snapshot(_complete_spec(), output_root=tmp_path, run_id="r1")
    payload = path.read_bytes()

    assert path == tmp_path / "r1" / "buildspec.yaml"
    assert payload == serialize_spec_bytes(_complete_spec())
    assert digest == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    changed_payload = serialize_spec_bytes(replace(_complete_spec(), title="변경된 제목"))
    assert compute_spec_digest(changed_payload) != digest


def test_snapshot_write_is_atomic_and_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(_source: str, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(serializer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_buildspec_snapshot(_complete_spec(), output_root=tmp_path, run_id="r1")

    assert not (tmp_path / "r1" / "buildspec.yaml").exists()
    assert list((tmp_path / "r1").glob(".buildspec_*.tmp")) == []


def test_snapshot_rejects_unsafe_run_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        write_buildspec_snapshot(_complete_spec(), output_root=tmp_path, run_id="../escape")


def test_spec_digest_changes_when_transform_rules_change() -> None:
    """Transform rules reflected in spec digest (#611).

    R1 asserts "same recipe produces same output". If rename/derived are not
    included in digest, changing transform rules produces the same digest,
    putting the actual Silver-generating rules outside the recipe.
    """
    from kpubdata_builder.spec.models import DerivedColumn

    base = _complete_spec()
    renamed = replace(
        base,
        sources=(replace(base.sources[0], schema=SchemaContract(rename={"sggCd": "district"})),),
    )
    derived = replace(
        base,
        sources=(
            replace(
                base.sources[0],
                schema=SchemaContract(
                    derived=(
                        DerivedColumn(name="deal_date", kind="date_parts", columns=("y", "m", "d")),
                    )
                ),
            ),
        ),
    )

    assert compute_spec_digest(serialize_spec_bytes(renamed)) != compute_spec_digest(
        serialize_spec_bytes(base)
    )
    assert compute_spec_digest(serialize_spec_bytes(derived)) != compute_spec_digest(
        serialize_spec_bytes(renamed)
    )


def _spec_with_secret_named_columns() -> BuildSpec:
    """spec where column name overlaps credential key (#623).

    Public datasets actually contain columns named like ``token`` or ``password``.
    """
    from kpubdata_builder.spec.models import ColumnNullTokens

    spec = _complete_spec()
    source = spec.sources[0]
    return replace(
        spec,
        sources=(
            replace(
                source,
                schema=SchemaContract(
                    required=("token",),
                    dtypes={"token": "string"},
                    read_as={"token": "str"},
                    rename={"password": "pw"},
                    zfill={"secret": 5},
                    coalesce={"api_key": ("apikey", "api_key")},
                    column_null_tokens={
                        "token": ColumnNullTokens(tokens=("", "-"), on_absent="error"),
                    },
                ),
            ),
        ),
    )


def test_column_names_that_look_like_credentials_are_not_redacted() -> None:
    # Column name is not credential. If redaction applied, snapshot expects
    # type receives "<redacted>" string instead so cannot be parsed again.
    spec = _spec_with_secret_named_columns()

    parsed = _parse_yaml(serialize_spec(spec))

    assert parsed == spec


def test_specs_differing_only_by_column_null_tokens_get_different_digests() -> None:
    # If redaction makes entire token list "<redacted>",
    # two specs declaring different null markers have same digest — recipe identity breaks.
    from kpubdata_builder.spec.models import ColumnNullTokens

    first = _spec_with_secret_named_columns()
    source = first.sources[0]
    assert source.schema is not None
    second = replace(
        first,
        sources=(
            replace(
                source,
                schema=replace(
                    source.schema,
                    column_null_tokens={
                        "token": ColumnNullTokens(tokens=("N/A",), on_absent="error"),
                    },
                ),
            ),
        ),
    )

    assert compute_spec_digest(serialize_spec_bytes(first)) != compute_spec_digest(
        serialize_spec_bytes(second)
    )


def test_credential_params_are_still_redacted_alongside_column_names() -> None:
    # Removing redaction from structural mapping must not weaken redaction of free-form params
    # .
    spec = _spec_with_secret_named_columns()
    source = spec.sources[0]
    with_secret = replace(spec, sources=(replace(source, params={"serviceKey": "plain-secret"}),))

    text = serialize_spec(with_secret)

    assert "plain-secret" not in text
    assert serializer.REDACTED_VALUE in text


class TestDigestStabilityForUnusedFields:
    """recipe identity doesn't change due to unused features (#611 follow-up).

    spec_digest is recipe identity exposed in manifest, BuildIndex, and
    ``GET /datasets``. If empty keys land in canonical mapping each time Silver
    transform declarations are added, existing specs that don't use that feature
    have their digests change entirely — upgrade time "is it same recipe"
    comparisons break.
    """

    OPTIONAL_KEYS = (
        "rename",
        "read_as",
        "null_tokens",
        "column_null_tokens",
        "coalesce",
        "zfill",
        "derived",
    )

    @staticmethod
    def _schema_mapping(spec: BuildSpec) -> dict[str, object]:
        from kpubdata_builder.spec.serializer import canonical_spec_mapping

        sources = cast(list[dict[str, object]], canonical_spec_mapping(spec)["sources"])
        return cast(dict[str, object], sources[0]["schema"])

    def test_an_unused_transform_field_is_not_emitted(self) -> None:
        spec = replace(
            _complete_spec(),
            sources=(
                replace(
                    _complete_spec().sources[0],
                    schema=SchemaContract(required=("id",), casts={"value": "float64"}),
                ),
            ),
        )

        emitted = self._schema_mapping(spec)

        for key in self.OPTIONAL_KEYS:
            assert key not in emitted, f"unused {key!r} would move the digest of every old spec"
        assert set(emitted) == {"required", "dtypes", "casts"}

    def test_a_declared_transform_field_is_emitted(self) -> None:
        # Declared values must remain in recipe — else transform rules
        # change but digest stays same.
        spec = replace(
            _complete_spec(),
            sources=(
                replace(
                    _complete_spec().sources[0],
                    schema=SchemaContract(rename={"sggCd": "district"}),
                ),
            ),
        )

        assert self._schema_mapping(spec)["rename"] == {"sggCd": "district"}

    def test_changing_a_declared_transform_moves_the_digest(self) -> None:
        def _spec(rename: dict[str, str]) -> BuildSpec:
            base = _complete_spec()
            return replace(
                base,
                sources=(replace(base.sources[0], schema=SchemaContract(rename=rename)),),
            )

        assert compute_spec_digest(serialize_spec_bytes(_spec({"a": "x"}))) != compute_spec_digest(
            serialize_spec_bytes(_spec({"a": "y"}))
        )

    def test_an_empty_declaration_matches_no_declaration(self) -> None:
        base = _complete_spec()
        without = replace(
            base, sources=(replace(base.sources[0], schema=SchemaContract(required=("id",))),)
        )
        with_empty = replace(
            base,
            sources=(
                replace(
                    base.sources[0],
                    schema=SchemaContract(required=("id",), rename={}, zfill={}, derived=()),
                ),
            ),
        )

        assert compute_spec_digest(serialize_spec_bytes(without)) == compute_spec_digest(
            serialize_spec_bytes(with_empty)
        )
