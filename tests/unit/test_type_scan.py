"""Raw JSON type checks run record by record, and case-fold collisions are refused (#868)."""

from __future__ import annotations

import polars as pl
import pytest

from kpubdata_builder.errors import TabularError
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.models import BronzeArtifact
from kpubdata_builder.stages.silver.normalize import normalize_table
from kpubdata_builder.tabular.convert import (
    RecordTypeScan,
    check_case_fold_collisions,
    records_to_dataframe,
)

_CASES: list[tuple[str, list[dict[str, JsonValue]], str | None]] = [
    ("R1 number and string", [{"v": 1}, {"v": "a"}], "heterogeneous"),
    ("R2 unsafe int and float", [{"v": 2**53 + 1}, {"v": 1.5}], "precision loss"),
    ("R3 float after the window", [{"v": i} for i in range(200)] + [{"v": 1.5}], None),
    ("list[int] vs list[str]", [{"v": [1]}, {"v": ["a"]}], "heterogeneous"),
    ("struct fields", [{"v": {"x": 1}}, {"v": {"x": "a"}}], "heterogeneous"),
    ("optional struct field", [{"v": {"x": 1}}, {"v": {"y": "a"}}], None),
    ("nested precision", [{"v": [{"w": 2**53 + 1}]}, {"v": [{"w": 2.5}]}], "precision loss"),
    ("nulls unify", [{"v": None}, {"v": [None, 1]}, {"v": [2]}], None),
    ("bool is not a number", [{"v": True}, {"v": 1}], "heterogeneous"),
]


@pytest.mark.parametrize(("name", "records", "error"), _CASES, ids=[c[0] for c in _CASES])
def test_the_streaming_scan_decides_as_the_table_builder_does(
    name: str, records: list[dict[str, JsonValue]], error: str | None
) -> None:
    scan = RecordTypeScan()
    for record in records:
        scan.add(record)

    if error is None:
        scan.check()
        records_to_dataframe(records)
    else:
        with pytest.raises(TabularError, match=error) as streamed:
            scan.check()
        with pytest.raises(TabularError) as whole:
            records_to_dataframe(records)
        assert str(streamed.value) == str(whole.value)


def test_read_as_keeps_the_text_the_source_gave() -> None:
    """A declared text column is text before any inference sees it; nothing is recovered
    afterwards (zeros already lost stay lost, which is why read_as acts at parse time)."""
    scan = RecordTypeScan(read_as={"code": "str"})

    kept = [scan.add({"code": value}) for value in ("00123", 123, None)]
    scan.check()

    assert [r["code"] for r in kept] == ["00123", "123", None]
    assert scan.columns == ("code",)


def test_case_fold_collisions_are_refused() -> None:
    """R7: no automatic suffix, no silent merge."""
    with pytest.raises(TabularError, match=r"\[\['NAME', 'Name', 'name'\]\]"):
        check_case_fold_collisions(["Name", "id", "name", "NAME"])
    check_case_fold_collisions(["name", "name_en", "id"])


def test_a_rename_can_resolve_a_collision() -> None:
    records: list[dict[str, JsonValue]] = [{"Name": "a", "name": "b"}]

    with pytest.raises(TabularError, match="letter case"):
        normalize_table(BronzeArtifact.from_records("p.d", records))
    table = normalize_table(BronzeArtifact.from_records("p.d", records), rename={"Name": "name_en"})

    assert isinstance(table, pl.DataFrame)
    assert table.columns == ["name_en", "name"]
