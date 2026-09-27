"""Build artifact query service (follow-up to #596, #637).

Provides **read-only surface** for completed runs — artifacts list, manifest, spec
snapshot, file serving, build list, event timeline.

Execution-side methods (``build``/``submit_build``/``cancel_build``) are kept separate.
That side holds eleven concerns (client factory, credential resolver, upload repository,
spec validation, etc.), so extracting it now would make the new service's constructor
receive essentially all of ``BuilderService`` again — exactly what #596 tried to remove.
Read paths use only four dependencies, so the boundary is genuinely narrower here.

**Wire contract unchanged.** ``BuilderService`` delegates with same signature.
"""

from __future__ import annotations

import heapq
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.parse import unquote

from kpubdata_builder.events import BuildEventStore
from kpubdata_builder.manifest import status_from_manifest
from kpubdata_builder.service import events as events_service
from kpubdata_builder.service import ownership as ownership_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import FileResponse, ServiceResponse
from kpubdata_builder.spec import JsonValue, compute_spec_digest
from kpubdata_builder.spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME
from kpubdata_builder.stages._path_safety import ensure_within, validate_path_segment
from kpubdata_builder.store.artifacts import ArtifactStore
from kpubdata_builder.store.build_index import BuildIndex

logger = logging.getLogger(__name__)

# Build list entry type for API responses
_BuildListEntry = dict[str, str | None]


def _apply_ownership(
    entries: list[_BuildListEntry], principal: Principal | None
) -> list[_BuildListEntry]:
    """Keep only own runs in list_builds response (#433, #505).

    Filter only when ENFORCE_OWNERSHIP+oidc principal. Dev/service principals and
    principal=None pass through (admin privilege + backward compatibility). Apply
    to both index branch and filesystem fallback so fallback path doesn't bypass
    filter.

    Each entry must carry internal-only "owner_id" key for decision — removed by
    ``_strip_internal_fields`` before response, so wire shape unchanged.
    """
    if not (ownership_module.enforce_ownership() and principal and principal.kind == "oidc"):
        return entries
    return [
        e
        for e in entries
        if ownership_module.ownership_allows(
            created_by=e.get("created_by"),
            owner_id=e.get("owner_id"),
            principal=principal,
            enforce=True,
        )
    ]


def _strip_internal_fields(entries: list[_BuildListEntry]) -> list[_BuildListEntry]:
    """Remove internal-only ownership fields from response before sending (#505).

    ``owner_id`` is a canonical hash meaningless to clients. Exposing it changes
    ``/builds`` response wire shape — use internally only, without contract change.
    """
    return [{k: v for k, v in e.items() if k != "owner_id"} for e in entries]


class BuildArtifactsApiService:
    """Query artifacts/manifest/spec/events for completed runs (#433, #487, #496)."""

    def __init__(
        self,
        *,
        output_root: Path,
        store: ArtifactStore,
        build_index: BuildIndex,
        event_store: Callable[[], BuildEventStore],
    ) -> None:
        self._output_root = output_root
        self._store = store
        self._build_index = build_index
        # Event store is lazily created (#496) — avoid sqlite file in preview-only
        # workspaces. Preserve this behavior by accepting accessor, not value.
        self._event_store_factory = event_store

    @property
    def _event_store(self) -> BuildEventStore:
        return self._event_store_factory()

    def artifacts(self, run_id: str) -> ServiceResponse:
        """Return list of artifact files in execution workspace."""
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.exists():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        # Wire always exposes POSIX relative paths from run directory — never absolute
        # path or OS-specific separators. `serve_artifact_file` receives this canonical
        # artifact identifier (clients don't need to know storage layout).
        files = sorted(
            path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()
        )
        return ServiceResponse(200, {"run_id": run_id, "files": list(files)})

    def manifest(self, run_id: str) -> ServiceResponse:
        """Read persisted manifest but remove internal ownership fields from wire.

        ``owner_id`` is stored only on disk in ``manifest.json`` and BuildIndex (#505).
        OpenAPI ``BuildManifest`` is SSOT for actual HTTP response, so remove it here
        explicitly to prevent becoming public API field.
        """
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # ADR 0016: Query manifest canonical through store (CUBRID rows first, FS fallback;
        # local=FS). get_manifest normalizes path safety, corruption, nonexistence to None.
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        manifest.pop("owner_id", None)
        return ServiceResponse(200, cast(dict[str, JsonValue], manifest))

    def spec(self, run_id: str) -> ServiceResponse:
        """Return canonical BuildSpec snapshot and digest actually used in run."""
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.is_dir():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
        ensure_within(run_dir, snapshot_path, label="BuildSpec snapshot")
        if not snapshot_path.is_file():
            return ServiceResponse(
                404, {"error": f"BuildSpec snapshot unavailable for run: {run_id}"}
            )
        try:
            payload = snapshot_path.read_bytes()
            spec_text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            # Returning exception string as-is exposes OSError with server absolute paths —
            # info callers don't need and shouldn't know. Diagnostic details logged as
            # full traceback; response includes only type name and safe message.
            logger.exception("failed to read BuildSpec snapshot for run %s", run_id)
            return ServiceResponse(500, {"error": "failed to read BuildSpec snapshot"})
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "spec": spec_text,
                "spec_digest": compute_spec_digest(payload),
            },
        )

    def serve_artifact_file(self, run_id: str, file_path: str) -> ServiceResponse | FileResponse:
        """Serve specific file from execution workspace (#323).

        Prevent path traversal by validating both run_id and file_path; don't follow
        symlinks.

        Args:
            run_id: Execution identifier.
            file_path: Requested file path (relative path under run_id).

        Returns:
            FileResponse (file found) or ServiceResponse (error).
        """
        # Validate run_id
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # Check run_dir
        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.exists():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        # Validate file_path (prevent traversal).
        #
        # Canonical artifact identifier is POSIX relative path from run directory
        # (e.g., "silver/datago.air_quality/table.parquet") returned by
        # `GET /artifacts/{run_id}`. HTTP route passes this path only via "/" between
        # segments; each segment can be percent-encoded (browsers convert non-ASCII/
        # special chars to %XX), so decode first. Must re-validate after decode —
        # encoded traversals like "%2e%2e"/"%2f"/"%5c" decode to components caught by
        # component checks; double-encoding "%252e" leaves "%" after decode, rejected
        # by component rules.
        decoded_path = unquote(file_path)
        segments = decoded_path.replace("\\", "/").split("/")
        if not decoded_path.strip() or any(seg in ("", ".", "..") for seg in segments):
            return ServiceResponse(
                400, {"error": f"file_path is not a safe relative path: {file_path!r}"}
            )
        try:
            for segment in segments:
                validate_path_segment(segment, field_name="file_path")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # Compute full path of requested file (only relative path passing component
        # validation combined).
        requested_file = run_dir.joinpath(*segments)
        # Verify path is within run_dir (symlink resolution for safety check).
        ensure_within(run_dir, requested_file, label="artifact file")

        if not requested_file.exists():
            return ServiceResponse(404, {"error": f"file not found: {file_path}"})
        if not requested_file.is_file():
            return ServiceResponse(400, {"error": f"not a file: {file_path}"})

        # Extract filename (for Content-Disposition).
        filename = requested_file.name

        return FileResponse(status_code=200, file_path=requested_file, filename=filename)

    def list_builds(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """Return execution history list sorted descending by latest completion time.

        Per ADR 0003, query SQLite index first; fall back to filesystem scan if index
        missing or empty. When ENFORCE_OWNERSHIP+oidc, both paths apply _apply_ownership
        to expose only own runs (#433).
        """
        # Query index first
        try:
            entries = self._build_index.list_builds(limit=limit)
            if entries:
                index_builds: list[_BuildListEntry] = [
                    {
                        "run_id": entry.run_id,
                        "status": entry.status,
                        "started_at": entry.started_at,
                        "finished_at": entry.finished_at,
                        "created_by": entry.created_by,
                        "owner_id": entry.owner_id,
                    }
                    for entry in entries
                ]
                filtered = _strip_internal_fields(_apply_ownership(index_builds, principal))
                return ServiceResponse(200, {"builds": cast(list[JsonValue], filtered)})
        except Exception:
            # Index query failed. When ENFORCE_OWNERSHIP+oidc, other users' runs could
            # leak via fallback, so return empty array fail-closed (#433). Normal mode
            # proceeds to filesystem fallback as before (ADR 0003).
            if ownership_module.enforce_ownership() and principal and principal.kind == "oidc":
                logger.warning(
                    "build index query failed; returning empty list "
                    "(ownership enforced, fail-closed)",
                    exc_info=True,
                )
                return ServiceResponse(200, {"builds": []})

        # Fallback: filesystem scan
        if not self._output_root.exists():
            return ServiceResponse(200, {"builds": []})

        candidates = heapq.nlargest(
            limit,
            (d for d in self._output_root.iterdir() if d.is_dir()),
            key=lambda p: p.stat().st_mtime,
        )
        fs_builds: list[_BuildListEntry] = []
        for run_dir in candidates:
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            fs_builds.append(
                {
                    "run_id": run_dir.name,
                    # manifest.json is SSOT so derive status rule in one place only
                    # (#481) — cancelled run may have empty errors, so presence check alone
                    # wrongly reports ok.
                    "status": status_from_manifest(manifest),
                    "started_at": manifest.get("started_at"),
                    "finished_at": manifest.get("finished_at"),
                    "created_by": manifest.get("created_by"),
                    "owner_id": manifest.get("owner_id"),
                }
            )
        filtered = _strip_internal_fields(_apply_ownership(fs_builds, principal))
        return ServiceResponse(200, {"builds": cast(list[JsonValue], filtered)})

    def get_build_events(self, run_id: str, *, limit: int, tail: bool) -> ServiceResponse:
        """Query run's append-only structured event timeline (#496).

        run_id validation, existence check, and ownership gating must complete before
        calling (dispatch route adapter handles first, same as ``/builds/{run_id}/stages``).
        Returns always chronological ascending — ``tail=True`` selects latest ``limit``
        items but doesn't reverse sort itself (#496 ordering policy).
        """
        events = self._event_store.list_for_run(run_id, limit=limit, tail=tail)
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "events": cast(JsonValue, [events_service.event_to_json(e) for e in events]),
        }
        return ServiceResponse(200, body)
