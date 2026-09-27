"""BuildSpec ``attribution`` field (ADR 0018).

공공누리는 제1~4유형 모두 출처표시를 **의무**로 둔다. ``license`` 식별자만으로는
그 의무를 채울 수 없다 — 기관명·유형·원문 URL 이 함께 있어야 성립하기 때문이다.

레거시 publish config 는 ``card.attribution`` 으로 이걸 담고 있었지만 BuildSpec
에는 대응 개념이 없었고, 그래서 정식 경로로 게시하면 출처표시가 통째로 빠졌다.
20개 config 중 3개만 attribution 을 갖고 있다는 점도 함께 기록해 둔다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from kpubdata_builder import ArtifactDataset
from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.exporters import HuggingFaceExporter
from kpubdata_builder.spec import ExportTarget, parse_spec
from kpubdata_builder.spec.serializer import compute_spec_digest, serialize_spec_bytes

_BASE = """dataset_id: d.x
title: T
description: D
license: cc-by-4.0
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/d.jsonl
"""

_ATTRIBUTION = "본 저작물은 '국토교통부'에서 공공누리 제1유형으로 개방한 자료를 이용하였습니다."


def _spec(extra: str = "") -> object:
    return parse_spec(yaml.safe_load(_BASE + extra))


class TestParsing:
    def test_it_is_optional(self) -> None:
        assert _spec().attribution is None  # type: ignore[attr-defined]

    def test_it_round_trips(self) -> None:
        spec = _spec(f'attribution: "{_ATTRIBUTION}"\n')

        assert spec.attribution == _ATTRIBUTION  # type: ignore[attr-defined]

    def test_a_non_string_is_rejected(self) -> None:
        with pytest.raises(SpecLoadError, match="attribution must be a string"):
            _ = parse_spec(yaml.safe_load(_BASE + "attribution: 42\n"))


class TestTheDigestStaysStable:
    """``spec_digest`` is recipe identity. New field must not change existing spec identity."""

    def test_a_spec_without_attribution_is_unchanged(self) -> None:
        first = compute_spec_digest(serialize_spec_bytes(_spec()))  # type: ignore[arg-type]
        second = compute_spec_digest(serialize_spec_bytes(_spec()))  # type: ignore[arg-type]

        assert first == second

    def test_the_key_is_absent_when_undeclared(self) -> None:
        assert b"attribution" not in serialize_spec_bytes(_spec())  # type: ignore[arg-type]

    def test_declaring_it_changes_the_digest(self) -> None:
        """Declaring makes different recipe — publication legal notices change."""
        plain = compute_spec_digest(serialize_spec_bytes(_spec()))  # type: ignore[arg-type]
        attributed = compute_spec_digest(
            serialize_spec_bytes(_spec(f'attribution: "{_ATTRIBUTION}"\n'))  # type: ignore[arg-type]
        )

        assert plain != attributed


class TestItReachesThePublishedCard:
    """Front matter license alone does not fulfill attribution obligation."""

    def _card(self, metadata: dict[str, object]) -> str:
        artifact = ArtifactDataset(records=({"a": "1"},), schema={"a": "str"}, metadata=metadata)
        target = ExportTarget(kind="huggingface", output_path="out/hf", options={"format": "jsonl"})
        with tempfile.TemporaryDirectory() as tmp:
            result = HuggingFaceExporter().export(artifact, target, Path(tmp))
            return (result.output_path / "README.md").read_text(encoding="utf-8")

    def test_the_notice_is_in_the_body(self) -> None:
        card = self._card(
            {
                "title": "T",
                "dataset_id": "k/t",
                "license": "cc-by-4.0",
                "attribution": _ATTRIBUTION,
            }
        )

        assert _ATTRIBUTION in card

    def test_a_card_without_it_is_unchanged(self) -> None:
        card = self._card({"title": "T", "dataset_id": "k/t", "license": "cc-by-4.0"})

        assert "attribution" not in card
        assert card.rstrip().endswith("공공데이터포털 (data.go.kr)")
