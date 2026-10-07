"""kpubdata's code columns reach clients as identifiers, sent as text (#702).

Builder does not decide which columns are codes. kpubdata declares them
(`semantic_kind: code`, kpubdata ADR 0006) and keeps their leading zeros; Builder
reads that declaration and reports such a text column as logical type `identifier`,
whose wire encoding is always `string`. These pin:

- the Core mapping: `semantic_kind` becomes a `core_spec` semantic hint, unknown kinds
  carried verbatim;
- the rule: only a text column whose resolved kind is `code` is an identifier, and
  nothing about its values or wire encoding changes;
- where the declaration comes from: the run's BuildSpec sources, renames included;
- the issue's completion list over the warehouse read paths, through `json.dumps` and
  back as a client would parse it: a leading-zero id, a 19-digit id, null, the largest
  safe integer, the first unsafe one, Int64 max and min, and an exact decimal amount.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest
import yaml

from kpubdata_builder.query.export import _TEXT_TYPES
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service import query_service_api as query_api_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.column_semantics import (
    describe_columns,
    source_semantics,
    spec_semantics,
)
from kpubdata_builder.service.datasets import read_snapshot_spec
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE
from kpubdata_builder.spec import parse_spec
from kpubdata_builder.spec.models import SchemaContract, SourceRef
from kpubdata_builder.tabular.semantics import (
    ColumnSemantics,
    SemanticHint,
    from_field_descriptor,
)
from kpubdata_builder.tabular.wire import (
    IDENTIFIER_LOGICAL_TYPE,
    JS_SAFE_INTEGER,
    column_meta,
    encode_rows,
    mark_identifiers,
)
from kpubdata_builder.warehouse import materialize
from tests.support.polars_engine import infer_schema

from ._openapi import validate

_CONTRACT: dict[str, Any] = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)
_DEV = Principal("dev")
_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)
_CODE = ColumnSemantics(semantic=SemanticHint(kind="code", origin="core_spec"))


# ---------------------------------------------------------------- Core mapping


@dataclass
class _Field:
    name: str
    semantic_kind: str | None = None
    title: str | None = None


def test_the_declared_semantic_kind_becomes_a_core_spec_hint() -> None:
    models = pytest.importorskip("kpubdata.core.models")
    field = models.FieldDescriptor(name="bonbun", type="string", semantic_kind="code")

    assert from_field_descriptor(field).semantic == SemanticHint("code", "core_spec")


def test_a_declared_kind_wins_over_the_kind_a_format_suggests() -> None:
    models = pytest.importorskip("kpubdata.core.models")
    field = models.FieldDescriptor(
        name="ym",
        semantic_kind="code",
        constraints=models.FieldConstraints(format="YYYYMM"),
    )

    assert from_field_descriptor(field).semantic == SemanticHint("code", "core_spec")


@pytest.mark.parametrize(("declared", "kind"), [("coordinate", "coordinate"), (" Code ", "code")])
def test_a_kind_this_builder_does_not_know_is_carried_verbatim(declared: str, kind: str) -> None:
    assert from_field_descriptor(_Field("x", semantic_kind=declared)).semantic == SemanticHint(
        kind, "core_spec"
    )


@pytest.mark.parametrize("declared", [None, "", "  ", 3])
def test_no_declared_kind_says_nothing(declared: object) -> None:
    field = _Field("x", semantic_kind=cast(Any, declared))
    assert from_field_descriptor(field).semantic is None


# ---------------------------------------------------------------- the rule


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "bonbun": ["0012", None, "0345"],
            "big_code": ["1234567890123456789", "0000000000000000001", None],
            "cast_code": [12, None, 345],
            "label": ["0012", "b", None],
        },
        schema={
            "bonbun": pl.String,
            "big_code": pl.String,
            "cast_code": pl.Int64,
            "label": pl.String,
        },
    )


def test_only_a_text_column_declared_code_is_an_identifier() -> None:
    columns = infer_schema(_frame()).columns
    semantics = {
        "bonbun": _CODE,
        "big_code": _CODE,
        # A code a user cast to a number is sent as one; the label says what is sent.
        "cast_code": _CODE,
        "label": ColumnSemantics(semantic=SemanticHint(kind="text", origin="core_spec")),
    }

    meta = mark_identifiers(column_meta(columns), semantics)

    assert {m["name"]: (m["logical_type"], m["wire_encoding"]) for m in meta} == {
        "bonbun": (IDENTIFIER_LOGICAL_TYPE, "string"),
        "big_code": (IDENTIFIER_LOGICAL_TYPE, "string"),
        "cast_code": ("int64", "number"),
        "label": ("string", "string"),
    }


def test_an_identifier_is_sent_exactly_as_stored() -> None:
    columns = infer_schema(_frame()).columns
    rows = json.loads(json.dumps(encode_rows(_frame().to_dicts(), columns)))

    assert [row["bonbun"] for row in rows] == ["0012", None, "0345"]
    assert [row["big_code"] for row in rows] == [
        "1234567890123456789",
        "0000000000000000001",
        None,
    ]


def test_without_semantics_the_metadata_is_unchanged() -> None:
    plain = column_meta(infer_schema(_frame()).columns)

    assert describe_columns(plain, None) == plain
    assert describe_columns(plain, {}) == plain


def test_the_contract_documents_identifier_and_accepts_it() -> None:
    entry = {"name": "bonbun", "logical_type": "identifier", "wire_encoding": "string"}
    described = {**entry, "semantic": {"kind": "code", "origin": "core_spec"}}
    for item in (entry, described):
        errors = validate(item, {"$ref": "#/components/schemas/ColumnWireInfo"}, _CONTRACT)
        assert errors == []
    description = _CONTRACT["components"]["schemas"]["ColumnWireInfo"]["properties"][
        "logical_type"
    ]["description"]
    assert "identifier" in description


def test_a_spreadsheet_export_treats_an_identifier_as_text() -> None:
    assert IDENTIFIER_LOGICAL_TYPE in _TEXT_TYPES


# ---------------------------------------------------------------- where it comes from


def test_the_declaration_comes_from_the_sources_kpubdata_spec() -> None:
    pytest.importorskip("kpubdata.core.spec")
    semantics = source_semantics(SourceRef(provider="datago", dataset="apt_trade"))

    kinds = {name: s.semantic.kind for name, s in semantics.items() if s.semantic is not None}
    # kpubdata 0.8 declares these apt_trade fields codes; Builder adds none of its own.
    assert {"bonbun", "bubun"} <= {name for name, kind in kinds.items() if kind == "code"}
    assert "dealAmount" not in {name for name, kind in kinds.items() if kind == "code"}


def test_a_renamed_code_is_described_under_its_new_name() -> None:
    pytest.importorskip("kpubdata.core.spec")
    source = SourceRef(
        provider="datago",
        dataset="apt_trade",
        schema=SchemaContract(rename={"bonbun": "main_lot"}),
    )

    semantics = source_semantics(source)

    assert semantics["main_lot"].semantic == SemanticHint("code", "core_spec")
    assert "bonbun" not in semantics


def test_a_file_or_unknown_source_declares_nothing() -> None:
    assert source_semantics(SourceRef(kind="file", upload_id="upl_" + "0" * 32)) == {}
    assert source_semantics(SourceRef(provider="datago", dataset="no_such_dataset")) == {}


def test_a_column_two_sources_describe_differently_gets_nothing() -> None:
    pytest.importorskip("kpubdata.core.spec")
    spec = parse_spec(
        {
            "dataset_id": "demo",
            "title": "t",
            "description": "d",
            "sources": [
                {"provider": "datago", "dataset": "apt_trade", "alias": "trade"},
                {
                    "provider": "datago",
                    "dataset": "apt_trade",
                    "alias": "renamed",
                    "schema": {"rename": {"bonbun": "lot", "aptNm": "bonbun"}},
                },
            ],
            "exports": [{"kind": "jsonl", "output_path": "o.jsonl"}],
        }
    )

    assert spec_semantics(spec, "trade")["bonbun"].semantic == SemanticHint("code", "core_spec")
    assert "bonbun" not in spec_semantics(spec, None)


# ---------------------------------------------------------------- /query


def test_query_reports_the_runs_code_columns_as_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = infer_schema(_frame()).columns

    class _Engine:
        def execute(self, table_path: Path, sql: str, *, limit: int) -> QueryResult:
            return QueryResult(
                columns=tuple(c.name for c in columns),
                column_meta=tuple(column_meta(columns)),
                rows=encode_rows(_frame().to_dicts(), columns),
                truncated=False,
                execution_ms=0,
                startup_ms=0,
                engine_execution_ms=0,
            )

    @dataclass
    class _Context:
        run_id: str = "run-1"
        stage: str = "gold"
        source: str = "trade"
        table_path: Path = tmp_path / "table.parquet"

    seen: list[tuple[Path, str]] = []

    def _spec(root: Path, run_id: str) -> Any:
        seen.append((root, run_id))
        return _spec_document_parsed()

    monkeypatch.setattr(query_api_module, "resolve_query_context", lambda *a: _Context())
    monkeypatch.setattr(query_api_module, "read_snapshot_spec", _spec)
    api = query_api_module.QueryApiService(output_root=tmp_path, engine=cast(Any, _Engine()))

    response = api.query(
        {"dataset_id": "demo", "run_id": "run-1", "stage": "gold", "sql": "SELECT * FROM dataset"},
        principal=_DEV,
    )

    assert response.status_code == 200, response.body
    assert seen == [(tmp_path, "run-1")]
    meta = {m["name"]: m for m in cast(list[dict[str, Any]], response.body["column_meta"])}
    assert meta["bonbun"]["logical_type"] == "identifier"
    assert meta["bonbun"]["wire_encoding"] == "string"
    assert meta["bonbun"]["semantic"] == {"kind": "code", "origin": "core_spec"}
    assert meta["label"]["logical_type"] == "string"


# ---------------------------------------------------------------- warehouse, end to end

_DATASET = "demo-trade"
_TABLE = f"{_DATASET}.trade"


def _spec_document() -> dict[str, Any]:
    return {
        "dataset_id": _DATASET,
        "title": "Trades",
        "description": "Apartment trades",
        "license": "other",
        "license_name": "kogl-type-1",
        "license_link": "https://www.kogl.or.kr/info/licenseType1.do",
        "attribution": "국토교통부",
        "sources": [{"provider": "datago", "dataset": "apt_trade", "alias": "trade"}],
        "exports": [{"kind": "jsonl", "output_path": "trade.jsonl"}],
    }


def _spec_document_parsed() -> Any:
    return parse_spec({**_spec_document(), "dataset_id": "demo"})


_WIRE = pl.DataFrame(
    {
        # Declared codes in kpubdata's apt_trade spec.
        "bonbun": ["0012", "0000", None],
        "roadNmSeq": ["1234567890123456789", "01", None],
        # Not declared a code: stays a plain string.
        "aptNm": ["001 Apartments", None, "B"],
        "safe": [JS_SAFE_INTEGER, -JS_SAFE_INTEGER, None],
        "unsafe": [JS_SAFE_INTEGER + 2, _INT64_MAX, _INT64_MIN],
        "amount": [Decimal("12345678901234.50"), Decimal("-0.10"), None],
    },
    schema={
        "bonbun": pl.String,
        "roadNmSeq": pl.String,
        "aptNm": pl.String,
        "safe": pl.Int64,
        "unsafe": pl.Int64,
        "amount": pl.Decimal(20, 2),
    },
)


@pytest.fixture
def warehouse(tmp_path: Path) -> BuilderService:
    pytest.importorskip("kpubdata.core.spec")
    service = BuilderService(
        output_root=tmp_path, client_factory=lambda **_: None, warehouse_root=tmp_path / "wh"
    )
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "buildspec.yaml").write_text(
        yaml.safe_dump(_spec_document(), allow_unicode=True), encoding="utf-8"
    )
    assert read_snapshot_spec(tmp_path, "run-1") is not None, "the test spec must parse"
    gold = tmp_path / "gold"
    gold.mkdir()
    _WIRE.write_parquet(gold / "table.parquet")
    catalog = service._table_catalog()
    assert catalog is not None
    materialize(
        catalog,
        workspace_id=PERSONAL_WORKSPACE,
        logical_name=_TABLE,
        source_dir=gold,
        run_id="run-1",
        row_count=_WIRE.height,
    )
    return service


def _as_client_reads(body: object) -> dict[str, Any]:
    """What a JSON client holds after parsing the body (Python's parser keeps big ints,
    so this checks the text sent rather than the double a browser would make)."""
    return cast(dict[str, Any], json.loads(json.dumps(body)))


def _assert_the_completion_list(body: dict[str, Any]) -> None:
    meta = {m["name"]: (m["logical_type"], m["wire_encoding"]) for m in body["column_meta"]}
    assert meta == {
        "bonbun": ("identifier", "string"),
        "roadNmSeq": ("identifier", "string"),
        "aptNm": ("string", "string"),
        "safe": ("int64", "number"),
        "unsafe": ("int64", "decimal_string"),
        "amount": ("decimal", "decimal_string"),
    }
    rows = body["rows"]
    assert [r["bonbun"] for r in rows] == ["0012", "0000", None]
    assert [r["roadNmSeq"] for r in rows] == ["1234567890123456789", "01", None]
    assert [r["safe"] for r in rows] == [JS_SAFE_INTEGER, -JS_SAFE_INTEGER, None]
    assert [r["unsafe"] for r in rows] == [
        "9007199254740993",
        str(_INT64_MAX),
        str(_INT64_MIN),
    ]
    assert [r["amount"] for r in rows] == ["12345678901234.50", "-0.10", None]


def test_warehouse_query_keeps_every_value_of_the_completion_list(
    warehouse: BuilderService,
) -> None:
    response = warehouse.query_warehouse(
        {"table": _TABLE, "sql": "SELECT * FROM dataset", "limit": 10}, principal=_DEV
    )

    assert response.status_code == 200, response.body
    _assert_the_completion_list(_as_client_reads(response.body["result"]))


def test_warehouse_rows_keep_every_value_of_the_completion_list(
    warehouse: BuilderService,
) -> None:
    response = warehouse.read_warehouse_rows({"table": _TABLE, "page_size": 10}, principal=_DEV)

    assert response.status_code == 200, response.body
    _assert_the_completion_list(_as_client_reads(response.body))


def test_warehouse_aggregate_groups_by_an_identifier_as_text(warehouse: BuilderService) -> None:
    response = warehouse.aggregate_warehouse(
        {
            "table": _TABLE,
            "group_by": ["bonbun"],
            "measures": [{"fn": "count_rows", "as": "n"}],
            "order_by": [{"key": "bonbun", "direction": "asc"}],
        },
        principal=_DEV,
    )

    assert response.status_code == 200, response.body
    body = _as_client_reads(response.body)
    meta = {m["name"]: m["logical_type"] for m in body["column_meta"]}
    assert meta["bonbun"] == "identifier"
    assert {r["bonbun"] for r in body["rows"]} == {"0012", "0000", None}


def test_warehouse_profile_reports_an_identifier(warehouse: BuilderService) -> None:
    for _ in range(2):  # computed, then read from the cache
        response = warehouse.get_warehouse_profile(_TABLE, "current", principal=_DEV)
        assert response.status_code == 200, response.body
        columns = cast(dict[str, Any], response.body["profile"])["columns"]
        types = {c["name"]: c["logical_type"] for c in columns}
        assert types["bonbun"] == "identifier"
        assert types["aptNm"] == "string"
        assert types["safe"] == "int64"
