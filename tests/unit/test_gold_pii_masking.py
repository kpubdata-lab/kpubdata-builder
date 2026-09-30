"""Declared PII columns are masked in the published Gold by default (#689).

The columns are declared, never guessed: kpubdata's spec lists them in
``license.pii_columns`` (kpubdata#525), and a BuildSpec may add its own in
``sources[].gold.pii_columns``. Unmasking takes ``sources[].gold.publish_unmasked`` and
leaves a warning in the manifest. No kpubdata spec declares a column yet, so the
declaration is supplied here by a fake client whose dataset carries the same
``ref.license.pii_columns`` shape kpubdata 0.8 exposes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import polars as pl
import pytest
import yaml

from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import (
    BuildSpec,
    JsonValue,
    compute_spec_digest,
    parse_spec,
    serialize_spec_bytes,
)
from kpubdata_builder.spec.models import GoldSelection, SchemaContract
from kpubdata_builder.spec.serializer import canonical_spec_mapping
from kpubdata_builder.stages.gold.pii import (
    PII_MASK_TOKEN,
    PiiDeclarationError,
    apply_pii_masking,
    builder_column_names,
    core_pii_columns,
    declared_pii_columns,
)

# Obviously fake numbers — the shape a licence-and-permit dataset's phone column has.
_PHONES = ("010-0000-0000", "02-000-0000", "010-0000-0001")
_PHONE_PATTERN = re.compile(r"\d{2,3}-\d{3,4}-\d{4}")

_SPEC = """\
dataset_id: permits.restaurant
title: Permits
description: d
sources:
  - provider: localdata
    dataset: general_restaurant
{gold}exports:
  - kind: jsonl
    output_path: data.jsonl
  - kind: csv
    output_path: data.csv
"""


@dataclass(frozen=True)
class _License:
    pii_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Ref:
    license: _License | None = None


class _Result:
    def __init__(self) -> None:
        self.items = [
            {"mgtNo": str(i), "bplcNm": f"shop {i}", "siteTel": phone}
            for i, phone in enumerate(_PHONES)
        ] + [{"mgtNo": "9", "bplcNm": "shop 9", "siteTel": None}]


@dataclass
class _Dataset:
    ref: _Ref = field(default_factory=_Ref)

    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    """A kpubdata-shaped client whose spec declares ``pii`` as its PII columns."""

    def __init__(self, pii: tuple[str, ...] = ("siteTel",)) -> None:
        self._pii = pii

    def dataset(self, _key: str) -> _Dataset:
        return _Dataset(ref=_Ref(license=_License(pii_columns=self._pii)))


def _service(tmp_path: Path, client: _Client | None = None) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: client or _Client())


def _published_text(run: Path) -> str:
    """Every byte a publisher could pick up from Gold, as text."""
    parts = [
        pl.read_parquet(path).write_csv() if path.suffix == ".parquet" else path.read_text("utf-8")
        for path in sorted((run / "gold").rglob("*"))
        if path.is_file()
    ]
    return "\n".join(parts)


def _manifest(run: Path) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], json.loads((run / "manifest.json").read_text("utf-8")))


# ------------------------------------------------------------------ end to end


def test_a_permit_dataset_publishes_no_plaintext_phone_number(tmp_path: Path) -> None:
    """Negative (acceptance): zero plaintext phone numbers anywhere in Gold."""
    response = _service(tmp_path).build(_SPEC.format(gold=""), run_id="r1")

    assert response.status_code == 200, response.body
    run = tmp_path / "r1"
    published = _published_text(run)
    assert not _PHONE_PATTERN.search(published)
    for phone in _PHONES:
        assert phone not in published

    gold = pl.read_parquet(next(run.glob("gold/*/table.parquet")))
    # Masked, not dropped: the column and the row count are kept, and so are nulls.
    assert gold.height == 4
    assert sorted(gold["siteTel"].to_list(), key=str) == [None, *[PII_MASK_TOKEN] * 3]
    assert gold["bplcNm"].to_list() == ["shop 0", "shop 1", "shop 2", "shop 9"]

    # Silver keeps the values (#611); it is not the published table.
    silver = pl.read_parquet(next(run.glob("silver/*/table.parquet")))
    assert "010-0000-0000" in silver["siteTel"].to_list()

    manifest = _manifest(run)
    (key,) = cast(dict[str, JsonValue], manifest["pii_masking"])
    assert cast(dict[str, JsonValue], manifest["pii_masking"])[key] == {
        "token": PII_MASK_TOKEN,
        "masked": [{"column": "siteTel", "declared_by": ["kpubdata_spec"]}],
        "unmasked": [],
    }
    assert manifest["warnings"] == []


def test_unmasked_is_not_the_default(tmp_path: Path) -> None:
    """A gold block that says nothing about PII still masks every declared column."""
    spec = _SPEC.format(gold="    gold:\n      select: [mgtNo, siteTel]\n")

    assert _service(tmp_path).build(spec, run_id="r1").status_code == 200

    gold = pl.read_parquet(next((tmp_path / "r1").glob("gold/*/table.parquet")))
    assert set(gold["siteTel"].drop_nulls().to_list()) == {PII_MASK_TOKEN}


def test_publishing_unmasked_takes_an_explicit_choice_and_warns(tmp_path: Path) -> None:
    spec = _SPEC.format(gold="    gold:\n      publish_unmasked: [siteTel]\n")

    assert _service(tmp_path).build(spec, run_id="r1").status_code == 200

    run = tmp_path / "r1"
    gold = pl.read_parquet(next(run.glob("gold/*/table.parquet")))
    assert "010-0000-0000" in gold["siteTel"].to_list()
    manifest = _manifest(run)
    (key,) = cast(dict[str, JsonValue], manifest["pii_masking"])
    record = cast(dict[str, JsonValue], cast(dict[str, JsonValue], manifest["pii_masking"])[key])
    assert record["masked"] == []
    assert record["unmasked"] == [{"column": "siteTel", "declared_by": ["kpubdata_spec"]}]
    (warning,) = cast(list[str], manifest["warnings"])
    assert "siteTel" in warning and "publish_unmasked" in warning


def test_a_build_spec_can_declare_its_own_pii_column(tmp_path: Path) -> None:
    """With nothing declared by kpubdata, the BuildSpec's own declaration masks."""
    spec = _SPEC.format(gold="    gold:\n      pii_columns: [bplcNm]\n")

    response = _service(tmp_path, _Client(pii=())).build(spec, run_id="r1")

    assert response.status_code == 200, response.body
    run = tmp_path / "r1"
    gold = pl.read_parquet(next(run.glob("gold/*/table.parquet")))
    assert set(gold["bplcNm"].to_list()) == {PII_MASK_TOKEN}
    assert "shop 0" not in _published_text(run)
    record = cast(dict[str, JsonValue], _manifest(run)["pii_masking"])
    assert list(record.values())[0] == {
        "token": PII_MASK_TOKEN,
        "masked": [{"column": "bplcNm", "declared_by": ["build_spec"]}],
        "unmasked": [],
    }


def test_a_build_spec_pii_column_silver_lacks_fails_the_source(tmp_path: Path) -> None:
    """Negative: a typo must not let the real column through unmasked."""
    spec = _SPEC.format(gold="    gold:\n      pii_columns: [siteTell]\n")

    response = _service(tmp_path).build(spec, run_id="r1")

    assert response.status_code == 502
    assert "siteTell" in str(response.body.get("error"))


def test_nothing_declared_leaves_gold_and_the_manifest_as_before(tmp_path: Path) -> None:
    response = _service(tmp_path, _Client(pii=())).build(_SPEC.format(gold=""), run_id="r1")

    assert response.status_code == 200, response.body

    run = tmp_path / "r1"
    gold = pl.read_parquet(next(run.glob("gold/*/table.parquet")))
    assert "010-0000-0000" in gold["siteTel"].to_list()
    assert "pii_masking" not in _manifest(run)


def test_a_composition_masks_declared_columns_on_both_sides(tmp_path: Path) -> None:
    spec = """\
dataset_id: permits.combined
title: Permits
description: d
sources:
  - provider: localdata
    dataset: general_restaurant
    alias: a
  - provider: localdata
    dataset: general_restaurant
    alias: b
composition:
  name: combined
  join: {left: a, right: b, left_key: siteTel, right_key: siteTel}
exports:
  - kind: jsonl
    output_path: data.jsonl
"""

    response = _service(tmp_path).build(spec, run_id="r1")

    assert response.status_code == 200, response.body
    run = tmp_path / "r1"
    combined = pl.read_parquet(run / "gold" / "combined" / "table.parquet")
    # The join ran on the real key: three matches (a null key never matches), not a
    # many-to-many on the token.
    assert combined.height == 3
    assert not _PHONE_PATTERN.search(_published_text(run))
    record = cast(dict[str, JsonValue], _manifest(run)["pii_masking"])
    assert cast(dict[str, JsonValue], record["combined"])["masked"] == [
        {"column": "siteTel", "declared_by": ["kpubdata_spec"]}
    ]


# ------------------------------------------------------------------- the pieces


def test_the_declaration_is_read_from_the_public_dataset_ref() -> None:
    assert core_pii_columns(_Client().dataset("x")) == ("siteTel",)
    assert core_pii_columns(_Dataset()) == ()
    assert core_pii_columns(object()) == ()


def test_the_real_kpubdata_ref_has_the_licence_shape() -> None:
    """kpubdata 0.8's public ``Dataset.ref.license`` is where the declaration lives."""
    from kpubdata import Client, LicenseSpec

    assert LicenseSpec(pii_columns=("siteTel",)).pii_columns == ("siteTel",)
    client = Client()
    try:
        dataset = client.dataset("localdata.general_restaurant")
        assert hasattr(dataset.ref, "license")
        assert isinstance(core_pii_columns(dataset), tuple)
    finally:
        client.close()


def test_declared_fields_follow_coalesce_and_rename() -> None:
    contract = SchemaContract(
        rename={"siteTel": "phone", "rep": "representative"},
        coalesce={"rep": ("rprsvNm", "rprsvNmOld")},
    )

    assert builder_column_names(("siteTel", "rprsvNmOld", "other"), contract) == {
        "phone",
        "representative",
        "other",
    }


def test_declarations_name_their_origins_and_skip_absent_kpubdata_fields() -> None:
    declared = declared_pii_columns(
        core=("siteTel", "notInThisResponse"),
        build_spec=("siteTel", "bplcNm"),
        silver_columns=["mgtNo", "bplcNm", "siteTel"],
        contract=None,
    )

    assert declared == {"siteTel": ("kpubdata_spec", "build_spec"), "bplcNm": ("build_spec",)}
    with pytest.raises(PiiDeclarationError):
        declared_pii_columns(core=(), build_spec=("nope",), silver_columns=["a"], contract=None)


def test_masking_keeps_nulls_and_turns_other_types_into_the_token() -> None:
    frame = pl.DataFrame({"tel": ["010-0000-0000", None], "code": [1, 2], "keep": ["a", "b"]})

    masked, result = apply_pii_masking(
        frame, {"tel": ("kpubdata_spec",), "code": ("build_spec",), "gone": ("build_spec",)}
    )

    assert masked.to_dicts() == [
        {"tel": PII_MASK_TOKEN, "code": PII_MASK_TOKEN, "keep": "a"},
        {"tel": None, "code": PII_MASK_TOKEN, "keep": "b"},
    ]
    # A declared column Gold does not carry is not reported as published.
    assert set(result.masked) == {"tel", "code"}


# ------------------------------------------------------------------------- spec


def _load(gold: str) -> BuildSpec:
    return parse_spec(yaml.safe_load(_SPEC.format(gold=gold)))


def test_the_pii_fields_are_parsed_and_part_of_the_recipe() -> None:
    spec = _load("    gold:\n      pii_columns: [bplcNm]\n      publish_unmasked: [siteTel]\n")

    assert spec.sources[0].gold == GoldSelection(
        pii_columns=("bplcNm",), publish_unmasked=("siteTel",)
    )
    source = cast(list[dict[str, JsonValue]], canonical_spec_mapping(spec)["sources"])[0]
    assert source["gold"] == {
        "select": [],
        "filters": [],
        "pii_columns": ["bplcNm"],
        "publish_unmasked": ["siteTel"],
    }
    plain = replace(spec, sources=(replace(spec.sources[0], gold=GoldSelection()),))
    assert compute_spec_digest(serialize_spec_bytes(plain)) != compute_spec_digest(
        serialize_spec_bytes(spec)
    )


def test_a_gold_block_without_pii_fields_keeps_its_canonical_form() -> None:
    """Regression: earlier gold specs' canonical form, and so their digest, do not change."""
    source = cast(
        list[dict[str, JsonValue]],
        canonical_spec_mapping(_load("    gold:\n      select: [mgtNo]\n"))["sources"],
    )[0]

    assert source["gold"] == {"select": ["mgtNo"], "filters": []}


@pytest.mark.parametrize(
    "gold",
    [
        "    gold:\n      publish_unmasked: siteTel\n",
        "    gold:\n      publish_unmasked: [siteTel, siteTel]\n",
        "    gold:\n      pii_columns: [1]\n",
    ],
    ids=["not-a-list", "duplicate", "not-a-name"],
)
def test_malformed_pii_fields_are_refused(gold: str) -> None:
    with pytest.raises(SpecLoadError):
        _load(gold)
