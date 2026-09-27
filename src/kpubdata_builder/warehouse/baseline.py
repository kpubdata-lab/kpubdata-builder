"""Choose a drift baseline that is actually comparable (#700).

Drift is how a warehouse says "something changed at the source". The previous
implementation picked the most recent successful run with a matching dataset and
source key, by scanning the filesystem. Two things were wrong with that in a shared
deployment.

**Someone else's metadata.** Another user's run could become the baseline. No raw
data leaks, but row count, schema and distribution changes are a metadata side
channel — and that is precisely what drift reports.

**Incomparable coverage.** Collect Seoul 2025, then Busan 2026, and the row-count
drift is computed between two different populations. That number means nothing, and
a meaningless number in a quality report is worse than no number.

So a baseline is a **committed snapshot of the same table, the same owner,
comparable coverage and the same schema contract.**

Schema drift and volume drift do not share a baseline. Comparing columns across a
coverage change is fine — the columns should be the same either way. Comparing row
counts across one is not. Selection is therefore per axis
(:class:`DriftAxis`), and the criteria differ.

When nothing qualifies, the result is :class:`NotEvaluated` with a reason, never an
empty finding list. "No baseline" and "compared, nothing changed" are different
answers, and the older code made them the same one: an absent manifest key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .catalog import SnapshotRow, TableCatalog


class DriftAxis(Enum):
    """Which comparison a baseline is being chosen for.

    ``SCHEMA`` tolerates a coverage change: the column set should not depend on
    which region was collected. ``VOLUME`` does not, because a row count only means
    something against the same population.
    """

    SCHEMA = "schema"
    VOLUME = "volume"


class NotEvaluatedReason(Enum):
    """Why no baseline could be chosen.

    Each value is a distinct answer a report can show. Collapsing them into "no
    findings" is the defect this module exists to remove.
    """

    NO_COMMITTED_SNAPSHOT = "no_committed_snapshot"
    """The table has never had a snapshot committed by this owner."""

    OWNER_UNKNOWN = "owner_unknown"
    """The candidate carries no owner, so it cannot be proven to be the same one."""

    COVERAGE_MISMATCH = "coverage_mismatch"
    """Every candidate collected a different population."""

    COVERAGE_UNKNOWN = "coverage_unknown"
    """A candidate has no coverage fingerprint, so comparability is unproven."""

    SCHEMA_CONTRACT_CHANGED = "schema_contract_changed"
    """The schema contract moved, which invalidates a volume baseline."""


@dataclass(frozen=True)
class BaselineFound:
    """A comparable baseline was chosen.

    Attributes:
        snapshot: The committed snapshot to compare against.
        axis: Which comparison it was chosen for.
    """

    snapshot: SnapshotRow
    axis: DriftAxis

    @property
    def evaluated(self) -> bool:
        """True. Present so callers can branch without an isinstance check."""
        return True


@dataclass(frozen=True)
class NotEvaluated:
    """No comparable baseline exists, with the reason.

    This is **not** the same as "no drift". A caller that reports an empty finding
    list here is reporting a healthy table it never checked.

    Attributes:
        reason: Why selection failed.
        axis: Which comparison was attempted.
        detail: Human-readable context, safe to show in a report.
    """

    reason: NotEvaluatedReason
    axis: DriftAxis
    detail: str

    @property
    def evaluated(self) -> bool:
        """False. Present so callers can branch without an isinstance check."""
        return False


BaselineOutcome = BaselineFound | NotEvaluated
"""Either a baseline or a stated reason there is none. There is no third case, and
in particular no ``None`` — an optional return is what let the previous code treat
"nothing to compare" as "nothing wrong"."""


def select_baseline(
    catalog: TableCatalog,
    table_id: str,
    *,
    axis: DriftAxis,
    owner_id: str,
    coverage_fingerprint: str | None = None,
    schema_contract_version: str | None = None,
    exclude_snapshot_id: str | None = None,
) -> BaselineOutcome:
    """Pick the newest committed snapshot that may be compared against.

    Candidates come from the catalog, not from walking the output directory: a
    baseline has to be a committed snapshot, or "the previous state" is whatever
    happens to be on disk.

    Rules, in the order they reject:

    1. Committed only. Staging, validated and quarantined snapshots are not a
       previous state anyone read.
    2. Same owner. A snapshot with no recorded owner is rejected rather than
       assumed to be ours.
    3. For ``VOLUME``: same coverage fingerprint, and same schema contract. An
       unknown value on either side is a rejection — an unlabelled snapshot is
       never silently compared.
    4. For ``SCHEMA``: coverage may differ, since the column set should not depend
       on it.

    Args:
        catalog: Where committed snapshots are recorded.
        table_id: The table being refreshed.
        axis: Which comparison the baseline is for.
        owner_id: The owner of the current run. Baselines never cross owners.
        coverage_fingerprint: The current run's coverage. Required for ``VOLUME``.
        schema_contract_version: The current schema contract. Required for
            ``VOLUME``.
        exclude_snapshot_id: The snapshot being produced, so a run cannot become
            its own baseline.

    Returns:
        :class:`BaselineFound` or :class:`NotEvaluated`. Never ``None``.
    """
    candidates = [
        s
        for s in catalog.list_snapshots(table_id)
        if s.state == "committed" and s.id != exclude_snapshot_id
    ]
    if not candidates:
        return NotEvaluated(
            NotEvaluatedReason.NO_COMMITTED_SNAPSHOT,
            axis,
            f"table {table_id} has no committed snapshot to compare against",
        )

    owned = [s for s in candidates if s.owner_id == owner_id]
    if not owned:
        # Distinguish "everything belongs to someone else" from "nothing says who
        # owns it". The second is a recording gap worth fixing; the first is the
        # isolation working.
        unlabelled = any(s.owner_id is None for s in candidates)
        return NotEvaluated(
            NotEvaluatedReason.OWNER_UNKNOWN
            if unlabelled
            else NotEvaluatedReason.NO_COMMITTED_SNAPSHOT,
            axis,
            f"no committed snapshot of {table_id} belongs to {owner_id}"
            + (" and some carry no owner" if unlabelled else ""),
        )

    if axis is DriftAxis.SCHEMA:
        # Coverage may differ: which region was collected should not change the
        # column set, and if it does, that is drift worth reporting.
        return BaselineFound(owned[0], axis)

    if coverage_fingerprint is None:
        return NotEvaluated(
            NotEvaluatedReason.COVERAGE_UNKNOWN,
            axis,
            "the current run has no coverage fingerprint, so a row count cannot be "
            "compared to anything",
        )
    if schema_contract_version is None:
        return NotEvaluated(
            NotEvaluatedReason.SCHEMA_CONTRACT_CHANGED,
            axis,
            "the current run has no schema contract version, so a volume "
            "comparison cannot be shown to be valid",
        )

    same_coverage = [s for s in owned if s.coverage_fingerprint == coverage_fingerprint]
    if not same_coverage:
        return NotEvaluated(
            NotEvaluatedReason.COVERAGE_MISMATCH,
            axis,
            f"no committed snapshot of {table_id} covers {coverage_fingerprint}; "
            "comparing row counts across populations would be meaningless",
        )

    comparable = [s for s in same_coverage if s.schema_contract_version == schema_contract_version]
    if not comparable:
        return NotEvaluated(
            NotEvaluatedReason.SCHEMA_CONTRACT_CHANGED,
            axis,
            f"the schema contract moved to {schema_contract_version}; the volume "
            "baseline is invalidated rather than compared silently",
        )
    return BaselineFound(comparable[0], axis)


__all__ = [
    "BaselineFound",
    "BaselineOutcome",
    "DriftAxis",
    "NotEvaluated",
    "NotEvaluatedReason",
    "select_baseline",
]
