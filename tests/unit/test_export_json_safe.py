"""Export common JSON safe conversion (#629 follow-up).

Declaring ``casts: {deal_date: date}`` puts ``datetime.date`` in Gold records,
but ``json.dumps`` cannot serialize it. That ``TypeError`` is masked at service boundary,
so users see a build failure without knowing the cause.
"""

from __future__ import annotations

import csv as csv_module
import datetime
import json
from decimal import Decimal
from pathlib import Path

import pytest

from kpubdata_builder.artifact import ArtifactDataset
from kpubdata_builder.exporters._json_safe import json_safe
from kpubdata_builder.exporters.csv import CsvExporter
from kpubdata_builder.exporters.jsonl import JsonlExporter
from kpubdata_builder.spec import ExportTarget


class TestJsonSafe:
    def test_dates_become_iso_8601(self) -> None:
        assert json_safe(datetime.date(2026, 9, 24)) == "2026-09-24"
        assert json_safe(datetime.datetime(2026, 9, 24, 1, 2, 3)) == "2026-09-24T01:02:03"
        assert json_safe(datetime.time(1, 2, 3)) == "01:02:03"

    def test_decimal_keeps_its_scale(self) -> None:
        # Changing to float silently alters decimal places — for amount columns that is
        # data change.
        assert json_safe(Decimal("1.50")) == "1.50"

    def test_nested_structures_are_converted(self) -> None:
        value = {"rows": [{"d": datetime.date(2026, 1, 1)}], "n": (Decimal("2.0"),)}

        assert json_safe(value) == {"rows": [{"d": "2026-01-01"}], "n": ["2.0"]}

    def test_an_unserializable_value_is_left_for_json_dumps_to_reject(self) -> None:
        # Expanding set to list silently changes data. Choosing representation is
        # contract, so this layer doesn't silently decide — TypeError must occur.
        assert json_safe({"bad": {1, 2}}) == {"bad": {1, 2}}
        with pytest.raises(TypeError):
            json.dumps(json_safe({"bad": {1, 2}}))

    def test_plain_json_values_pass_through_unchanged(self) -> None:
        value = {"a": 1, "b": "x", "c": None, "d": True, "e": 1.5, "f": [1, 2]}

        assert json_safe(value) == value

    def test_binary_is_left_for_json_dumps_to_reject(self) -> None:
        # Choosing arbitrary encoding is also a contract — don't silently decide here.
        with pytest.raises(TypeError):
            json.dumps(json_safe(b"\x00\x01"))


class TestExportersHandleDeclaredDateCasts:
    RECORDS = ({"id": 1, "deal_date": datetime.date(2026, 9, 24), "amount": Decimal("1200.50")},)

    def _artifact(self) -> ArtifactDataset:
        return ArtifactDataset.from_records(records=self.RECORDS, statistics={"row_count": 1})

    def test_jsonl_export_serializes_a_date_column(self, tmp_path: Path) -> None:
        result = JsonlExporter().export(
            self._artifact(), ExportTarget(kind="jsonl", output_path="data.jsonl"), tmp_path
        )

        row = json.loads(result.output_path.read_text(encoding="utf-8").strip())
        assert row["deal_date"] == "2026-09-24"
        assert row["amount"] == "1200.50"

    def test_csv_export_serializes_a_date_column(self, tmp_path: Path) -> None:
        result = CsvExporter().export(
            self._artifact(), ExportTarget(kind="csv", output_path="data.csv"), tmp_path
        )

        rows = list(
            csv_module.DictReader(result.output_path.read_text(encoding="utf-8").splitlines())
        )
        assert rows[0]["deal_date"] == "2026-09-24"
        assert rows[0]["amount"] == "1200.50"
