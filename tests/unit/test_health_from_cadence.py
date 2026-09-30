"""A table's health follows the refresh cadence its spec declares (#781, owner decision D7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
import yaml

from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.datasets import RunRecord, last_success_at, status_axes
from kpubdata_builder.spec import JsonValue, parse_spec
from kpubdata_builder.spec.cadence import parse_cadence
from kpubdata_builder.spec.serializer import canonical_spec_mapping

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
_SPEC = """\
dataset_id: cadence.table
title: Cadence
description: d
{cadence}sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


def _record(status: str = "ok") -> RunRecord:
    return RunRecord("r1", "cadence.table", status, None, None, None, None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("P1D", timedelta(days=1)),
        ("PT6H", timedelta(hours=6)),
        ("P1W", timedelta(weeks=1)),
        ("P1DT12H", timedelta(days=1, hours=12)),
    ],
)
def test_cadences_are_parsed(value: str, expected: timedelta) -> None:
    assert parse_cadence(value) == expected


@pytest.mark.parametrize("value", ["P1M", "P1Y", "P0D", "1d", "daily", ""])
def test_unsupported_cadences_are_refused(value: str) -> None:
    """Negative: months and years vary in length; a verdict must not."""
    with pytest.raises(SpecLoadError):
        parse_spec(yaml.safe_load(_SPEC.format(cadence=f"refresh_cadence: '{value}'\n")))


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        (_NOW - timedelta(hours=23), "healthy"),
        (_NOW - timedelta(hours=25), "stale"),
    ],
)
def test_health_compares_the_last_success_with_one_cadence(last: datetime, expected: str) -> None:
    axes = status_axes(
        {"row_counts": {"a": 1}},
        _record(),
        refresh_cadence="P1D",
        last_success_at=last.isoformat(),
        now=_NOW,
    )

    assert axes["health"] == expected


@pytest.mark.parametrize(
    ("cadence", "last"),
    [
        (None, (_NOW - timedelta(days=30)).isoformat()),
        ("P1D", None),
        ("P1D", "2026-09-30T00:00:00"),
    ],
    ids=["no-cadence", "never-succeeded", "naive-time"],
)
def test_health_without_evidence_is_unknown(cadence: str | None, last: str | None) -> None:
    """Negative: never inferred from how often the table happens to run."""
    axes = status_axes(
        {"row_counts": {"a": 1}},
        _record(),
        refresh_cadence=cadence,
        last_success_at=last,
        now=_NOW,
    )

    assert axes["health"] == "unknown"


def test_the_last_success_ignores_failed_runs() -> None:
    records = [
        RunRecord("a", "t", "ok", None, "2026-09-28T00:00:00+00:00", None, None),
        RunRecord("b", "t", "failed", None, "2026-09-29T00:00:00+00:00", None, None),
        RunRecord("c", "other", "ok", None, "2026-09-30T00:00:00+00:00", None, None),
    ]

    assert last_success_at(records, "t") == "2026-09-28T00:00:00+00:00"
    assert last_success_at(records, "none") is None


def test_the_cadence_is_part_of_the_recipe_only_when_declared() -> None:
    plain = parse_spec(yaml.safe_load(_SPEC.format(cadence="")))
    declared = parse_spec(yaml.safe_load(_SPEC.format(cadence="refresh_cadence: P1D\n")))

    assert "refresh_cadence" not in canonical_spec_mapping(plain)
    assert canonical_spec_mapping(declared)["refresh_cadence"] == "P1D"


class _Result:
    items = [{"id": "1"}]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


def test_a_freshly_built_table_with_a_cadence_is_healthy(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())
    spec = _SPEC.format(cadence="refresh_cadence: P1D\n")
    assert service.build(spec, run_id="r1").status_code == 200

    detail = service.get_dataset("cadence.table", principal=Principal("dev"))

    assert cast(dict[str, JsonValue], detail.body["status_axes"])["health"] == "healthy"
