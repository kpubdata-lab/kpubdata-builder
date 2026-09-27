"""Dataset Quality History aggregation logic (#486).

Contains pure read-only logic used by ``GET /datasets/{dataset_id}/quality/history``.
Dataset→run lookup reuses helpers from #488's ``datasets`` module
(``BuilderService._dataset_records_for`` etc.) — does not create new dataset
grouping/index. This module only aggregates ``quality_results`` from each run's
manifest into pass/warn/fail summaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Literal

from ..spec import JsonValue
from .datasets import RunRecord

Availability = Literal["available", "partial", "unavailable"]

# Only window supported by recent quality aggregate (same vocabulary as monitoring #516).
QUALITY_SUMMARY_WINDOW_SECONDS = 24 * 3600


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Parse ISO 8601 string to UTC ``datetime``. Returns None if invalid/None.

    Uses the same strict rules as ``monitoring._parse_iso_utc`` — avoids naive
    local time comparison by treating missing tz info as UTC. Since monitoring
    imports quality, this module holds an identical helper to avoid back-reference.
    """
    if not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validated_rows(manifest: dict[str, object]) -> int | None:
    """Calculate run's validated_rows from manifest.row_counts sum (#486).

    Summing each QualityCheckResult.evaluated_rows double-counts the same Silver
    row by the number of rules, so is not used. Instead, reuses row_counts
    semantics already defined in #488 (sum of Silver row_counts per source).
    Multi-source datasets sum per source; does not arbitrarily pick first source.
    """
    raw = manifest.get("row_counts")
    if not isinstance(raw, dict):
        return None
    total = 0
    found = False
    for value in raw.values():
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
            found = True
    return total if found else None


def summarize_run_quality(record: RunRecord, manifest: dict[str, object]) -> dict[str, JsonValue]:
    """Summarize run's quality_results into pass/warn/fail aggregate (#486).

    Legacy runs (missing quality_results field) are represented as evaluated_checks=0,
    rule_pass_rate=None — unevaluated checks are not interpreted as "all PASS".
    Partial/failed runs with structured quality_results are included in aggregate
    as-is (not excluded from history by policy).
    """
    raw_quality = manifest.get("quality_results")
    pass_count = warn_count = fail_count = 0
    if isinstance(raw_quality, dict):
        for source_results in raw_quality.values():
            if not isinstance(source_results, list):
                continue
            for entry in source_results:
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if status == "pass":
                    pass_count += 1
                elif status == "warn":
                    warn_count += 1
                elif status == "fail":
                    fail_count += 1

    evaluated_checks = pass_count + warn_count + fail_count
    rule_pass_rate = (pass_count / evaluated_checks) if evaluated_checks > 0 else None

    return {
        "run_id": record.run_id,
        "timestamp": record.finished_at or record.started_at,
        "status": record.status,
        "pass_count": pass_count,
        "warn_count": warn_count,
        "fail_count": fail_count,
        "evaluated_checks": evaluated_checks,
        "rule_pass_rate": rule_pass_rate,
        "validated_rows": _validated_rows(manifest),
    }


def quality_availability(
    manifest: dict[str, object], known_sources: tuple[str, ...]
) -> tuple[Availability, int]:
    """Determine availability/evaluated_checks for ``GET /builds/{run_id}/quality`` (#514).

    Empty ``{"quality_results": {}, "schema_drift": {}}`` alone cannot distinguish
    "evaluated but zero checks" from "never calculated" — this function makes that
    distinction.

    - ``unavailable``: No results at all. Includes legacy runs (missing
      quality_results field, before #486) and new runs with field but not covering
      any known source (``quality_results: {}``) — manifest writer always records
      empty ``{}`` even if no quality was calculated; actually occurs in runs where
      all sources failed before quality stage entry.
    - ``partial``: quality_results exists but covers only some of the sources this
      run actually attempted (``stages.known_source_keys``) — example: multi-source
      run where one source's Silver failed so quality evaluation didn't run.
    - ``available``: Covers all known sources (includes legacy manifest with empty
      known_sources). May have evaluated_checks==0 — treated separate as available
      to distinguish zero evaluated rules from absent results.
    """
    raw_quality = manifest.get("quality_results")
    if not isinstance(raw_quality, dict):
        return "unavailable", 0

    evaluated_checks = 0
    for source_results in raw_quality.values():
        if not isinstance(source_results, list):
            continue
        for entry in source_results:
            if isinstance(entry, dict) and entry.get("status") in ("pass", "warn", "fail"):
                evaluated_checks += 1

    known = set(known_sources)
    if known:
        covered = known & raw_quality.keys()
        if not covered:
            return "unavailable", evaluated_checks
        if covered != known:
            return "partial", evaluated_checks
    return "available", evaluated_checks


def aggregate_quality_window(
    entries: Iterable[tuple[RunRecord, dict[str, object] | None]],
    *,
    now: datetime,
    window_seconds: int = QUALITY_SUMMARY_WINDOW_SECONDS,
) -> dict[str, JsonValue]:
    """Aggregate PASS/WARN/FAIL run counts for ``GET /quality/summary`` (#486 follow-up,
    additive).

    ``entries`` are canonical runs (``RunRecord``) and manifests already passing
    principal access filter (#505 ownership). Here only time-window filter and
    per-run aggregation are applied.

    - Time basis: canonical run timestamp (``finished_at``, or ``started_at``) parsed
      to UTC; only runs in ``(now - window, now]`` are counted. Unparseable timestamps
      (legacy/malformed) cannot be placed in window, so excluded — no naive local time
      comparison.
    - ``total_runs``: run count in window (status-independent).
    - ``evaluated_runs``: count of runs where structured quality was actually evaluated
      (``evaluated_checks > 0``). Excludes unavailable/0-check runs — unevaluated is
      not counted as PASS.
    - ``warn_runs``: count of evaluated runs with at least one WARN result. Multiple
      WARN checks in same run count as 1 run. Studio Home's "QUALITY WARN (24H)" KPI
      uses this value.
    - ``fail_runs``: count of evaluated runs with at least one FAIL result.
      Independent of WARN requirement, so may overlap with ``warn_runs`` (run with
      both WARN and FAIL).
    - ``pass_runs``: count of evaluated runs with no WARN/FAIL results.
      ``pass_runs + (runs with WARN or FAIL) == evaluated_runs``.
    """
    lower = now - timedelta(seconds=window_seconds)
    total = evaluated = pass_runs = warn_runs = fail_runs = 0
    for record, manifest in entries:
        timestamp = _parse_iso_utc(record.finished_at or record.started_at)
        if timestamp is None or not (lower < timestamp <= now):
            continue
        total += 1
        if manifest is None:
            continue
        summary = summarize_run_quality(record, manifest)
        checks = summary["evaluated_checks"]
        if not isinstance(checks, int) or checks <= 0:
            continue
        evaluated += 1
        warn = summary["warn_count"]
        fail = summary["fail_count"]
        has_warn = isinstance(warn, int) and warn > 0
        has_fail = isinstance(fail, int) and fail > 0
        if has_warn:
            warn_runs += 1
        if has_fail:
            fail_runs += 1
        if not has_warn and not has_fail:
            pass_runs += 1
    return {
        "total_runs": total,
        "evaluated_runs": evaluated,
        "pass_runs": pass_runs,
        "warn_runs": warn_runs,
        "fail_runs": fail_runs,
    }


__all__ = [
    "Availability",
    "QUALITY_SUMMARY_WINDOW_SECONDS",
    "aggregate_quality_window",
    "quality_availability",
    "summarize_run_quality",
]
