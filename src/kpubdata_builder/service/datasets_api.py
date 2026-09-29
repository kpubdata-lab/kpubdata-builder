"""Dataset domain service (#596, fourth piece).

Holds the query surface (``dataset_id``-grouped built datasets, #488/#486).
Same rules as previous pieces — **self-dependency only**; does not touch wire
contract.

This domain **shares run record collection helpers with the quality domain.** So
helpers are public methods, not private — when the quality piece is moved, it can
import ``DatasetsApiService`` as a dependency and reuse the same collection
logic, with no code duplication.

The core of this file is applying ownership filter **before** grouping/latest
selection (#488 semantics D). If the order is reversed, other users' runs with
the same ``dataset_id`` mix into latest candidates.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from kpubdata_builder.service import datasets as datasets_service
from kpubdata_builder.service import quality as quality_service
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import BuildJobSnapshot
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.artifacts import ArtifactStore
from kpubdata_builder.store.build_index import BuildIndex

# BuildIndex window query is enhanced by manifest mtime margin. mtime is from the
# source-of-truth file created at completion, so it's always >= finished_at, but
# we absorb post-completion re-recording (e.g., secret redaction), clock skew,
# and filesystem mtime resolution by lowering the window lower bound by this amount.
# Exact boundary is re-applied by quality.aggregate_quality_window using canonical
# timestamp.
QUALITY_WINDOW_MTIME_MARGIN_SECONDS = 3600


class DatasetsApiService:
    """Built dataset list/detail/run history/quality history (#488/#486)."""

    def __init__(
        self,
        *,
        output_root: Path,
        build_index: BuildIndex,
        store: ArtifactStore,
        enforce_ownership: bool | None = None,
        active_runs: Callable[[], Sequence[BuildJobSnapshot]] = lambda: (),
    ) -> None:
        self._output_root = output_root
        self._build_index = build_index
        self._store = store
        # Queued and running async jobs, so a table can say it is being refreshed (#781).
        self._active_runs = active_runs
        # If None, re-read environment at call time — preserving test pattern of
        # toggling via env (#389).
        self._enforce_ownership_override = enforce_ownership

    def _enforce_ownership(self) -> bool:
        if self._enforce_ownership_override is not None:
            return self._enforce_ownership_override
        from kpubdata_builder.service import ownership as ownership_module

        return ownership_module.enforce_ownership()

    def _active_statuses(self, principal: Principal | None) -> dict[str, list[str]]:
        """Status of each in-progress job the principal may see, by dataset_id (#781).

        The same ownership rule as the run records: another user's queued refresh must
        not show up in this user's table any more than their finished run does.
        """
        from kpubdata_builder.service import ownership as ownership_module

        enforce = self._enforce_ownership()
        by_dataset: dict[str, list[str]] = {}
        for job in self._active_runs():
            if job.dataset_id is None:
                continue
            if principal is not None and not ownership_module.ownership_allows(
                created_by=job.created_by,
                owner_id=job.owner_id,
                principal=principal,
                enforce=enforce,
            ):
                continue
            by_dataset.setdefault(job.dataset_id, []).append(job.status)
        return by_dataset

    # --- Run record collection (shared with quality domain) ---

    def _canonical_records(self) -> list[datasets_service.RunRecord]:
        """Every canonical run with a dataset_id, before any ownership filter."""
        index_records = datasets_service.collect_run_records_from_index(self._build_index) or []
        filesystem_records = datasets_service.collect_run_records_from_filesystem(self._output_root)
        records = datasets_service.merge_run_records(index_records, filesystem_records)
        return datasets_service.retain_canonical_run_records(self._output_root, records)

    def dataset_records(self, principal: Principal | None) -> list[datasets_service.RunRecord]:
        """Get all accessible runs with dataset_id (index-first, filesystem fallback).

        Apply ownership filter before grouping/latest selection — ensure other
        users' runs with the same dataset_id do not mix into latest candidates
        (#488 semantics D).
        """
        return datasets_service.filter_ownership(
            self._canonical_records(), principal, enforce=self._enforce_ownership()
        )

    def dataset_records_for(
        self, dataset_id: str, principal: Principal | None
    ) -> list[datasets_service.RunRecord]:
        """Get all accessible canonical runs for a specific dataset_id."""
        return [
            record for record in self.dataset_records(principal) if record.dataset_id == dataset_id
        ]

    def recent_canonical_records(
        self, principal: Principal | None, *, now: datetime, window_seconds: int
    ) -> list[datasets_service.RunRecord]:
        """Confirm candidate runs within window as canonical source-of-truth (#488 follow-up).

        Candidate run_id is the union of two signals:
          - Canonical ``manifest.json`` mtime within window (margin included).
          - ``BuildIndex.list_between`` runs within window (derived index fast lookup).

        BuildIndex is a derived search index with best-effort writes (ADR 0003) —
        if omissions or stale rows change the 24h aggregate, that's wrong, so we
        don't narrow by index alone; we enhance with mtime candidates. Index
        query failure is fully covered by mtime candidates.
        """
        margin_seconds = window_seconds + QUALITY_WINDOW_MTIME_MARGIN_SECONDS
        mtime_cutoff = now.timestamp() - margin_seconds
        candidate_run_ids: set[str] = set()
        if self._output_root.exists():
            for run_dir in self._output_root.iterdir():
                if not run_dir.is_dir():
                    continue
                try:
                    manifest_mtime = (run_dir / "manifest.json").stat().st_mtime
                except OSError:
                    continue  # Manifest missing/unreachable — not a completed run
                if manifest_mtime >= mtime_cutoff:
                    candidate_run_ids.add(run_dir.name)
        lower = (now - timedelta(seconds=margin_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        upper = (now + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Derived index query failure is fully covered by mtime candidates.
        with suppress(Exception):
            candidate_run_ids.update(
                entry.run_id for entry in self._build_index.list_between(lower, upper)
            )
        canonical = datasets_service.canonical_records_for_run_ids(
            self._output_root, candidate_run_ids
        )
        return datasets_service.filter_ownership(
            canonical, principal, enforce=self._enforce_ownership()
        )

    # --- Public endpoints ---

    def list_datasets(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """Return list of built datasets, grouping multiple runs of same dataset_id into one (#488).

        ``total`` is the count of distinct datasets after canonical grouping +
        ownership filter, before pagination (limit). Expensive full summary is
        only performed on response page candidates; after page is filled, only
        lightweight renderability check is done — preventing regression where
        full summary runs on entire catalog with limit=1 + large dataset count
        (#488 follow-up review).
        """
        records = self.dataset_records(principal)
        latest_by_dataset = datasets_service.group_latest_by_dataset(records)
        ordered = sorted(
            latest_by_dataset.values(),
            key=datasets_service.sort_key,
            reverse=True,
        )
        items: list[JsonValue] = []
        total = 0
        active = self._active_statuses(principal)
        for record in ordered:
            if len(items) < limit:
                view = datasets_service.build_dataset_summary(
                    self._output_root,
                    record,
                    active_statuses=active.get(record.dataset_id, ()),
                )
                if view is None:
                    continue
                items.append(view)
                total += 1
            elif datasets_service.dataset_summary_renderable(self._output_root, record):
                total += 1
        return ServiceResponse(200, {"datasets": items, "total": total})

    def get_dataset(
        self, dataset_id: str, *, principal: Principal | None = None
    ) -> ServiceResponse:
        """Return canonical summary of a single built dataset (#488).

        Return 404 if no accessible run exists (dataset_id truly missing, or all
        runs are owned by other users) — do not distinguish between the two cases.
        """
        records = self.dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        latest = datasets_service.pick_latest(records)
        view = datasets_service.build_dataset_summary(
            self._output_root,
            latest,
            active_statuses=self._active_statuses(principal).get(dataset_id, ()),
        )
        if view is None:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        view["run_count"] = len(records)
        return ServiceResponse(200, view)

    def list_dataset_runs(
        self, dataset_id: str, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """Return accessible run history for dataset_id in most-recent order (#488)."""
        records = self.dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        ordered = sorted(records, key=datasets_service.sort_key, reverse=True)[:limit]
        runs: list[JsonValue] = [_run_item(r) for r in ordered]
        return ServiceResponse(200, {"dataset_id": dataset_id, "runs": runs})

    def get_dataset_run(
        self, dataset_id: str, run_id: str, *, principal: Principal | None = None
    ) -> ServiceResponse:
        """Return one run of dataset_id, found directly rather than in a page (studio#418).

        The runs list returns the most recent `limit` runs. A permalink to an older run is
        still valid, and the list cannot say so; this can. The server decides both facts a
        client must not decide for itself:

            404  no run with this id belongs to this dataset — missing, or another
                 dataset's run. Both mean "this URL does not open".
            403  the run belongs to the dataset, but not to this principal.

        The body is one item of the runs list, so a client treats both the same way.
        """
        record = next(
            (
                r
                for r in self._canonical_records()
                if r.run_id == run_id and r.dataset_id == dataset_id
            ),
            None,
        )
        if record is None:
            return ServiceResponse(
                404, {"error": f"run not found in dataset {dataset_id}: {run_id}"}
            )
        if not datasets_service.filter_ownership(
            [record], principal, enforce=self._enforce_ownership()
        ):
            return ServiceResponse(403, {"error": "forbidden: not run owner"})
        return ServiceResponse(200, {"dataset_id": dataset_id, "run": _run_item(record)})

    def get_dataset_quality_history(
        self, dataset_id: str, *, limit: int = 30, principal: Principal | None = None
    ) -> ServiceResponse:
        """Return quality PASS/WARN/FAIL aggregate history for accessible runs of dataset_id (#486).

        Dataset→run queries reuse ``dataset_records_for`` (ownership included) —
        no new grouping/index. Existence/ownership judgment is same as other
        dataset endpoints.
        """
        records = self.dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        ordered = sorted(records, key=datasets_service.sort_key, reverse=True)[:limit]
        runs: list[JsonValue] = []
        for r in ordered:
            manifest = self._store.get_manifest(r.run_id) or {}
            runs.append(cast(JsonValue, quality_service.summarize_run_quality(r, manifest)))
        return ServiceResponse(200, {"dataset_id": dataset_id, "runs": runs})


def _run_item(record: datasets_service.RunRecord) -> JsonValue:
    """One run as the runs list and the direct lookup both send it."""
    return {
        "run_id": record.run_id,
        "status": record.status,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "spec_digest": record.spec_digest,
        "created_by": record.created_by,
    }


__all__ = ["DatasetsApiService", "QUALITY_WINDOW_MTIME_MARGIN_SECONDS"]
