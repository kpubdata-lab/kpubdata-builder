"""Column meaning is kept apart from storage and wire encoding (#813, ADR 0019).

These pin three things: a hint can never change how a column is stored or sent; hints
from several sources resolve by origin, each part on its own; and the contract accepts
both the metadata a pre-1.41.0 Builder sends and the metadata with hints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import yaml

from kpubdata_builder.tabular.polars_engine import infer_schema
from kpubdata_builder.tabular.semantics import (
    ORIGIN_PRIORITY,
    ColumnSemantics,
    DisplayHint,
    SemanticHint,
    UnitHint,
    from_field_descriptor,
    resolve,
    semantics_json,
    with_semantics,
)
from kpubdata_builder.tabular.wire import column_meta, encode_rows

from ._openapi import validate

_CONTRACT: dict[str, Any] = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)


def _schema(name: str) -> dict[str, Any]:
    return {"$ref": f"#/components/schemas/{name}"}


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            # A legal-dong code: leading zeros are the value.
            "code": ["0011010100", None, "4113510900"],
            "amount": [Decimal("12.50"), None, Decimal("0.10")],
            "deal_date": [date(2025, 1, 2), None, date(2024, 12, 31)],
            "count": [1, None, 3],
        },
        schema={
            "code": pl.String,
            "amount": pl.Decimal(10, 2),
            "deal_date": pl.Date,
            "count": pl.Int64,
        },
    )


# Deliberately wrong hints: a code called a measure, a date called a code. If a hint could
# steer storage or encoding, these are the ones that would show it.
_MISLEADING = {
    "code": ColumnSemantics(
        semantic=SemanticHint(kind="measure", origin="user_annotation"),
        unit=UnitHint(name="KRW", scale=1000, origin="user_annotation"),
    ),
    "amount": ColumnSemantics(
        semantic=SemanticHint(kind="measure", origin="core_spec"),
        display=DisplayHint(origin="core_spec", label="Amount", format="#,##0.00"),
        unit=UnitHint(name="KRW", scale=1000, origin="catalog"),
    ),
    "deal_date": ColumnSemantics(semantic=SemanticHint(kind="code", origin="engine_inferred")),
    "count": ColumnSemantics(),
}


def test_hints_do_not_change_storage_encoding_or_values() -> None:
    frame = _frame()
    columns = infer_schema(frame).columns
    plain = column_meta(columns)

    described = with_semantics(plain, _MISLEADING)

    for before, after in zip(plain, described, strict=True):
        assert {k: after[k] for k in before} == before
    rows = json.loads(json.dumps(list(encode_rows(frame.to_dicts(), columns))))
    assert rows == [
        {"code": "0011010100", "amount": "12.50", "deal_date": "2025-01-02", "count": 1},
        {"code": None, "amount": None, "deal_date": None, "count": None},
        {"code": "4113510900", "amount": "0.10", "deal_date": "2024-12-31", "count": 3},
    ]
    assert {c["name"]: c["wire_encoding"] for c in described} == {
        "code": "string",
        "amount": "decimal_string",
        "deal_date": "string",
        "count": "number",
    }


def test_an_undescribed_column_gets_no_new_keys() -> None:
    plain = column_meta(infer_schema(_frame()).columns)

    assert with_semantics(plain, None) == plain
    assert with_semantics(plain, {"count": ColumnSemantics()}) == plain
    described = with_semantics(plain, _MISLEADING)
    assert described[3] == plain[3]
    assert set(described[2]) == set(plain[2]) | {"semantic"}


def test_each_part_is_taken_from_the_highest_origin_on_its_own() -> None:
    engine = ColumnSemantics(
        semantic=SemanticHint(kind="measure", origin="engine_inferred"),
        display=DisplayHint(origin="engine_inferred", label="col_7"),
    )
    catalog = ColumnSemantics(unit=UnitHint(name="EA", origin="catalog"))
    core = ColumnSemantics(
        semantic=SemanticHint(kind="code", origin="core_spec"),
        display=DisplayHint(origin="core_spec", label="Legal-dong code"),
        unit=UnitHint(name="KRW", origin="core_spec"),
    )
    user = ColumnSemantics(display=DisplayHint(origin="user_annotation", label="Dong"))

    # The order layers are given in does not matter; only their origins do.
    for layers in ([engine, catalog, core, user], [user, core, catalog, engine]):
        result = resolve(layers)
        assert result.semantic == SemanticHint(kind="code", origin="core_spec")
        assert result.display == DisplayHint(origin="user_annotation", label="Dong")
        assert result.unit == UnitHint(name="KRW", origin="core_spec")


def test_origin_priority_is_the_documented_order() -> None:
    assert ORIGIN_PRIORITY == ("user_annotation", "core_spec", "catalog", "engine_inferred")
    enum = _CONTRACT["components"]["schemas"]["ColumnMetaOrigin"]["enum"]
    assert tuple(enum) == ORIGIN_PRIORITY


@dataclass
class _Constraints:
    format: str | None = None


@dataclass
class _Field:
    """The attributes of kpubdata's FieldDescriptor this mapping reads."""

    name: str
    title: str | None = None
    description: str | None = None
    type: str | None = None
    nullable: bool | None = None
    constraints: _Constraints | None = None


@pytest.mark.parametrize(
    ("fmt", "kind"),
    [("YYYYMM", "period"), ("date", "date"), ("YYYY-MM-DD", "date"), ("url", None), (None, None)],
)
def test_core_field_descriptor_maps_to_display_and_kind(fmt: str | None, kind: str | None) -> None:
    field = _Field(
        name="DEAL_YMD",
        title="Deal month",
        description="Month of the contract",
        type="integer",  # the source's type never becomes the storage type
        constraints=_Constraints(format=fmt),
    )

    result = from_field_descriptor(field)

    assert result.display == DisplayHint(
        origin="core_spec", label="Deal month", description="Month of the contract", format=fmt
    )
    assert result.semantic == (None if kind is None else SemanticHint(kind, "core_spec"))
    assert result.unit is None


def test_an_empty_field_descriptor_says_nothing() -> None:
    assert from_field_descriptor(_Field(name="x", title=" ")).is_empty()


def test_the_real_core_field_descriptor_is_read_the_same_way() -> None:
    models = pytest.importorskip("kpubdata.core.models")
    field = models.FieldDescriptor(
        name="DEAL_YMD",
        title="Deal month",
        constraints=models.FieldConstraints(format="YYYYMM"),
    )
    assert semantics_json(from_field_descriptor(field)) == {
        "semantic": {"kind": "period", "origin": "core_spec"},
        "display": {"origin": "core_spec", "label": "Deal month", "format": "YYYYMM"},
    }


def test_the_contract_accepts_metadata_with_and_without_hints() -> None:
    plain = column_meta(infer_schema(_frame()).columns)
    described = with_semantics(plain, _MISLEADING)

    for entry in [*plain, *described]:
        assert validate(entry, _schema("ColumnWireInfo"), _CONTRACT) == []
    silver = {"name": "code", "dtype": "String", "nullable": True, "unique_count": 3}
    assert validate(silver, _schema("SilverColumnInfo"), _CONTRACT) == []
    assert validate({**silver, **described[0]}, _schema("SilverColumnInfo"), _CONTRACT) == []


def test_the_contract_keeps_semantic_kind_open_and_required_fields_typed() -> None:
    """An unknown kind is not a parse error; a missing origin or a numeric kind is."""
    entry = {"name": "c", "logical_type": "string", "wire_encoding": "string"}
    newer = {**entry, "semantic": {"kind": "coordinate", "origin": "catalog"}}
    assert validate(newer, _schema("ColumnWireInfo"), _CONTRACT) == []

    no_origin = {**entry, "unit": {"name": "KRW"}}
    wrong_type = {**entry, "semantic": {"kind": 3, "origin": "catalog"}}
    assert validate(no_origin, _schema("ColumnWireInfo"), _CONTRACT)
    assert validate(wrong_type, _schema("ColumnWireInfo"), _CONTRACT)
