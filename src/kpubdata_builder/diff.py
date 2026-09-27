"""Diff tool comparing two build manifests (#16).

Takes two BuildManifests and returns structured diff list of changes in
sources/record count/artifacts/errors·warnings.
Allows at-a-glance understanding of what was added/deleted/changed between builds.

Key components:
    - DiffItem: Single change item
    - BuildDiff: Comparison result
    - compare_manifests: Compare two BuildManifests
"""

from __future__ import annotations

from dataclasses import dataclass

from .manifest import BuildManifest

ADDED = "added"
REMOVED = "removed"
MODIFIED = "modified"


@dataclass(frozen=True)
class DiffItem:
    """Single change item between two builds.

    Attributes:
        field: Changed item identifier (e.g. "row_count:datago.apt_trade").
        old_value: Previous value (empty string if added).
        new_value: New value (empty string if deleted).
        change_type: "added" | "removed" | "modified".
    """

    field: str
    old_value: str
    new_value: str
    change_type: str


@dataclass(frozen=True)
class BuildDiff:
    """Result of comparing two BuildManifests.

    Attributes:
        manifest_a: Base (previous) build ID.
        manifest_b: Comparison (subsequent) build ID.
        diffs: List of changes (deterministic order).
        summary: Human-readable one-line summary.
    """

    manifest_a: str
    manifest_b: str
    diffs: tuple[DiffItem, ...]
    summary: str

    @property
    def changed(self) -> bool:
        """True if there is at least one change item."""
        return bool(self.diffs)


def _diff_set(field_prefix: str, before: tuple[str, ...], after: tuple[str, ...]) -> list[DiffItem]:
    """Compare additions and deletions of set-type fields (sources/artifacts)."""
    before_set, after_set = set(before), set(after)
    items = [
        DiffItem(field=f"{field_prefix}:{value}", old_value="", new_value=value, change_type=ADDED)
        for value in sorted(after_set - before_set)
    ]
    items += [
        DiffItem(
            field=f"{field_prefix}:{value}", old_value=value, new_value="", change_type=REMOVED
        )
        for value in sorted(before_set - after_set)
    ]
    return items


def _diff_row_counts(before: dict[str, int], after: dict[str, int]) -> list[DiffItem]:
    """Compare additions, deletions, and changes in record count per source."""
    items: list[DiffItem] = []
    for key in sorted(set(before) | set(after)):
        in_before, in_after = key in before, key in after
        if in_before and in_after:
            if before[key] != after[key]:
                items.append(
                    DiffItem(
                        field=f"row_count:{key}",
                        old_value=str(before[key]),
                        new_value=str(after[key]),
                        change_type=MODIFIED,
                    )
                )
        elif in_after:
            items.append(
                DiffItem(
                    field=f"row_count:{key}",
                    old_value="",
                    new_value=str(after[key]),
                    change_type=ADDED,
                )
            )
        else:
            items.append(
                DiffItem(
                    field=f"row_count:{key}",
                    old_value=str(before[key]),
                    new_value="",
                    change_type=REMOVED,
                )
            )
    return items


def compare_manifests(a: BuildManifest, b: BuildManifest) -> BuildDiff:
    """Compare two BuildManifests to create BuildDiff.

    Args:
        a: Base (previous) manifest.
        b: Comparison (subsequent) manifest.

    Returns:
        BuildDiff: Changes in deterministic order and one-line summary.
    """
    diffs: list[DiffItem] = []
    diffs += _diff_set("source", a.inputs, b.inputs)
    diffs += _diff_row_counts(a.row_counts, b.row_counts)
    diffs += _diff_set("output", a.outputs, b.outputs)
    diffs += _diff_set("error", a.errors, b.errors)
    diffs += _diff_set("warning", a.warnings, b.warnings)

    if diffs:
        added = sum(1 for item in diffs if item.change_type == ADDED)
        removed = sum(1 for item in diffs if item.change_type == REMOVED)
        modified = sum(1 for item in diffs if item.change_type == MODIFIED)
        summary = f"{len(diffs)} change(s): {added} added, {removed} removed, {modified} modified"
    else:
        summary = "no changes"

    return BuildDiff(
        manifest_a=a.build_id, manifest_b=b.build_id, diffs=tuple(diffs), summary=summary
    )


__all__ = ["ADDED", "MODIFIED", "REMOVED", "BuildDiff", "DiffItem", "compare_manifests"]
