"""Gold decides the published columns and rows; Silver keeps everything (#659).

ADR 0018 option C (owner decision D2, 2026-09-30): ``filters`` and column selection live
in Gold. Silver keeps every column and row of Bronze, and quality is measured there, as
#611 decided.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import polars as pl
import pytest
import yaml

from kpubdata_builder.errors import SpecLoadError, ValidationError
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import BuildSpec, JsonValue, compute_spec_digest, parse_spec
from kpubdata_builder.spec.models import CompositionSpec, GoldFilter, GoldSelection, JoinSpec
from kpubdata_builder.spec.serializer import canonical_spec_mapping, serialize_spec_bytes
from kpubdata_builder.spec.validator import validate_spec
from kpubdata_builder.stages.gold.select import GoldSelectionError, apply_gold_selection

_BASE = """\
dataset_id: gold.table
title: Gold
description: d
sources:
  - provider: datago
    dataset: air_quality
{gold}exports:
  - kind: jsonl
    output_path: data.jsonl
"""
_GOLD = """\
    gold:
      select: [id, amount]
      filters:
        - column: amount
          op: gt
          value: 0
"""


class _Result:
    def __init__(self) -> None:
        self.items = [
            {"id": "1", "amount": 5, "secret_note": "a"},
            {"id": "2", "amount": 0, "secret_note": "b"},
            {"id": "3", "amount": None, "secret_note": "c"},
            {"id": "4", "amount": 7, "secret_note": "d"},
        ]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


def _load(text: str) -> BuildSpec:
    return parse_spec(yaml.safe_load(text))


def _parse(gold: str) -> BuildSpec:
    return _load(_BASE.format(gold=gold))


# --------------------------------------------------------------------------- spec


def test_the_selection_is_parsed() -> None:
    spec = _parse(_GOLD)

    assert spec.sources[0].gold == GoldSelection(
        select=("id", "amount"), filters=(GoldFilter("amount", "gt", 0),)
    )


@pytest.mark.parametrize(
    "gold",
    [
        "    gold:\n      pick: [id]\n",
        "    gold:\n      select: [id, id]\n",
        "    gold:\n      filters:\n        - {column: amount, op: like, value: 1}\n",
        "    gold:\n      filters:\n        - {column: amount, op: in, value: 1}\n",
        "    gold:\n      filters:\n        - {column: amount, op: not_null, value: 1}\n",
        "    gold:\n      filters:\n        - {column: amount, op: gt}\n",
        "    gold:\n      filters:\n        - {column: amount, op: gt, value: 1, expr: x}\n",
    ],
    ids=[
        "unknown-key",
        "duplicate",
        "unknown-op",
        "in-scalar",
        "not-null-value",
        "no-value",
        "expr",
    ],
)
def test_malformed_selections_are_refused(gold: str) -> None:
    """Negative: a filter is data — never an expression, never a guess."""
    with pytest.raises(SpecLoadError):
        _load(_BASE.format(gold=gold))


def test_a_selection_with_composition_is_refused() -> None:
    base = _parse(_GOLD)
    spec = replace(
        base,
        sources=(
            replace(base.sources[0], alias="a"),
            replace(base.sources[0], alias="b", gold=None),
        ),
        composition=CompositionSpec(
            name="combined", join=JoinSpec(left="a", right="b", left_key="id", right_key="id")
        ),
    )
    with pytest.raises(ValidationError) as exc:
        validate_spec(spec)
    assert "gold_selection_with_composition" in [p.code for p in exc.value.structured_problems]


def test_a_spec_without_gold_keeps_its_digest() -> None:
    """Regression: existing specs' canonical form, and so their digest, do not change."""
    spec = _parse("")
    mapping = canonical_spec_mapping(spec)

    assert "gold" not in cast(list[dict[str, JsonValue]], mapping["sources"])[0]


def test_the_selection_is_part_of_the_recipe() -> None:
    plain, selected = _parse(""), _parse(_GOLD)

    assert compute_spec_digest(serialize_spec_bytes(plain)) != compute_spec_digest(
        serialize_spec_bytes(selected)
    )
    source = cast(list[dict[str, JsonValue]], canonical_spec_mapping(selected)["sources"])[0]
    assert source["gold"] == {
        "select": ["id", "amount"],
        "filters": [{"column": "amount", "op": "gt", "value": 0}],
    }


# ------------------------------------------------------------------------ applying


def test_filters_run_before_select_and_nulls_never_pass() -> None:
    frame = pl.DataFrame(
        {"id": ["1", "2", "3"], "amount": [5, None, -1], "year": [2021, 2019, 2022]}
    )
    selection = GoldSelection(
        select=("id",),
        filters=(GoldFilter("amount", "gt", 0), GoldFilter("year", "ge", 2020)),
    )

    result, stats = apply_gold_selection(frame, selection)

    assert result.to_dicts() == [{"id": "1"}]
    assert (stats.input_rows, stats.output_rows) == (3, 1)


@pytest.mark.parametrize(
    ("rule", "kept"),
    [
        (GoldFilter("v", "eq", 2), [2]),
        (GoldFilter("v", "ne", 2), [1, 3]),
        (GoldFilter("v", "lt", 2), [1]),
        (GoldFilter("v", "le", 2), [1, 2]),
        (GoldFilter("v", "in", [1, 3]), [1, 3]),
        (GoldFilter("v", "not_null"), [1, 2, 3]),
    ],
)
def test_every_operator(rule: GoldFilter, kept: list[int]) -> None:
    frame = pl.DataFrame({"v": [1, 2, 3, None]})

    result, _ = apply_gold_selection(frame, GoldSelection(filters=(rule,)))

    assert result["v"].to_list() == kept


@pytest.mark.parametrize(
    "selection",
    [
        GoldSelection(select=("nope",)),
        GoldSelection(filters=(GoldFilter("nope", "gt", 0),)),
        GoldSelection(filters=(GoldFilter("name", "gt", 0),)),
    ],
    ids=["missing-select", "missing-filter", "type-mismatch"],
)
def test_a_selection_that_cannot_apply_fails_loudly(selection: GoldSelection) -> None:
    """Negative: never publish a different table than the spec asked for."""
    frame = pl.DataFrame({"name": ["a"], "v": [1]})

    with pytest.raises(GoldSelectionError):
        apply_gold_selection(frame, selection)


# ---------------------------------------------------------------------- end to end


def test_silver_keeps_everything_and_gold_publishes_the_selection(tmp_path: Path) -> None:
    service = BuilderService(
        output_root=tmp_path, client_factory=lambda **_: _Client(), warehouse_root=tmp_path / "wh"
    )

    response = service.build(_BASE.format(gold=_GOLD), run_id="r1")

    assert response.status_code == 200, response.body
    run = tmp_path / "r1"
    silver = pl.read_parquet(next(run.glob("silver/*/table.parquet")))
    gold = pl.read_parquet(next(run.glob("gold/*/table.parquet")))
    assert silver.height == 4 and "secret_note" in silver.columns
    assert gold.columns == ["id", "amount"]
    assert sorted(gold["id"].to_list()) == ["1", "4"]

    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    (key,) = manifest["gold_selection"]
    assert manifest["gold_selection"][key]["input_rows"] == 4
    assert manifest["gold_selection"][key]["output_rows"] == 2
    assert manifest["gold_selection"][key]["dropped_rows"] == 2
    assert manifest["row_counts"][key] == 4  # quality's basis stays Silver

    card = next(run.glob("gold/*/README.md")).read_text(encoding="utf-8")
    assert "secret_note" not in card

    (committed,) = cast(dict[str, dict[str, JsonValue]], response.body["materialized"]).values()
    catalog = service._table_catalog()
    assert catalog is not None
    assert catalog.get_snapshot(cast(str, committed["snapshot_id"])).row_count == 2


def test_a_build_without_gold_writes_no_selection(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())

    assert service.build(_BASE.format(gold=""), run_id="r1").status_code == 200

    manifest = json.loads((tmp_path / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert "gold_selection" not in manifest


def test_a_selection_that_cannot_apply_fails_the_source(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())
    spec = _BASE.format(gold="    gold:\n      select: [id, nope]\n")

    response = service.build(spec, run_id="r1")

    assert response.status_code == 502
    assert "nope" in str(response.body.get("error"))
