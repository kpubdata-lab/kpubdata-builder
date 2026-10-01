"""Dataset cards are filled from recorded provenance, and publishing needs them whole (#694)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.catalog_info import DatasetCatalogInfo
from kpubdata_builder.pipeline import card_facts
from kpubdata_builder.pipeline.card_facts import card_source, personal_information, processing_steps
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.spec.models import PiiPolicy, SchemaContract
from kpubdata_builder.stages.gold.card import missing_sections
from kpubdata_builder.stages.gold.pii import PiiMaskResult

from .test_service import _FakeClient
from .test_service_publish import LICENSED_SPEC_YAML, _blocker_codes, _readiness

_ROWS = [{"id": "1", "v": 10}, {"id": "2", "v": 20}]


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": _ROWS, "datago.apt_trade": _ROWS})
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: client)


def _card(tmp_path: Path, run_id: str, key: str = "datago.air_quality") -> dict[str, JsonValue]:
    text = (tmp_path / run_id / "gold" / key / "card.json").read_text(encoding="utf-8")
    return cast(dict[str, JsonValue], json.loads(text))


def test_a_built_card_says_where_the_data_comes_from(tmp_path: Path) -> None:
    service = _service(tmp_path)
    spec = LICENSED_SPEC_YAML.replace(
        "    dataset: air_quality\n",
        "    dataset: air_quality\n    schema:\n      rename:\n        v: value\n",
    )

    assert service.build(spec, run_id="r1").status_code == 200

    card = _card(tmp_path, "r1")
    (source,) = cast(list[dict[str, str]], card["provenance"])
    assert source["institution"] == "한국환경공단 에어코리아"
    assert source["url"].startswith("https://")
    # The declared licence first, as written; the provider's own terms beside it, so a
    # mismatch (here the provider says KOGL type 1) is visible, not hidden.
    assert source["license"].startswith("CC-BY-4.0")
    assert source["collected_at"].endswith("UTC")
    assert card["processing"] == ["Renamed v to value"]
    assert "not scanned" in cast(str, card["personal_information"])
    assert missing_sections(card) == []
    readme = (tmp_path / "r1" / "gold" / "datago.air_quality" / "README.md").read_text("utf-8")
    for heading in ("## Provenance", "## Processing", "## Personal information"):
        assert heading in readme
    assert "Attribution: 한국환경공단 에어코리아" in readme


def test_a_licence_keeps_its_original_name() -> None:
    """Negative: KOGL type 1 is never rewritten as cc-by-4.0."""
    spec = BuildSpec(
        dataset_id="d",
        title="t",
        description="d",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="jsonl", output_path="d.jsonl"),),
        license="other",
        license_name="공공누리 제1유형: 출처표시",
        license_link="https://www.kogl.or.kr/info/licenseType1.do",
        attribution="국토교통부",
    )

    source = card_source(
        spec.sources[0],
        spec=spec,
        provenance=None,
        label="apt",
        lookup=lambda _id: DatasetCatalogInfo("https://www.data.go.kr", "KOGL-1", None),
    )

    assert source.license == (
        "공공누리 제1유형: 출처표시 (https://www.kogl.or.kr/info/licenseType1.do); "
        "the provider declares: KOGL-1"
    )
    assert "cc-by" not in source.license.lower()
    assert source.institution == "국토교통부"


def test_file_and_url_sources_are_described() -> None:
    spec = BuildSpec(
        dataset_id="d",
        title="t",
        description="d",
        sources=(),
        exports=(ExportTarget(kind="jsonl", output_path="d.jsonl"),),
        attribution="me",
    )
    upload = SourceRef(kind="file", upload_id="upl_" + "a" * 32, format="csv")
    url = SourceRef(kind="url", endpoint="https://example.org/data.json?serviceKey=secret")

    # The upload id is internal: a public card does not carry it.
    assert card_source(upload, spec=spec, provenance=None, label="u").url == "uploaded file"
    described = card_source(url, spec=spec, provenance=None, label="w").url
    assert described == "https://example.org/data.json"
    assert "secret" not in described


def test_processing_and_personal_information_are_always_stated() -> None:
    bare = SourceRef(provider="p", dataset="d")
    shaped = SourceRef(
        provider="p",
        dataset="d",
        schema=SchemaContract(casts={"v": "int"}, zfill={"code": 5}, null_tokens=("-",)),
    )
    spec = BuildSpec(
        dataset_id="d",
        title="t",
        description="d",
        sources=(bare,),
        exports=(ExportTarget(kind="jsonl", output_path="d.jsonl"),),
    )

    assert processing_steps(bare, spec) == [
        "No transformation declared: values are as the source gave them."
    ]
    assert processing_steps(shaped, spec) == [
        "Treated as missing: '-'",
        "Zero-padded code to 5 characters",
        "Converted v to int",
    ]
    from dataclasses import replace

    assert personal_information(spec) == (
        "No column was declared personal information. Values were not scanned for "
        "personal information (no pii policy)."
    )
    stated = {
        mode: personal_information(
            replace(spec, pii=PiiPolicy(mode=mode, allow_columns=("contact",)))
        )
        for mode in ("block", "warn", "allow")
    }
    assert "failed the build" in stated["block"]
    assert "reported, not removed" in stated["warn"]
    # allow scans too; it does not say the data has no personal information.
    assert "Values were scanned (pii mode: allow)" in stated["allow"]
    assert "no personal information" not in stated["allow"]
    assert all("Accepted as publishable despite the scan: contact." in t for t in stated.values())


def test_personal_information_says_what_gold_masked_and_what_it_did_not() -> None:
    spec = BuildSpec(
        dataset_id="d",
        title="t",
        description="d",
        sources=(),
        exports=(ExportTarget(kind="jsonl", output_path="d.jsonl"),),
    )
    masking = PiiMaskResult(
        masked={"tel": ("kpubdata_spec",), "born": ("build_spec",)},
        unmasked={"name": ("build_spec",)},
        nulled=frozenset({"born"}),
        declared_absent=("siteTel",),
    )

    stated = personal_information(spec, masking)

    assert "masked: born (emptied), tel (replaced by [masked])" in stated
    assert "published unmasked, as the BuildSpec's gold.publish_unmasked asks: name" in stated
    assert "Declared personal information by kpubdata but not in this source: siteTel" in stated


def test_missing_sections() -> None:
    complete: dict[str, JsonValue] = {
        "provenance": [
            {"source": "s", "institution": "i", "url": "u", "license": "l", "collected_at": "c"}
        ],
        "processing": ["x"],
        "personal_information": "p",
    }
    assert missing_sections(complete) == []
    assert missing_sections({**complete, "processing": []}) == ["processing"]
    blank_institution = {
        **complete,
        "provenance": [
            {**cast(list[dict[str, str]], complete["provenance"])[0], "institution": " "}
        ],
    }
    assert missing_sections(blank_institution) == ["provenance[s].institution"]


def test_publishing_refuses_a_card_with_an_empty_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: no attribution declared → no providing institution → no publish.

    Neither the BuildSpec nor the catalog names the institution here; the catalog is
    pinned, since kpubdata declares attributions over time (kpubdata#617).
    """
    monkeypatch.setattr(
        card_facts,
        "catalog_info",
        lambda _id: DatasetCatalogInfo(source_url="u", license_type="l", attribution=None),
    )
    service = _service(tmp_path)
    no_attribution = LICENSED_SPEC_YAML.replace("attribution: 한국환경공단 에어코리아\n", "")
    service.build(no_attribution, run_id="r1")

    readiness = _readiness(service, "r1")

    assert "card_incomplete" in _blocker_codes(readiness)
    blockers = cast(list[dict[str, str]], readiness.body["blockers"])
    message = next(b["message"] for b in blockers if b["code"] == "card_incomplete")
    assert "provenance[datago.air_quality].institution" in message


def test_publishing_refuses_a_run_without_a_card(tmp_path: Path) -> None:
    """Negative: a run built before cards (no card.json) is not published as it is."""
    service = _service(tmp_path)
    service.build(LICENSED_SPEC_YAML, run_id="r1")
    card = tmp_path / "r1" / "gold" / "datago.air_quality" / "card.json"
    card.unlink()
    manifest_path = tmp_path / "r1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"] = [o for o in manifest["outputs"] if not o.endswith("card.json")]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert "card_missing" in _blocker_codes(_readiness(service, "r1"))


def test_a_complete_card_does_not_block(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.build(LICENSED_SPEC_YAML, run_id="r1")

    codes = _blocker_codes(_readiness(service, "r1"))

    assert "card_missing" not in codes and "card_incomplete" not in codes


def test_a_kaggle_package_carries_the_card(tmp_path: Path) -> None:
    service = _service(tmp_path)
    spec = LICENSED_SPEC_YAML.replace(
        "  - kind: jsonl\n    output_path: out/data.jsonl\n",
        "  - kind: jsonl\n    output_path: out/data.jsonl\n"
        "  - kind: kaggle\n    output_path: kaggle\n    options:\n      id: kpubdata/air\n",
    )

    assert service.build(spec, run_id="r1").status_code == 200

    packages = [p.parent for p in (tmp_path / "r1" / "gold").rglob("dataset-metadata.json")]
    assert packages
    for package in packages:
        assert (package / "README.md").is_file()
        assert (package / "card.json").is_file()


def test_a_composed_card_names_both_sources(tmp_path: Path) -> None:
    service = _service(tmp_path)
    spec = """\
dataset_id: combined.card
title: Combined
description: d
sources:
  - provider: datago
    dataset: air_quality
    alias: air
  - provider: datago
    dataset: apt_trade
    alias: apt
composition:
  name: joined
  join:
    left: air
    right: apt
    left_key: id
    right_key: id
exports:
  - kind: jsonl
    output_path: out/data.jsonl
license: CC-BY-4.0
attribution: 여러 기관
"""
    response = dispatch(service, "POST", "/build", {"spec": spec, "run_id": "r1"})

    assert response.status_code == 200
    card = _card(tmp_path, "r1", "joined")
    assert [s["source"] for s in cast(list[dict[str, str]], card["provenance"])] == [
        "air",
        "apt",
    ]
    assert any("Joined air and apt" in step for step in cast(list[str], card["processing"]))


def test_a_card_tells_a_column_published_unmasked(tmp_path: Path) -> None:
    """The manifest's pii_masking reaches the card: masked and unmasked columns both."""
    service = _service(tmp_path)
    spec = LICENSED_SPEC_YAML.replace(
        "    dataset: air_quality\n",
        "    dataset: air_quality\n    gold:\n      pii_columns: [id, v]\n"
        "      publish_unmasked: [v]\n",
    )

    assert service.build(spec, run_id="r1").status_code == 200

    stated = cast(str, _card(tmp_path, "r1")["personal_information"])
    assert "masked: id (replaced by [masked])" in stated
    assert "published unmasked, as the BuildSpec's gold.publish_unmasked asks: v" in stated


def test_a_hugging_face_layout_carries_the_whole_card(tmp_path: Path) -> None:
    """The layout's README is what lands at the repository root, after the Gold
    directory's: it has the same sections, under the exporter's front matter."""
    service = _service(tmp_path)
    spec = LICENSED_SPEC_YAML.replace(
        "  - kind: jsonl\n    output_path: out/data.jsonl\n",
        "  - kind: jsonl\n    output_path: out/data.jsonl\n"
        "  - kind: huggingface\n    output_path: hf\n",
    )

    assert service.build(spec, run_id="r1").status_code == 200

    layouts = [p.parent for p in (tmp_path / "r1" / "gold").rglob("dataset_infos.json")]
    assert layouts
    for layout in layouts:
        readme = (layout / "README.md").read_text(encoding="utf-8")
        assert readme.startswith("---\n") and "license:" in readme.split("---")[1]
        for heading in ("## Provenance", "## Processing", "## Personal information"):
            assert heading in readme
        assert (layout / "card.json").is_file()


def test_card_and_terms_blockers_come_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#688 and #694 both block in readiness, and neither hides the other."""
    monkeypatch.setattr(
        card_facts,
        "catalog_info",
        lambda _id: DatasetCatalogInfo(source_url="u", license_type="l", attribution=None),
    )
    client = _FakeClient({"datago.air_quality": _ROWS})
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        terms_lookup=lambda _id: "forbidden",
    )
    no_attribution = LICENSED_SPEC_YAML.replace("attribution: 한국환경공단 에어코리아\n", "")
    service.build(no_attribution, run_id="r1")

    codes = _blocker_codes(_readiness(service, "r1"))

    assert "redistribution_forbidden" in codes
    assert "card_incomplete" in codes
