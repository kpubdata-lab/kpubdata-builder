"""Single source-of-truth rule for reading run terminal state from recorded manifest.json (#481).

Since manifest.json is canonical for run results (ADR 0003), rules determining "what terminal state
is this manifest" are also owned by manifest package. BuildIndex rebuild (``store.rebuild_index``),
``GET /builds`` filesystem fallback, dataset grouping (``service.datasets``) all
reuse this function,
eliminating drift where same manifest reads as different status across code paths.
"""

from __future__ import annotations

# Run terminal state vocabulary shared by index/wire (same values as
# ``store.build_index.BuildStatus``).
# Do not create new values. To avoid index layer reverse-referencing manifest
# layer, kept as internal
# module constant instead of import, not exposed in public API — only judgment
# function needed externally.
_KNOWN_MANIFEST_STATUSES: frozenset[str] = frozenset({"ok", "failed", "cancelled"})


def status_from_manifest(manifest: dict[str, object], *, fallback_status: str | None = None) -> str:
    """Determine run terminal state from single manifest (``ok``/``failed``/``cancelled``).

    Decision order:
        1. If explicit ``status`` (#481, additive) exists, use it as-is —
           especially ``cancelled`` can't be restored from ``errors`` alone (cancelled run
           may have empty ``errors``).
        2. Legacy manifest (no this field) derives from ``errors`` presence as before
           — backward compat behavior unchanged.
        3. If none of above, use ``fallback_status`` if derived index knew
           ``cancelled`` (derived index value doesn't override canonical
           value).

    Args:
        manifest: Parsed manifest.json mapping.
        fallback_status: Status known by derived BuildIndex (optional).

    Returns:
        ``"ok"`` | ``"failed"`` | ``"cancelled"``.
    """
    explicit_status = manifest.get("status")
    if isinstance(explicit_status, str) and explicit_status in _KNOWN_MANIFEST_STATUSES:
        return explicit_status
    if manifest.get("errors"):
        return "failed"
    return "cancelled" if fallback_status == "cancelled" else "ok"


def run_status_from_manifest(
    manifest: dict[str, object], *, fallback_status: str | None = None
) -> str:
    """The state a caller is told a run ended in (``ok``/``failed``/``cancelled``) (#1106).

    ``status_from_manifest`` says whether the **build** produced its artifacts. A build
    can do that and still fail: its table was not committed (``warehouse_failures``,
    #788). The request that ran it answered 409 and its job ended ``failed`` (#997),
    while the build list, reading the manifest's ``ok`` alone, showed the same run as
    succeeded. This is the one reading for every place that reports a run's outcome —
    the list, the index it is served from, the admin list, the job status restored
    from a manifest — so they cannot disagree.

    What decides whether the artifacts can be used — publishing, retention, the drift
    baseline — keeps asking ``status_from_manifest``: the files of such a run are whole.
    """
    status = status_from_manifest(manifest, fallback_status=fallback_status)
    if status == "ok" and manifest.get("warehouse_failures"):
        return "failed"
    return status


def run_failure_summary(manifest: dict[str, object]) -> str | None:
    """Why a run failed, in one line for its index entry and the admin list (#1120).

    The manifest is the record; the index holds this projection of it, so a rebuild of
    the index from the manifests gives the same line. The first ``failures`` entry
    decides; a manifest written before #1120 falls back to its first ``errors`` entry,
    then to its first refused table commit. None for a run nothing in failed.

    Every source of the line is already safe to show: fixed sentences and public
    messages, written after the run's keys were redacted from the manifest.
    """
    failures = manifest.get("failures")
    if isinstance(failures, list):
        for failure in failures:
            if isinstance(failure, dict):
                key, summary = failure.get("source_key"), failure.get("summary")
                if isinstance(summary, str) and summary:
                    return f"{key}: {summary}" if isinstance(key, str) and key else summary
    errors = manifest.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, str) and error:
                return error
    warehouse = manifest.get("warehouse_failures")
    if isinstance(warehouse, dict):
        for key, failure in warehouse.items():
            if isinstance(failure, dict):
                detail = failure.get("detail") or failure.get("reason")
                if isinstance(detail, str) and detail:
                    return f"{key}: {detail}"
    return None


__all__ = ["run_failure_summary", "run_status_from_manifest", "status_from_manifest"]
