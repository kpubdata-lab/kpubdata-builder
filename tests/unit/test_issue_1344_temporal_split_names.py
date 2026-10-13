"""The ratio-split leakage guard reads a column name word by word (#1344)."""

from __future__ import annotations

import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef, SplitSpec
from kpubdata_builder.spec.validator import validate_spec
from kpubdata_builder.stages.gold.split import apply_splits

# The indicators the guard matched anywhere in the name before #1344.
_OLD_SUBSTRINGS = ("date", "time", "dt", "year", "month", "day", "hour", "timestamp", "at", "ts")

_TIME_NAMES = (
    "date",
    "time",
    "timestamp",
    "datetime",
    "dt",
    "ts",
    "year",
    "month",
    "day",
    "hour",
    "base_date",
    "baseDate",
    "BASE_DATE",
    "basedate",
    "created_at",
    "createdAt",
    "CREATED_AT",
    "event_ts",
    "eventTs",
    "STDR_DT",
    "stdrDt",
    "rentDT",
    "dealYear",
    "deal_month",
    "dealDay",
    "use-hour",
    "obs.time",
    "dataTime",
    "regdatetime",
    "logtimestamp",
    "baseyear",
    "basemonth",
    "date1",
    "dt2",
    "ISODate",
    "iso_timestamp",
    "last_update_date",
)

_OTHER_NAMES = (
    "category",
    "status",
    "state",
    "rate",
    "latitude",
    "data",
    "format",
    "habitat",
    "seats",
    "costs",
    "district",
    "width",
    "items",
    "accounts",
    "Category",
    "STATUS",
    "itemStatus",
    "product_category",
    "update",
    "candidate",
    "validated",
    "holiday",
    "lifetime",
    "shoulder",
    "at",
    "region",
)


def _spec(key: str, mode: str = "ratio") -> BuildSpec:
    ratios = {"train": 0.8, "test": 0.2} if mode == "ratio" else {}
    return BuildSpec(
        dataset_id="dataset.sample",
        title="Sample",
        description="desc",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="markdown", output_path="README.md"),),
        splits=SplitSpec(mode=mode, ratios=ratios, key=key),
    )


@pytest.mark.parametrize("key", _TIME_NAMES)
def test_time_column_names_still_get_the_leakage_warning(key: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_spec(_spec(key))
    problems = exc_info.value.structured_problems or []
    assert [p.code for p in problems] == ["time_column_random_split"]
    assert problems[0].path == "splits"
    assert repr(key) in problems[0].message


@pytest.mark.parametrize("key", _OTHER_NAMES)
def test_other_column_names_pass_a_ratio_split(key: str) -> None:
    validate_spec(_spec(key))


@pytest.mark.parametrize("key", _TIME_NAMES)
def test_key_mode_is_never_warned(key: str) -> None:
    validate_spec(_spec(key, mode="key"))


def test_every_name_warned_now_was_warned_before() -> None:
    # The fix only removes warnings: no spec that validated before is refused now.
    for key in _TIME_NAMES:
        assert any(ind in key.lower() for ind in _OLD_SUBSTRINGS), key


def test_several_negative_examples_were_false_positives_before() -> None:
    for key in ("category", "status", "rate", "seats", "width"):
        assert any(ind in key.lower() for ind in _OLD_SUBSTRINGS), key


def test_ratio_split_membership_does_not_depend_on_the_key_name() -> None:
    records = [{"category": f"c{i % 3}", "n": i} for i in range(50)]
    baseline = apply_splits(
        records, SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, seed=7)
    )
    for key in ("category", "status", "date", "created_at"):
        spec = SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, key=key, seed=7)
        assert apply_splits(records, spec) == baseline
    assert {name: len(rows) for name, rows in baseline.items()} == {"train": 40, "test": 10}
