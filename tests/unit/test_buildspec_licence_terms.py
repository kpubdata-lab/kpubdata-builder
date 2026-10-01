"""A BuildSpec can state its source's own licence terms (#764).

#758 made the legacy publish path record `license: other` with a name and a link,
because none of the sources grants CC BY. The BuildSpec path had nowhere to put the
name and link, so `specs/*.yaml` kept declaring `cc-by-4.0`, and declaring `other`
would have published a card that says only "other".
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from kpubdata_builder import ArtifactDataset
from kpubdata_builder.errors import SpecLoadError, ValidationError
from kpubdata_builder.exporters import HuggingFaceExporter
from kpubdata_builder.pipeline.orchestrator import _gold_package_metadata
from kpubdata_builder.spec import BuildSpec, ExportTarget, load_spec, parse_spec
from kpubdata_builder.spec.serializer import compute_spec_digest, serialize_spec_bytes
from kpubdata_builder.spec.validator import validate_spec

_ROOT = Path(__file__).resolve().parents[2]

_BASE = """dataset_id: d.x
title: T
description: D
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/d.jsonl
"""

_OTHER = """license: other
license_name: korea-public-data-unrestricted
license_link: https://www.data.go.kr/data/15126468/openapi.do
"""


def _spec(extra: str = "license: cc-by-4.0\n") -> BuildSpec:
    return parse_spec(yaml.safe_load(_BASE + extra))


def _codes(spec: BuildSpec) -> list[tuple[str, str]]:
    try:
        validate_spec(spec)
    except ValidationError as exc:
        return [(p.code, p.path) for p in exc.structured_problems or []]
    return []


class TestParsing:
    def test_both_are_optional(self) -> None:
        spec = _spec()

        assert spec.license_name is None and spec.license_link is None

    def test_they_round_trip(self) -> None:
        spec = _spec(_OTHER)

        assert spec.license == "other"
        assert spec.license_name == "korea-public-data-unrestricted"
        assert spec.license_link == "https://www.data.go.kr/data/15126468/openapi.do"

    @pytest.mark.parametrize("field", ["license_name", "license_link"])
    def test_a_non_string_is_rejected(self, field: str) -> None:
        with pytest.raises(SpecLoadError, match=f"{field} must be a string"):
            parse_spec(yaml.safe_load(_BASE + f"{field}: 42\n"))


class TestValidation:
    def test_other_with_its_terms_is_valid(self) -> None:
        assert _codes(_spec(_OTHER)) == []

    @pytest.mark.parametrize("missing", ["license_name", "license_link"])
    def test_other_without_its_terms_is_refused(self, missing: str) -> None:
        """Negative: a card saying only "other" tells a reader nothing."""
        declared = "".join(line + "\n" for line in _OTHER.splitlines() if missing not in line)

        assert ("license_other_needs_terms", missing) in _codes(_spec(declared))

    def test_terms_without_other_are_refused(self) -> None:
        """A name and link describe a licence outside the list; the id must say so."""
        declared = "license: cc-by-4.0\nlicense_name: kogl-type-1\n"

        assert ("license_terms_without_other", "license") in _codes(_spec(declared))


class TestTheDigestStaysStable:
    def test_the_keys_are_absent_when_undeclared(self) -> None:
        payload = serialize_spec_bytes(_spec())

        assert b"license_name" not in payload and b"license_link" not in payload

    def test_declaring_them_changes_the_digest(self) -> None:
        plain = compute_spec_digest(serialize_spec_bytes(_spec("license: other\n")))
        named = compute_spec_digest(serialize_spec_bytes(_spec(_OTHER)))

        assert plain != named

    def test_the_snapshot_round_trips(self) -> None:
        spec = _spec(_OTHER)

        again = parse_spec(yaml.safe_load(serialize_spec_bytes(spec)))

        assert (again.license_name, again.license_link) == (spec.license_name, spec.license_link)


class TestTheyReachThePublishedCard:
    def _card(self, spec: BuildSpec) -> str:
        artifact = ArtifactDataset.from_records(
            records=({"a": "1"},), schema={"a": "str"}, metadata=_gold_package_metadata(spec)
        )
        target = ExportTarget(kind="huggingface", output_path="out/hf", options={"format": "jsonl"})
        with tempfile.TemporaryDirectory() as tmp:
            result = HuggingFaceExporter().export(artifact, target, Path(tmp))
            return (result.output_path / "README.md").read_text(encoding="utf-8")

    def test_the_front_matter_carries_name_and_link(self) -> None:
        front_matter = yaml.safe_load(self._card(_spec(_OTHER)).split("---")[1])

        assert front_matter["license"] == "other"
        assert front_matter["license_name"] == "korea-public-data-unrestricted"
        assert front_matter["license_link"] == "https://www.data.go.kr/data/15126468/openapi.do"

    def test_a_standard_licence_card_is_unchanged(self) -> None:
        front_matter = yaml.safe_load(self._card(_spec()).split("---")[1])

        assert front_matter["license"] == "cc-by-4.0"
        assert "license_name" not in front_matter


@pytest.mark.parametrize("path", sorted((_ROOT / "specs").glob("*.yaml")), ids=lambda p: p.stem)
def test_no_shipped_spec_claims_cc_by(path: Path) -> None:
    """The three specs state their sources' terms, as the legacy configs do (#758)."""
    spec = load_spec(path)

    assert spec.license == "other"
    assert spec.license_name in {"korea-public-data-unrestricted", "kogl-type-1"}
    assert (spec.license_link or "").startswith("https://")
    assert _codes(spec) == []
