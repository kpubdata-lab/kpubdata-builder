"""Verify build diff/compare tool (#16)."""

from __future__ import annotations

from datetime import datetime, timezone

from kpubdata_builder.diff import BuildDiff, DiffItem, compare_manifests
from kpubdata_builder.manifest import BuildManifest

_TS = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)


def _manifest(
    build_id: str,
    *,
    inputs: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    row_counts: dict[str, int] | None = None,
) -> BuildManifest:
    return BuildManifest(
        build_id=build_id,
        started_at=_TS,
        finished_at=_TS,
        inputs=inputs,
        outputs=outputs,
        errors=errors,
        warnings=warnings,
        row_counts=row_counts or {},
    )


def test_identical_manifests_have_no_diffs() -> None:
    a = _manifest("a", inputs=("datago.apt_trade",), row_counts={"datago.apt_trade": 10})
    b = _manifest("b", inputs=("datago.apt_trade",), row_counts={"datago.apt_trade": 10})

    result = compare_manifests(a, b)

    assert isinstance(result, BuildDiff)
    assert result.diffs == ()
    assert result.changed is False
    assert result.summary == "no changes"
    assert (result.manifest_a, result.manifest_b) == ("a", "b")


def test_detects_row_count_modification() -> None:
    a = _manifest("a", row_counts={"datago.apt_trade": 1000})
    b = _manifest("b", row_counts={"datago.apt_trade": 1200})

    result = compare_manifests(a, b)

    assert DiffItem("row_count:datago.apt_trade", "1000", "1200", "modified") in result.diffs
    assert result.changed is True


def test_detects_added_and_removed_sources() -> None:
    a = _manifest("a", inputs=("datago.apt_trade",))
    b = _manifest("b", inputs=("datago.air_quality",))

    result = compare_manifests(a, b)

    assert DiffItem("source:datago.air_quality", "", "datago.air_quality", "added") in result.diffs
    assert DiffItem("source:datago.apt_trade", "datago.apt_trade", "", "removed") in result.diffs


def test_detects_added_row_count_and_output_and_error() -> None:
    a = _manifest("a")
    b = _manifest(
        "b",
        row_counts={"datago.apt_trade": 5},
        outputs=("out/data.jsonl",),
        errors=("datago.x: boom",),
    )

    result = compare_manifests(a, b)
    fields = {(item.field, item.change_type) for item in result.diffs}

    assert ("row_count:datago.apt_trade", "added") in fields
    assert ("output:out/data.jsonl", "added") in fields
    assert ("error:datago.x: boom", "added") in fields


def test_summary_counts_change_types() -> None:
    a = _manifest("a", inputs=("s1",), row_counts={"s1": 10})
    b = _manifest("b", inputs=("s2",), row_counts={"s1": 20})

    result = compare_manifests(a, b)

    # s2 added, s1 removed (source), row_count s1 modified.
    assert result.summary == "3 change(s): 1 added, 1 removed, 1 modified"


def test_diffs_are_deterministic_order() -> None:
    a = _manifest("a", row_counts={})
    b = _manifest("b", row_counts={"b_src": 1, "a_src": 1})

    result = compare_manifests(a, b)

    fields = [item.field for item in result.diffs]
    assert fields == sorted(fields)
