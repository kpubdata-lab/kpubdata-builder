"""Versioned data checksums and separate artifact digests (#867)."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder.manifest import checksums, compute_data_checksum, compute_inputs_fingerprint
from kpubdata_builder.manifest.checksums import (
    _DOMAIN,
    _MODULUS,
    CURRENT_ALGORITHM,
    FINGERPRINT_ALGORITHM,
    LEGACY_ALGORITHM,
    LEGACY_FINGERPRINT_ALGORITHM,
    MultisetChecksum,
    algorithm_of,
    fingerprint_algorithm_of,
    multiset_checksum,
    multiset_checksum_of_jsonl,
    record_line,
    same_data,
)
from kpubdata_builder.manifest.provenance import SourceProvenance, build_source_provenance
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.warehouse.layout import content_digest

_RECORDS: list[dict[str, JsonValue]] = [
    {"id": 1, "name": "강남"},
    {"id": 2, "name": "서초", "nested": {"b": 1, "a": [1, 2]}},
    {"id": 3, "name": None},
]


def _is_probable_prime(n: int) -> bool:
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def test_the_multiset_modulus_is_prime() -> None:
    assert _MODULUS == 2**3072 - 1103717
    assert _is_probable_prime(_MODULUS)


# ------------------------------------------------------------------ v2 algorithm


def test_order_does_not_matter_but_multiplicity_does() -> None:
    forward = multiset_checksum(_RECORDS)

    assert multiset_checksum(list(reversed(_RECORDS))) == forward
    assert multiset_checksum([{"name": "강남", "id": 1}, *_RECORDS[1:]]) == forward
    assert multiset_checksum([*_RECORDS, _RECORDS[0]]) != forward
    assert multiset_checksum(_RECORDS[:2]) != forward
    assert multiset_checksum([]) != multiset_checksum([{}])


def test_a_bronze_file_gives_the_checksum_of_its_records(tmp_path: Path) -> None:
    path = tmp_path / "raw_records.jsonl"
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in _RECORDS),
        encoding="utf-8",
    )

    assert multiset_checksum_of_jsonl(path) == multiset_checksum(_RECORDS)


def test_the_accumulator_is_constant_size() -> None:
    """Streaming: folding in more records does not grow what is kept."""
    checksum = MultisetChecksum()
    for n in range(2000):
        checksum.add({"n": n})

    assert checksum.count == 2000
    assert checksum._product.bit_length() <= 3072


# ------------------------------------------------------------------ v2 is unchanged (#917)

#: v2 checksums computed by the implementation before #917 (origin/main 5d93249). The
#: faster reduction must reproduce them exactly, or existing manifests stop verifying.
_V2_GOLDEN: dict[str, tuple[list[dict[str, JsonValue]], str]] = {
    "empty": (
        [],
        "sha256:e1da2cb49d3147237c621d2ccd91e6c9222ec68fca48d9255680c1f174762abc",
    ),
    "one empty record": (
        [{}],
        "sha256:7a5e4b2dfc215c3f3165dd9528878859480b4406d640958077affda9a3adc034",
    ),
    "korean, null, nested": (
        _RECORDS,
        "sha256:cba558f24829ed796ce883731a9804b4c93c803ee23400034e27c0ddd19127c1",
    ),
    "duplicates": (
        [{"a": 1}, {"a": 1}, {"a": 2}],
        "sha256:c9fcd265f9039ab5321496ef1428189dbde31779f7aefc9314cb3ca6a3f61ff9",
    ),
    "numbers": (
        [{"i": 0, "f": 1.5, "neg": -7, "big": 10**30, "b": True}],
        "sha256:ee77c199a42bd7e8dc7a5352e2ba2a2c94a89a99e5247e7c7949f305b8c60800",
    ),
    "issue 917 shape, 1000 records": (
        [{"id": i, "name": f"서울-{i % 57}", "value": i % 137} for i in range(1000)],
        "sha256:df12fb9c36a1ebf70f4acc199ccea32fe45458b2506a6bd1a3bd2b3be8d4a5a4",
    ),
}


def _reference_v2(
    lines: list[str],
    digest: Callable[[bytes], bytes] = lambda data: hashlib.shake_256(data).digest(384),
) -> str:
    """v2 as specified: a general ``% modulus`` after every multiplication."""
    product = 1
    for line in lines:
        element = digest(_DOMAIN + line.encode("utf-8"))
        product = product * (int.from_bytes(element, "big") % _MODULUS or 1) % _MODULUS
    final = hashlib.sha256(_DOMAIN)
    final.update(len(lines).to_bytes(8, "big"))
    final.update(product.to_bytes(384, "big"))
    return f"sha256:{final.hexdigest()}"


@pytest.mark.parametrize("name", sorted(_V2_GOLDEN))
def test_v2_checksums_are_the_ones_existing_manifests_hold(name: str, tmp_path: Path) -> None:
    records, expected = _V2_GOLDEN[name]

    assert multiset_checksum(records) == expected
    assert multiset_checksum(list(reversed(records))) == expected
    assert _reference_v2([record_line(r) for r in records]) == expected
    path = tmp_path / "raw_records.jsonl"
    path.write_text("".join(record_line(r) + "\n" for r in records), encoding="utf-8")
    assert multiset_checksum_of_jsonl(path) == expected


def test_v2_matches_the_reference_on_varied_inputs() -> None:
    rng = random.Random(917)
    alphabet = 'az09 ,:{}"\\강남서초😀\u0000\n'
    for size in (0, 1, 2, 3, 7, 64, 257):
        records: list[dict[str, JsonValue]] = [
            {
                "k": "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12))),
                "n": rng.choice([None, 0, -1, 2**70, 0.1, rng.randrange(5)]),
            }
            for _ in range(size)
        ]
        records += records[: size // 3]  # duplicates
        rng.shuffle(records)
        lines = [record_line(r) for r in records]

        assert multiset_checksum(records) == _reference_v2(lines)


@pytest.mark.parametrize(
    "elements",
    [
        pytest.param([0, 0], id="zero maps to one"),
        pytest.param([_MODULUS, 5], id="the modulus reduces to zero, then one"),
        pytest.param([_MODULUS - 1] * 3, id="minus one"),
        pytest.param([_MODULUS + 1, _MODULUS + 2], id="just above the modulus"),
        pytest.param([2**3072 - 1] * 4, id="the largest digest"),
        pytest.param([2**3071, 2**3071, 3], id="powers of two"),
        # 2 * ((P + 1) // 2 + 1) == P + 3: the running product lands in
        # [P, 2**3072), so only the final reduction brings it below the modulus.
        pytest.param([2, (_MODULUS + 1) // 2 + 1], id="product between modulus and 2**3072"),
    ],
)
def test_the_reduction_matches_modulo_at_the_edges(
    elements: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Digest values the real hash practically never yields: the ones where reducing
    below 2**3072, and below the modulus, could differ from ``% modulus``."""
    lines = [f"edge-{n}" for n in range(len(elements))]
    by_input = {
        _DOMAIN + line.encode("utf-8"): value for line, value in zip(lines, elements, strict=True)
    }

    class _Fixed:
        def __init__(self, data: bytes) -> None:
            self._value = by_input[data]

        def digest(self, length: int) -> bytes:
            return self._value.to_bytes(length, "big")

    expected = _reference_v2(lines, lambda data: _Fixed(data).digest(384))
    monkeypatch.setattr(checksums.hashlib, "shake_256", _Fixed)
    checksum = MultisetChecksum()
    for line in lines:
        checksum.add_line(line)

    assert checksum.hexdigest() == expected
    assert checksum._product.bit_length() <= 3072


def test_v2_is_not_v1() -> None:
    assert multiset_checksum(_RECORDS) != compute_data_checksum(_RECORDS)


# ------------------------------------------------------------------ algorithm names


def test_a_manifest_entry_without_an_algorithm_is_legacy() -> None:
    assert algorithm_of({"data_checksum": "sha256:x"}) == LEGACY_ALGORITHM
    assert algorithm_of({"data_checksum_algorithm": CURRENT_ALGORITHM}) == CURRENT_ALGORITHM
    assert fingerprint_algorithm_of({"inputs_fingerprint": "sha256:y"}) == (
        LEGACY_FINGERPRINT_ALGORITHM
    )


def test_checksums_of_different_algorithms_are_never_equal_data() -> None:
    """Negative: equal strings from two algorithms prove nothing, and unequal ones do
    not prove a change."""
    legacy = {"data_checksum": "sha256:same"}
    current = {"data_checksum": "sha256:same", "data_checksum_algorithm": CURRENT_ALGORITHM}
    other = {"data_checksum": "sha256:other", "data_checksum_algorithm": CURRENT_ALGORITHM}

    assert same_data(legacy, current) is None
    assert same_data(legacy, {**other, "data_checksum_algorithm": LEGACY_ALGORITHM}) is False
    assert same_data(current, dict(current)) is True
    assert same_data(current, other) is False
    assert same_data(current, {}) is None


def test_the_legacy_checksum_is_still_computable() -> None:
    fetched_at = datetime(2026, 9, 30, tzinfo=timezone.utc)

    legacy = build_source_provenance(
        provider="p",
        dataset="d",
        fetched_at=fetched_at,
        records=_RECORDS,
        params={},
        checksum_algorithm=LEGACY_ALGORITHM,
    )

    assert legacy.data_checksum == compute_data_checksum(_RECORDS)
    assert legacy.data_checksum_algorithm == LEGACY_ALGORITHM
    with pytest.raises(ValueError, match="unknown checksum algorithm"):
        build_source_provenance(
            provider="p",
            dataset="d",
            fetched_at=fetched_at,
            records=_RECORDS,
            params={},
            checksum_algorithm="md5-v0",
        )


# ------------------------------------------------------------------ fingerprint


def _entry(dataset: str, checksum: str, algorithm: str | None) -> SourceProvenance:
    return SourceProvenance(
        provider="p",
        dataset=dataset,
        fetched_at="2026-09-30T00:00:00+00:00",
        record_count=1,
        data_checksum=checksum,
        data_checksum_algorithm=algorithm,
    )


def test_the_v1_fingerprint_is_the_old_formula() -> None:
    entries = [_entry("b", "sha256:2", None), _entry("a", "sha256:1", None)]
    expected = hashlib.sha256(b"p.a=sha256:1\np.b=sha256:2").hexdigest()

    assert compute_inputs_fingerprint(entries, algorithm=LEGACY_FINGERPRINT_ALGORITHM) == (
        f"sha256:{expected}"
    )


def test_the_v2_fingerprint_carries_the_algorithm_boundary() -> None:
    """The same checksum strings under different algorithms give different fingerprints."""
    as_v1 = [_entry("a", "sha256:1", LEGACY_ALGORITHM)]
    as_v2 = [_entry("a", "sha256:1", CURRENT_ALGORITHM)]

    assert compute_inputs_fingerprint(as_v1) != compute_inputs_fingerprint(as_v2)
    assert compute_inputs_fingerprint(as_v1) != compute_inputs_fingerprint(
        as_v1, algorithm=LEGACY_FINGERPRINT_ALGORITHM
    )


def test_a_fingerprint_over_mixed_algorithms_is_refused() -> None:
    mixed = [_entry("a", "sha256:1", LEGACY_ALGORITHM), _entry("b", "sha256:2", CURRENT_ALGORITHM)]

    with pytest.raises(ValueError, match="different algorithms"):
        compute_inputs_fingerprint(mixed)


# ------------------------------------------------------------------ manifests


class _Result:
    def __init__(self) -> None:
        self.items = [dict(r) for r in _RECORDS]


class _Client:
    def dataset(self, _key: str) -> _Client:
        return self

    def list(self, **_params: object) -> _Result:
        return _Result()


def _build(root: Path, run_id: str, exports: tuple[ExportTarget, ...]) -> dict[str, object]:
    spec = BuildSpec(
        dataset_id="checksum.test",
        title="Checksums",
        description="d",
        sources=(SourceRef(provider="datago", dataset="air_quality", alias="t"),),
        exports=exports,
    )
    result = run_build(spec, client=_Client(), output_root=root, run_id=run_id)
    assert result.status == "ok"
    manifest: dict[str, object] = json.loads(
        (root / run_id / "manifest.json").read_text(encoding="utf-8")
    )
    return manifest


def test_a_manifest_names_its_algorithms_and_digests_its_artifacts(tmp_path: Path) -> None:
    manifest = _build(tmp_path, "r1", (ExportTarget(kind="jsonl", output_path="d.jsonl"),))

    (entry,) = manifest["provenance"]  # type: ignore[misc]
    assert entry["data_checksum_algorithm"] == CURRENT_ALGORITHM
    assert entry["data_checksum"] == multiset_checksum(_RECORDS)
    assert manifest["inputs_fingerprint_algorithm"] == FINGERPRINT_ALGORITHM
    artifact = manifest["artifacts"]["t"]  # type: ignore[index]
    assert artifact["artifact_digest"] == content_digest(tmp_path / "r1" / "gold" / "t")
    assert artifact["artifact_writer"] == {"name": "polars", "version": pl.__version__}


def test_bytes_and_data_are_told_apart(tmp_path: Path) -> None:
    """Different files from the same records: the digest moves, the checksum does not."""
    one = _build(tmp_path, "r1", (ExportTarget(kind="jsonl", output_path="d.jsonl"),))
    two = _build(
        tmp_path,
        "r2",
        (
            ExportTarget(kind="jsonl", output_path="d.jsonl"),
            ExportTarget(kind="csv", output_path="d.csv"),
        ),
    )

    assert one["provenance"][0]["data_checksum"] == two["provenance"][0]["data_checksum"]  # type: ignore[index]
    assert one["inputs_fingerprint"] == two["inputs_fingerprint"]
    assert (
        one["artifacts"]["t"]["artifact_digest"]  # type: ignore[index]
        != two["artifacts"]["t"]["artifact_digest"]  # type: ignore[index]
    )
