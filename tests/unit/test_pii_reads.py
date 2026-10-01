"""Declared PII does not leave in plain text through Silver, Bronze or preview reads (#900).

Gold masks declared PII (#689); Silver and Bronze keep the original values (#611). Each
service read of Silver or Bronze either shows the declared columns masked as Gold
masks them, or refuses the raw file. One negative test per way out, and the
``publish_unmasked`` opt-out is the only thing that shows a declared column as is.

The declaration comes from the same fake kpubdata client the Gold masking tests use
(``ref.license.pii_columns``), so the build and the reads see the same declaration.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import polars as pl
import pytest
import yaml

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import FileResponse, ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.gold.pii import PII_MASK_TOKEN, columns_withheld_from_silver

from ._openapi import validate
from .test_gold_pii_masking import _PHONES, _SPEC, _Client

_DEV = Principal("dev")
_KEY = "localdata.general_restaurant"
_PHONE_PATTERN = re.compile(r"\d{2,3}-\d{3,4}-\d{4}")
_CONTRACT = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)


def _service(tmp_path: Path, client: _Client | None = None, **kwargs: object) -> BuilderService:
    return BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client or _Client(),
        **kwargs,  # type: ignore[arg-type]
    )


def _built(tmp_path: Path, gold: str = "", client: _Client | None = None) -> BuilderService:
    service = _service(tmp_path, client)
    response = service.build(_SPEC.format(gold=gold), run_id="r1")
    assert response.status_code == 200, response.body
    return service


def _no_phone(body: object) -> None:
    text = json.dumps(body, ensure_ascii=False)
    assert not _PHONE_PATTERN.search(text), text
    for phone in _PHONES:
        assert phone not in text


def _silver_query(service: BuilderService, sql: str) -> ServiceResponse:
    return service.query(
        {"dataset_id": "permits.restaurant", "run_id": "r1", "stage": "silver", "sql": sql},
        principal=_DEV,
    )


def _raw_files(tmp_path: Path) -> list[str]:
    run = tmp_path / "r1"
    return sorted(
        str(p.relative_to(run))
        for stage in ("bronze", "silver")
        for p in (run / stage).rglob("*")
        if p.is_file()
    )


# ------------------------------------------------------------------ /query stage=silver


def test_a_silver_query_never_sees_a_declared_value(tmp_path: Path) -> None:
    """Negative: selecting, transforming or filtering on the column shows nothing."""
    service = _built(tmp_path)

    selected = _silver_query(service, "SELECT siteTel, upper(siteTel) AS loud FROM dataset")
    assert selected.status_code == 200, selected.body
    _no_phone(selected.body)
    rows = cast(list[dict[str, JsonValue]], selected.body["rows"])
    # Masked as Gold masks it: the token, and nulls stay null.
    assert sorted((r["siteTel"] for r in rows), key=str) == [None, *[PII_MASK_TOKEN] * 3]
    assert selected.body["masked_columns"] == ["siteTel"]

    # A WHERE equality is no oracle for an original value.
    probe = _silver_query(
        service, "SELECT count(*) AS n FROM dataset WHERE siteTel = '010-0000-0000'"
    )
    assert probe.status_code == 200, probe.body
    assert probe.body["rows"] == [{"n": 0}]
    assert (
        validate(
            cast(JsonValue, selected.body),
            {"$ref": "#/components/schemas/QueryResponse"},
            _CONTRACT,
        )
        == []
    )


def test_a_gold_query_is_unchanged(tmp_path: Path) -> None:
    """Gold is masked where it is built; its query response gains nothing."""
    service = _built(tmp_path)

    response = service.query(
        {
            "dataset_id": "permits.restaurant",
            "run_id": "r1",
            "stage": "gold",
            "sql": "SELECT siteTel FROM dataset",
        },
        principal=_DEV,
    )

    assert response.status_code == 200, response.body
    _no_phone(response.body)
    assert "masked_columns" not in response.body


# ------------------------------------------------- the masked copy keeps Builder dtypes

_DTYPE_CASES = ["null only", "duration", "int128", "zoned", "odd names", "list null"]


@pytest.mark.parametrize("name", _DTYPE_CASES)
def test_the_masked_silver_copy_keeps_every_builder_dtype(tmp_path: Path, name: str) -> None:
    """#891 review: DuckDB records Builder dtypes and real names in the file's metadata.

    A query of the masked copy reports the same columns and dtypes as a query of the
    original — an all-null column stays Null, a Duration, an Int128 and a zoned datetime
    keep their dtype, and an internally named column keeps its real name.
    """
    import duckdb

    from kpubdata_builder.query.service import QueryService
    from kpubdata_builder.service.pii_reads import masked_silver_table
    from kpubdata_builder.service.query_service_api import execute_query
    from kpubdata_builder.tabular.builder_parquet import (
        builder_dtypes,
        read_builder_parquet_schema,
    )
    from kpubdata_builder.tabular.duckdb_load import TableHandle, load_records

    from .test_duckdb_load import CASES

    records = [{**r, "phone": f"010-0000-000{i}"} for i, r in enumerate(CASES[name])]
    connection = duckdb.connect()
    loaded = load_records(connection, lambda: iter(records), table="raw", workdir=tmp_path)
    original = tmp_path / "table.parquet"
    TableHandle(connection, loaded, tmp_path).write_parquet(original)
    assert builder_dtypes(original)  # written with the recorded dtypes

    masked = masked_silver_table(original, frozenset({"phone"}))
    assert masked is not None
    try:
        expected = read_builder_parquet_schema(original)
        assert read_builder_parquet_schema(masked.path) == expected
        engine = QueryService()
        plain = execute_query(engine, original, "SELECT * FROM dataset", limit=10)
        hidden = execute_query(engine, masked.path, "SELECT * FROM dataset", limit=10)
    finally:
        masked.close()

    assert plain.status_code == hidden.status_code == 200, (plain.body, hidden.body)
    assert hidden.body["columns"] == plain.body["columns"]
    assert hidden.body["column_meta"] == plain.body["column_meta"]
    _no_phone(hidden.body)
    rows = cast(list[dict[str, JsonValue]], hidden.body["rows"])
    assert {r["phone"] for r in rows} == {PII_MASK_TOKEN}


class _NullColumnResult:
    def __init__(self) -> None:
        self.items = [
            {"mgtNo": str(i), "siteTel": phone, "memo": None} for i, phone in enumerate(_PHONES)
        ]


class _NullColumnClient(_Client):
    def dataset(self, key: str) -> object:
        inner = super().dataset(key)

        class _Dataset:
            ref = inner.ref

            def list(self, **_params: object) -> _NullColumnResult:
                return _NullColumnResult()

        return _Dataset()


def test_a_silver_query_reports_the_same_dtypes_with_and_without_declared_pii(
    tmp_path: Path,
) -> None:
    """End to end: masking changes the values of a declared column, never a dtype."""
    sql = "SELECT * FROM dataset"
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
    declared = _service(tmp_path / "a", _NullColumnClient())
    plain = _service(tmp_path / "b", _NullColumnClient(pii=()))
    for service in (declared, plain):
        assert service.build(_SPEC.format(gold=""), run_id="r1").status_code == 200

    masked = _silver_query(declared, sql)
    unmasked = _silver_query(plain, sql)

    assert masked.status_code == unmasked.status_code == 200, (masked.body, unmasked.body)
    assert masked.body["masked_columns"] == ["siteTel"]
    assert masked.body["columns"] == unmasked.body["columns"]
    assert masked.body["column_meta"] == unmasked.body["column_meta"]
    meta = {m["name"]: m for m in cast(list[dict[str, JsonValue]], masked.body["column_meta"])}
    assert meta["memo"]["logical_type"] == "null"
    _no_phone(masked.body)


# ------------------------------------------------------------------ /preview


def test_a_preview_shows_no_declared_value(tmp_path: Path) -> None:
    """Negative: sample, the raw source sample and the diffs are all masked."""
    response = _service(tmp_path).preview(_SPEC.format(gold=""), limit=10, principal=_DEV)

    assert response.status_code == 200, response.body
    _no_phone(response.body)
    (preview,) = cast(list[dict[str, JsonValue]], response.body["previews"])
    assert preview["masked_columns"] == ["siteTel"]
    sample = cast(list[dict[str, JsonValue]], preview["sample"])
    assert {r["siteTel"] for r in sample} == {PII_MASK_TOKEN, None}
    assert [r["bplcNm"] for r in sample] == ["shop 0", "shop 1", "shop 2", "shop 9"]


def test_a_preview_masks_the_raw_field_a_renamed_column_came_from(tmp_path: Path) -> None:
    """kpubdata names the source field; the raw sample and the diffs are masked too."""
    gold = "    schema:\n      rename:\n        siteTel: phone\n"

    response = _service(tmp_path).preview(_SPEC.format(gold=gold), limit=10, principal=_DEV)

    assert response.status_code == 200, response.body
    _no_phone(response.body)
    (preview,) = cast(list[dict[str, JsonValue]], response.body["previews"])
    assert preview["masked_columns"] == ["phone"]
    raw = cast(list[dict[str, JsonValue]], preview["source_sample"])
    assert {r["siteTel"] for r in raw} == {PII_MASK_TOKEN, None}


# ------------------------------------------------------------------ stage samples


def test_a_silver_stage_sample_shows_no_declared_value(tmp_path: Path) -> None:
    """Negative: the Silver stage detail's sample is masked."""
    service = _built(tmp_path)

    response = service.get_run_stage_detail("r1", "silver", _KEY, limit=10)

    assert response.status_code == 200, response.body
    _no_phone(response.body)
    assert response.body["masked_columns"] == ["siteTel"]
    sample = cast(list[dict[str, JsonValue]], response.body["sample"])
    assert sorted((r["siteTel"] for r in sample), key=str) == [None, *[PII_MASK_TOKEN] * 3]
    assert (
        validate(
            cast(JsonValue, response.body),
            {"$ref": "#/components/schemas/SilverStageDetailResponse"},
            _CONTRACT,
        )
        == []
    )


def test_a_bronze_stage_detail_shows_no_declared_value(tmp_path: Path) -> None:
    """Negative: the Bronze stage detail carries metadata, never a raw record."""
    service = _built(tmp_path)

    response = service.get_run_stage_detail("r1", "bronze", _KEY, limit=10)

    assert response.status_code == 200, response.body
    _no_phone(response.body)


# ------------------------------------------------------------------ artifact downloads


def test_a_silver_table_download_is_refused(tmp_path: Path) -> None:
    """Negative: silver table.parquet holds the values as is, so it does not leave."""
    service = _built(tmp_path)

    response = service.serve_artifact_file("r1", f"silver/{_KEY}/table.parquet")

    assert isinstance(response, ServiceResponse)
    assert response.status_code == 403
    assert response.body["code"] == "declared_pii_withheld"
    assert response.body["columns"] == ["siteTel"]
    _no_phone(response.body)
    for schema in ("DeclaredPiiWithheldError", "Error"):
        assert (
            validate(
                cast(JsonValue, response.body),
                {"$ref": f"#/components/schemas/{schema}"},
                _CONTRACT,
            )
            == []
        )


def test_a_bronze_raw_download_is_refused(tmp_path: Path) -> None:
    """Negative: the raw records hold the values as is, so they do not leave."""
    service = _built(tmp_path)
    bronze = [p for p in _raw_files(tmp_path) if p.startswith("bronze/")]
    assert any(p.endswith("raw_records.jsonl") for p in bronze)

    for path in bronze:
        response = service.serve_artifact_file("r1", path)
        assert (response.status_code, getattr(response, "body", {}).get("code")) == (
            403,
            "declared_pii_withheld",
        ), path


def test_every_silver_file_is_refused_and_gold_is_served(tmp_path: Path) -> None:
    """preview.json holds sample rows as is too; Gold holds the masked table."""
    service = _built(tmp_path)

    for path in _raw_files(tmp_path):
        assert service.serve_artifact_file("r1", path).status_code == 403, path
    gold = service.serve_artifact_file("r1", f"gold/{_KEY}/table.parquet")
    assert isinstance(gold, FileResponse)
    assert service.serve_artifact_file("r1", "manifest.json").status_code == 200


def test_a_percent_encoded_path_is_refused_too(tmp_path: Path) -> None:
    service = _built(tmp_path)

    response = service.serve_artifact_file("r1", f"silver/{_KEY}/table%2Eparquet")

    assert response.status_code == 403


# ------------------------------------------------------------------ consistency


def test_nothing_declared_leaves_every_read_as_before(tmp_path: Path) -> None:
    service = _built(tmp_path, client=_Client(pii=()))

    silver = service.serve_artifact_file("r1", f"silver/{_KEY}/table.parquet")
    assert isinstance(silver, FileResponse)
    stage = service.get_run_stage_detail("r1", "silver", _KEY, limit=10)
    assert "masked_columns" not in stage.body
    assert "010-0000-0000" in json.dumps(stage.body)
    query = _silver_query(service, "SELECT siteTel FROM dataset")
    assert "masked_columns" not in query.body
    assert "010-0000-0000" in json.dumps(query.body)


def test_publish_unmasked_is_the_only_column_shown_as_is(tmp_path: Path) -> None:
    """A read shows in plain text exactly what Gold publishes in plain text."""
    gold = "    gold:\n      pii_columns: [bplcNm]\n      publish_unmasked: [siteTel]\n"
    service = _built(tmp_path, gold=gold)

    stage = service.get_run_stage_detail("r1", "silver", _KEY, limit=10)
    sample = cast(list[dict[str, JsonValue]], stage.body["sample"])
    assert stage.body["masked_columns"] == ["bplcNm"]
    assert {r["bplcNm"] for r in sample} == {PII_MASK_TOKEN}
    assert "010-0000-0000" in [r["siteTel"] for r in sample]

    query = _silver_query(service, "SELECT bplcNm, siteTel FROM dataset")
    assert query.body["masked_columns"] == ["bplcNm"]
    assert "shop 0" not in json.dumps(query.body)
    assert "010-0000-0000" in json.dumps(query.body)

    refused = service.serve_artifact_file("r1", f"silver/{_KEY}/table.parquet")
    assert refused.status_code == 403
    assert refused.body["columns"] == ["bplcNm"]  # type: ignore[union-attr]

    gold_table = pl.read_parquet(next((tmp_path / "r1").glob("gold/*/table.parquet")))
    assert "010-0000-0000" in gold_table["siteTel"].to_list()
    assert set(gold_table["bplcNm"].to_list()) == {PII_MASK_TOKEN}


def test_every_column_opted_out_lets_silver_be_read_as_is(tmp_path: Path) -> None:
    service = _built(tmp_path, gold="    gold:\n      publish_unmasked: [siteTel]\n")

    silver = service.serve_artifact_file("r1", f"silver/{_KEY}/table.parquet")
    assert isinstance(silver, FileResponse)
    assert "masked_columns" not in _silver_query(service, "SELECT siteTel FROM dataset").body


def test_a_column_gold_select_drops_is_still_masked_in_silver(tmp_path: Path) -> None:
    """Gold never publishes it, but Silver holds it: the read masks it."""
    service = _built(tmp_path, gold="    gold:\n      select: [mgtNo, bplcNm]\n")

    stage = service.get_run_stage_detail("r1", "silver", _KEY, limit=10)

    _no_phone(stage.body)
    assert stage.body["masked_columns"] == ["siteTel"]
    _no_phone(_silver_query(service, "SELECT * FROM dataset").body)


def test_forbidden_terms_are_refused_before_pii_is_masked(tmp_path: Path) -> None:
    """The redistribution gate (#688) runs first at the same choke point."""
    service = _service(tmp_path, terms_lookup=lambda _id: "forbidden")
    assert service.build(_SPEC.format(gold=""), run_id="r1").status_code == 200

    download = service.serve_artifact_file("r1", f"silver/{_KEY}/table.parquet")
    assert download.body["code"] == "redistribution_forbidden"  # type: ignore[union-attr]
    stage = service.get_run_stage_detail("r1", "silver", _KEY, limit=10)
    assert stage.body["sample"] == []
    assert stage.body["sample_withheld"] == "redistribution_forbidden"


def test_the_manifest_record_still_masks_when_the_declaration_is_gone(tmp_path: Path) -> None:
    """A declaration the catalog later drops does not unmask a run built under it."""
    _built(tmp_path)
    later = _service(tmp_path, pii_lookup=lambda _id: ())

    stage = later.get_run_stage_detail("r1", "silver", _KEY, limit=10)

    _no_phone(stage.body)
    assert later.serve_artifact_file("r1", f"silver/{_KEY}/table.parquet").status_code == 403


# ------------------------------------------------------------------ fail closed


def _unreadable(_dataset_id: str) -> tuple[str, ...]:
    raise RuntimeError("catalog unavailable")


def _unavailable(response: ServiceResponse | FileResponse) -> None:
    assert isinstance(response, ServiceResponse)
    assert response.status_code == 503, response.body
    assert response.body["code"] == "pii_declaration_unavailable"
    _no_phone(response.body)
    assert (
        validate(cast(JsonValue, response.body), {"$ref": "#/components/schemas/Error"}, _CONTRACT)
        == []
    )


def test_an_unreadable_declaration_refuses_every_silver_and_bronze_read(tmp_path: Path) -> None:
    """Negative (#688: unknown is not permission): no partial mask, nothing as is.

    ``gold.select`` drops siteTel, so the manifest records nothing masked: falling back to
    the record would have served the phone numbers.
    """
    _built(tmp_path, gold="    gold:\n      select: [mgtNo, bplcNm]\n")
    later = _service(tmp_path, pii_lookup=_unreadable)

    _unavailable(_silver_query(later, "SELECT siteTel FROM dataset"))
    for path in _raw_files(tmp_path):
        _unavailable(later.serve_artifact_file("r1", path))
    stage = later.get_run_stage_detail("r1", "silver", _KEY, limit=10)
    assert stage.status_code == 200
    assert stage.body["sample"] == []
    assert stage.body["sample_withheld"] == "pii_declaration_unavailable"
    _no_phone(stage.body)
    assert (
        validate(
            cast(JsonValue, stage.body),
            {"$ref": "#/components/schemas/SilverStageDetailResponse"},
            _CONTRACT,
        )
        == []
    )


def test_an_unreadable_declaration_leaves_gold_reads_as_they_were(tmp_path: Path) -> None:
    _built(tmp_path)
    later = _service(tmp_path, pii_lookup=_unreadable)

    assert isinstance(later.serve_artifact_file("r1", f"gold/{_KEY}/table.parquet"), FileResponse)
    gold = later.query(
        {
            "dataset_id": "permits.restaurant",
            "run_id": "r1",
            "stage": "gold",
            "sql": "SELECT siteTel FROM dataset",
        },
        principal=_DEV,
    )
    assert gold.status_code == 200, gold.body
    _no_phone(gold.body)


class _UnreadableRefDataset:
    """Fetches like the fixture dataset, but its spec cannot be read."""

    def __init__(self) -> None:
        from .test_gold_pii_masking import _Dataset

        self._inner = _Dataset()

    @property
    def ref(self) -> object:
        raise RuntimeError("spec unavailable")

    def list(self, **params: object) -> object:
        return self._inner.list(**params)


class _UnreadableRefClient(_Client):
    def dataset(self, _key: str) -> _UnreadableRefDataset:  # type: ignore[override]
        return _UnreadableRefDataset()


def test_a_preview_whose_declaration_is_unreadable_is_refused(tmp_path: Path) -> None:
    """Negative: the fetched rows are there, the declaration is not — nothing leaves."""
    response = _service(tmp_path, _UnreadableRefClient()).preview(
        _SPEC.format(gold=""), limit=10, principal=_DEV
    )

    _unavailable(response)


def test_a_build_spec_only_declaration_needs_no_lookup(tmp_path: Path) -> None:
    """A file source, or a BuildSpec-only declaration, does not depend on kpubdata."""
    from kpubdata_builder.service.pii_reads import source_withheld_columns
    from kpubdata_builder.spec.models import GoldSelection, SourceRef

    source = SourceRef(kind="file", upload_id="u1", gold=GoldSelection(pii_columns=("phone",)))

    assert source_withheld_columns(source, _unreadable, silver_columns=["phone"]) == {"phone"}


# ------------------------------------------------------------------ the resolution


@pytest.mark.parametrize(
    ("silver_columns", "expected"),
    [
        (["mgtNo", "phone"], {"phone"}),
        (None, {"phone", "owner"}),
        (["mgtNo"], set()),
    ],
    ids=["present", "silver unknown: every declaration", "declared but absent"],
)
def test_withheld_columns_follow_the_gold_declaration(
    silver_columns: list[str] | None, expected: set[str]
) -> None:
    from kpubdata_builder.spec.models import SchemaContract

    withheld = columns_withheld_from_silver(
        core=("siteTel", "rprsvNm"),
        build_spec=(),
        publish_unmasked=("rprsvNm_ok",),
        silver_columns=silver_columns,
        contract=SchemaContract(rename={"siteTel": "phone", "rprsvNm": "owner"}),
    )

    assert withheld == expected
