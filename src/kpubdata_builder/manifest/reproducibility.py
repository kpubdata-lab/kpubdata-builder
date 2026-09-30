"""Whether a run can stand for "same recipe, same output" (#648, owner decision D4).

A build that resumed a ``param_grid`` fetch from a checkpoint is allowed, but its records
came from two fetches at two times: rebuilding the same spec is not expected to give the
same bytes. Its manifest says so in ``reproducibility``, and the R1 comparison —
rebuilding a run and comparing outputs — leaves it out. A run that fetched everything in
one go has no ``reproducibility`` entry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..spec import JsonValue

#: The reason recorded when a source resumed from a checkpoint.
RESUMED_FROM_CHECKPOINT = "resumed_from_checkpoint"


def not_reproducible(
    resumed_sources: Mapping[str, Mapping[str, JsonValue]],
) -> dict[str, JsonValue]:
    """The manifest entry for a run that resumed: why, and which sources, how far."""
    return {
        "reproducible": False,
        "reason": RESUMED_FROM_CHECKPOINT,
        "resumed_sources": {key: dict(value) for key, value in resumed_sources.items()},
    }


def is_reproducible(manifest: Mapping[str, object]) -> bool:
    """Whether a manifest's run may be used as an R1 reference."""
    entry = manifest.get("reproducibility")
    return not (isinstance(entry, Mapping) and entry.get("reproducible") is False)


def reproducible_runs(
    manifests: Iterable[tuple[str, Mapping[str, object]]],
) -> list[str]:
    """The run ids, of ``(run_id, manifest)`` pairs, an R1 comparison may use."""
    return [run_id for run_id, manifest in manifests if is_reproducible(manifest)]


__all__ = [
    "RESUMED_FROM_CHECKPOINT",
    "is_reproducible",
    "not_reproducible",
    "reproducible_runs",
]
