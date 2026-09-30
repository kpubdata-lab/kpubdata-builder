"""Build publish readiness/execution service logic (#491).

Contains pure logic that allows Studio to check readiness (ready/blockers/warnings)
of completed Gold builds before requesting actual publish, rather than publishing
immediately. Does not create new Publishers - HTTP reuses only the existing Hugging Face
publisher from ``publishers.PUBLISHER_REGISTRY``. Maintains Kaggle/Local registry and CLI
behavior but excludes them from HTTP targets (#28/#491).

Core principles:
    - readiness (GET) is side-effect-free: does not call Publisher or create remote
      datasets.
    - POST performs the exact same deterministic checks on the server - does not trust
      that caller ran GET first (TOCTOU prevention).
    - artifact list passed to Publisher always comes from intersection of
      ``manifest.outputs`` (source of truth: only files actually used by pipeline) and
      ``gold_source_dir`` (authoritative stage path helper, #488) - does not glob
      arbitrary directories.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime as datetime_module
from datetime import timezone
from pathlib import Path
from typing import Literal, cast

from ..errors import ValidationError
from ..publishers import PUBLISHER_REGISTRY
from ..spec import BuildSpec
from ..spec.validator import validate_spec
from ..stages._path_safety import ensure_within
from ..stages._stage_reader import gold_source_dir
from . import stages as stages_service
from .publish_credentials import PublishCredentialResolution
from .redistribution import (
    BuildVerdict,
    TermsLookup,
    build_verdict,
    is_public,
    kpubdata_terms,
    publish_issues,
)

# HTTP-safe publish targets. PUBLISHER_REGISTRY also has "local", but LocalPublisher
# uses caller-provided destination directly as a local filesystem Path (publishers/local.py)
# - HTTP exposure is only allowed as relative paths within publish-root specified by
# ``KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT`` (#550). Kaggle is only allowed when the ``id``
# in ``dataset-metadata.json`` recorded by packaging matches destination (#550
# reconciliation rule - same as existing CLI semantics).
HTTP_PUBLISH_TARGETS: tuple[str, ...] = ("huggingface", "kaggle", "local")

RunStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]

# Local publish-root absolute path configuration for server (#550). If not set,
# readiness of local target reports credential/publish boundary misconfiguration issue.
_LOCAL_PUBLISH_ROOT_ENV = "KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT"

_DESTINATION_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)

# Allow only kwargs that actual Publisher.publish() receives per target. Hugging Face allows
# only 'private' for new repo visibility, Kaggle allows only 'public' for new dataset
# public status, and rejects other options. Local has no exposed options.
_ALLOWED_OPTIONS: dict[str, dict[str, type]] = {
    # confirm_non_commercial (#688): the publisher's statement that a dataset whose
    # terms allow non-commercial use only is published for that use.
    "huggingface": {"private": bool, "confirm_non_commercial": bool},
    "kaggle": {"public": bool, "confirm_non_commercial": bool},
    "local": {"confirm_non_commercial": bool},
}

_DEFAULT_OPTIONS: dict[str, dict[str, object]] = {
    "huggingface": {"private": True},
    "kaggle": {"public": False},
    "local": {},
}

PublishReceiptState = Literal["pending", "succeeded", "unknown"]
PublishClaimStatus = Literal["claimed", "replay", "in_progress", "state_unknown", "conflict"]


@dataclass(frozen=True)
class PublishReceipt:
    """Durable publish operation receipt without credential/path."""

    fingerprint: str
    state: PublishReceiptState
    target: str
    destination: str
    options: dict[str, object]
    result: dict[str, object] | None


class PublishReceiptStore:
    """Receipt store that prevents duplicate remote side effects via SQLite UNIQUE claim.

    DB at ``output_root/_publish_receipts.sqlite``, internal service state outside
    run workspace. Artifact API lists only run directory, so not included in public
    artifacts or ``manifest.outputs``.
    """

    _FILENAME = "_publish_receipts.sqlite"

    def __init__(self, output_root: Path) -> None:
        self.path = output_root / self._FILENAME
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publish_receipts (
                        owner_key TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        target TEXT NOT NULL,
                        destination TEXT NOT NULL,
                        fingerprint TEXT NOT NULL UNIQUE,
                        options_json TEXT NOT NULL,
                        state TEXT NOT NULL
                            CHECK (state IN ('pending', 'succeeded', 'unknown')),
                        result_json TEXT,
                        PRIMARY KEY (owner_key, run_id, target, destination)
                    )
                    """
                )
                # reconcile/reset audit log (#551) - append only minimal fields without
                # credential/path/raw value.
                # Store owner_key/run_id directly in row so audit history is queryable
                # even if receipt is deleted by reset (#563).
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publish_receipt_audit (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        fingerprint TEXT NOT NULL,
                        owner_key TEXT,
                        run_id TEXT,
                        action TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    )
                    """
                )
                # Database-compatible migration created from #557 schema (no owner/run columns).
                for column in ("owner_key", "run_id"):
                    # If column already exists, ALTER fails with OperationalError.
                    with suppress(sqlite3.OperationalError):
                        connection.execute(
                            f"ALTER TABLE publish_receipt_audit ADD COLUMN {column} TEXT"
                        )
            self._initialized = True

    @staticmethod
    def fingerprint(
        *,
        owner_key: str,
        run_id: str,
        target: str,
        destination: str,
        options: dict[str, object],
    ) -> str:
        canonical = json.dumps(
            {
                "owner": owner_key,
                "run_id": run_id,
                "target": target,
                "destination": destination,
                "options": options,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    @staticmethod
    def _row_to_receipt(row: tuple[object, ...]) -> PublishReceipt:
        options = json.loads(cast(str, row[4]))
        result = json.loads(cast(str, row[6])) if row[6] is not None else None
        if not isinstance(options, dict) or (result is not None and not isinstance(result, dict)):
            raise ValueError("invalid publish receipt JSON")
        return PublishReceipt(
            fingerprint=cast(str, row[0]),
            state=cast(PublishReceiptState, row[5]),
            target=cast(str, row[2]),
            destination=cast(str, row[3]),
            options=cast(dict[str, object], options),
            result=cast(dict[str, object] | None, result),
        )

    def claim(
        self,
        *,
        owner_key: str,
        run_id: str,
        target: str,
        destination: str,
        options: dict[str, object],
    ) -> tuple[PublishClaimStatus, PublishReceipt]:
        """Preempt operation to durable pending or return existing state."""
        self._initialize()
        fingerprint = self.fingerprint(
            owner_key=owner_key,
            run_id=run_id,
            target=target,
            destination=destination,
            options=options,
        )
        options_json = json.dumps(
            options, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT fingerprint, owner_key, target, destination, options_json, state,
                       result_json
                FROM publish_receipts
                WHERE owner_key = ? AND run_id = ? AND target = ? AND destination = ?
                """,
                (owner_key, run_id, target, destination),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO publish_receipts(
                        owner_key, run_id, target, destination, fingerprint,
                        options_json, state, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)
                    """,
                    (owner_key, run_id, target, destination, fingerprint, options_json),
                )
                connection.commit()
                return (
                    "claimed",
                    PublishReceipt(
                        fingerprint=fingerprint,
                        state="pending",
                        target=target,
                        destination=destination,
                        options=dict(options),
                        result=None,
                    ),
                )

            receipt = self._row_to_receipt(cast(tuple[object, ...], row))
            connection.commit()
            if receipt.fingerprint != fingerprint:
                return "conflict", receipt
            if receipt.state == "succeeded":
                return "replay", receipt
            if receipt.state == "unknown":
                return "state_unknown", receipt
            return "in_progress", receipt
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def lookup(
        self,
        *,
        owner_key: str,
        run_id: str,
        target: str,
        destination: str,
        options: dict[str, object],
    ) -> tuple[PublishClaimStatus, PublishReceipt] | None:
        """Query existing operation receipt and replay decision side-effect-free."""
        self._initialize()
        fingerprint = self.fingerprint(
            owner_key=owner_key,
            run_id=run_id,
            target=target,
            destination=destination,
            options=options,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT fingerprint, owner_key, target, destination, options_json, state,
                       result_json
                FROM publish_receipts
                WHERE owner_key = ? AND run_id = ? AND target = ? AND destination = ?
                """,
                (owner_key, run_id, target, destination),
            ).fetchone()
        if row is None:
            return None
        receipt = self._row_to_receipt(cast(tuple[object, ...], row))
        if receipt.fingerprint != fingerprint:
            return "conflict", receipt
        if receipt.state == "succeeded":
            return "replay", receipt
        if receipt.state == "unknown":
            return "state_unknown", receipt
        return "in_progress", receipt

    def mark_succeeded(self, fingerprint: str, result: dict[str, object]) -> None:
        self._set_terminal(fingerprint, state="succeeded", result=result)

    def mark_unknown(self, fingerprint: str) -> None:
        self._set_terminal(fingerprint, state="unknown", result=None)

    def get_by_key(
        self,
        *,
        owner_key: str,
        run_id: str,
        target: str,
        destination: str,
    ) -> PublishReceipt | None:
        """Query receipt directly by operation key (#551 query API, side-effect-free)."""
        self._initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT fingerprint, owner_key, target, destination, options_json, state,
                       result_json
                FROM publish_receipts
                WHERE owner_key = ? AND run_id = ? AND target = ? AND destination = ?
                """,
                (owner_key, run_id, target, destination),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_receipt(cast(tuple[object, ...], row))

    def reconcile_succeeded(self, fingerprint: str, result: dict[str, object]) -> None:
        """Confirm unknown as succeeded via remote status check (#551). Record audit log."""
        self._initialize()
        self._set_terminal(
            fingerprint,
            state="succeeded",
            result=result,
            allowed_source_states=("pending", "unknown"),
        )
        owner_key, run_id = self._receipt_owner_run(fingerprint)
        self._append_audit(fingerprint, "reconcile_succeeded", owner_key=owner_key, run_id=run_id)

    def reset(self, fingerprint: str, *, action: str = "reset") -> bool:
        """Delete receipt to allow retry (new claim) (#551). Record audit log.

        Return True if delete actually happened, False if fingerprint not found.
        Audit row carries receipt owner/run (#563) so history remains queryable
        by owner-run even after receipt deletion.
        """
        self._initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_key, run_id FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            owner_key, run_id = cast(str | None, row[0]), cast(str, row[1])
            connection.execute(
                "DELETE FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            )
            self._append_audit_on(
                connection, fingerprint, action, owner_key=owner_key, run_id=run_id
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _receipt_owner_run(self, fingerprint: str) -> tuple[str | None, str | None]:
        """Read receipt (owner_key, run_id) from fingerprint (#563)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_key, run_id FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if row is None:
            return None, None
        return cast(str | None, row[0]), cast(str | None, row[1])

    def _append_audit(
        self,
        fingerprint: str,
        action: str,
        *,
        owner_key: str | None = None,
        run_id: str | None = None,
        actor: str = "operator",
    ) -> None:
        with self._connect() as connection:
            self._append_audit_on(
                connection, fingerprint, action, owner_key=owner_key, run_id=run_id, actor=actor
            )

    @staticmethod
    def _append_audit_on(
        connection: sqlite3.Connection,
        fingerprint: str,
        action: str,
        *,
        owner_key: str | None,
        run_id: str | None,
        actor: str = "operator",
    ) -> None:
        recorded_at = datetime_module.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO publish_receipt_audit(
                fingerprint, owner_key, run_id, action, actor, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fingerprint, owner_key, run_id, action, actor, recorded_at),
        )

    def audit_entries(self, *, owner_key: str, run_id: str) -> list[dict[str, str]]:
        """Return audit log for owner/run in time order.

        Includes rows after receipt deletion/reset (#563) - audit row carries owner/run
        itself, no JOIN needed.
        """
        self._initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT fingerprint, action, actor, recorded_at
                FROM publish_receipt_audit
                WHERE owner_key = ? AND run_id = ?
                ORDER BY seq
                """,
                (owner_key, run_id),
            ).fetchall()
        return [
            {
                "fingerprint": cast(str, row[0]),
                "action": cast(str, row[1]),
                "actor": cast(str, row[2]),
                "recorded_at": cast(str, row[3]),
            }
            for row in rows
        ]

    def _set_terminal(
        self,
        fingerprint: str,
        *,
        state: Literal["succeeded", "unknown"],
        result: dict[str, object] | None,
        allowed_source_states: tuple[str, ...] = ("pending",),
    ) -> None:
        self._initialize()
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if result is not None
            else None
        )
        # Hold write lock until transition to BEGIN IMMEDIATE ensures state
        # transitions are confirmed at serialization level like claim() (#564).
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if current is None or cast(str, current[0]) not in allowed_source_states:
                connection.commit()
                raise RuntimeError(
                    "publish receipt is not in an allowed state for this transition"
                    f" (allowed: {list(allowed_source_states)})"
                )
            connection.execute(
                """
                UPDATE publish_receipts
                SET state = ?, result_json = ?
                WHERE fingerprint = ?
                """,
                (state, result_json, fingerprint),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


@dataclass(frozen=True)
class PublishIssue:
    """Structured blocker/warning. Separate code+message so UI does not parse strings."""

    code: str
    message: str

    def to_body(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ResolvedArtifacts:
    """Canonical Gold artifact path to actually pass to target.

    ``paths`` always contains only files actually recorded in manifest.outputs (source
    of truth) and under this run gold_source_dir - BuildSpec snapshot, credential,
    temp files, silver/bronze files never mixed.
    """

    paths: tuple[Path, ...]
    expects_directory: bool


@dataclass(frozen=True)
class ReadinessResult:
    target: str
    ready: bool
    blockers: tuple[PublishIssue, ...]
    warnings: tuple[PublishIssue, ...]
    artifacts: ResolvedArtifacts | None = None
    #: The source terms' verdict on redistribution (#688); None before a spec exists.
    redistribution: BuildVerdict | None = None


def resolve_target(value: object) -> tuple[str | None, str | None]:
    """(target, error_message) - if target is None, error_message is filled."""
    if not isinstance(value, str) or not value:
        return None, "'target' must be a non-empty string"
    if value in HTTP_PUBLISH_TARGETS:
        return value, None
    if value in PUBLISHER_REGISTRY:
        return None, f"target {value!r} is not available over the publish HTTP API"
    return None, f"unknown publish target: {value!r}"


def run_status_blocker(status: RunStatus) -> PublishIssue | None:
    """Determine whether run status itself blocks publish. Use only existing status vocabulary."""
    if status in ("queued", "running", "cancelling"):
        return PublishIssue("run_not_terminal", f"run is not finished yet (status={status})")
    if status == "failed":
        return PublishIssue("run_failed", "run finished with errors and cannot be published")
    if status == "cancelled":
        return PublishIssue("run_cancelled", "run was cancelled and cannot be published")
    return None


def license_blocker(spec: BuildSpec | None) -> PublishIssue | None:
    """Reuse #443 license/redistribution gate - do not create new license policy.

    If BuildSpec.license not declared (including empty string), block publish.
    Do not auto-allow unknown license - declaration itself is sole redistribution
    basis (#443 principle as-is).

    If ``spec.license`` is whitespace-only string (spec loader type-checks only,
    passes through; spec/loader.py), treat same as no actual declaration, so not
    recognized as "declared" (#491 guideline 4) - do not add new SPDX allowlist/registry,
    only enhance blank determination.
    """
    if spec is None or not spec.license or not spec.license.strip():
        return PublishIssue(
            "license_missing",
            "BuildSpec.license must be declared before this dataset can be published (#443)",
        )
    return None


def effective_publish_policy_blockers(spec: BuildSpec | None) -> tuple[PublishIssue, ...]:
    """Re-validate stored spec with ``publish=True`` as if actual external publish.

    Do not replicate PII/license rules in service, use canonical ``validate_spec``
    structured problems. Whitespace-only license supplemented by stricter #491 gate
    ``license_blocker`` beyond validator truthiness check.
    """
    if spec is None:
        return ()
    try:
        validate_spec(replace(spec, publish=True))
    except ValidationError as exc:
        structured = exc.structured_problems or ()
        return tuple(
            PublishIssue(problem.code, problem.message)
            for problem in structured
            if problem.code != "missing_license_for_publish"
        )
    return ()


def _huggingface_credential_configured() -> bool:
    return bool(os.environ.get("HF_TOKEN"))


def _kaggle_credential_configured() -> bool:
    return bool(os.environ.get("KAGGLE_USERNAME")) and bool(os.environ.get("KAGGLE_KEY"))


def local_publish_root() -> Path | None:
    """Configured local publish-root absolute path (#550). None if not set/incomplete."""
    raw = os.environ.get(_LOCAL_PUBLISH_ROOT_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def resolve_local_destination(destination: str) -> tuple[Path, Path] | PublishIssue:
    """Interpret local target destination as absolute path within publish-root.

    Return: ``(publish_root, absolute_destination)`` or blocker. Destination must
    always be relative ``owner/name`` form, paths escaping root fail-closed (#550).
    """
    root = local_publish_root()
    if root is None:
        return PublishIssue(
            "local_publish_root_unconfigured",
            "KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT is not configured on the server",
        )
    absolute = (root / destination).resolve()
    try:
        ensure_within(root, absolute, label="local publish destination")
    except ValueError:
        return PublishIssue(
            "destination_outside_publish_root",
            "destination must stay inside the configured local publish root",
        )
    return root, absolute


def kaggle_package_id(artifacts: ResolvedArtifacts) -> tuple[Path, str] | PublishIssue | None:
    """Interpret Kaggle packaging (#550).

    Return:
        ``(package_dir, metadata_id)`` - when exactly one dataset-metadata.json exists
        and id readable (KagglePublisher receives directory artifact). ``None`` - when
        no packaging at all. :class:`PublishIssue` - when packaging multiple (ambiguous)
        or id not readable.
    """
    # dataset-metadata.json is exporter-recorded sidecar not in manifest.outputs -
    # find in sibling dir of interpreted gold artifact (#550).
    package_dirs: list[Path] = []
    for artifact_path in artifacts.paths:
        candidate = artifact_path.parent / "dataset-metadata.json"
        if candidate.is_file() and candidate.parent not in package_dirs:
            package_dirs.append(candidate.parent)
    if not package_dirs:
        return None
    if len(package_dirs) > 1:
        return PublishIssue(
            "kaggle_metadata_ambiguous",
            "run has multiple Kaggle packagings; publish target is ambiguous",
        )
    metadata_path = package_dirs[0] / "dataset-metadata.json"
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PublishIssue(
            "kaggle_metadata_unreadable",
            "Kaggle dataset-metadata.json could not be read",
        )
    if not isinstance(raw, dict):
        return PublishIssue(
            "kaggle_metadata_unreadable",
            "Kaggle dataset-metadata.json could not be read",
        )
    metadata_id = raw.get("id")
    if not isinstance(metadata_id, str) or not metadata_id:
        return PublishIssue(
            "kaggle_metadata_unreadable",
            "Kaggle dataset-metadata.json has no dataset id",
        )
    return metadata_path.parent, metadata_id


def credential_blocker(
    target: str, resolution: PublishCredentialResolution | None = None
) -> PublishIssue | None:
    """Check whether this publish has credential actually to use.

    Never read or return raw credential. If unable to verify if set (including
    unsupported credential shapes), do not assume ready=true, explicitly mark
    unavailable (#491 guideline 7). Local target's "credential" is publish-root
    config (#550).

    If ``resolution`` given, that is the answer - requester credential and server
    fallback policy (#635) already reflects result. Previously function only looked at
    server env vars. So even deployments with fallback closed via
    ``REQUIRE_OWN_PUBLISH_CREDENTIAL=true``, principals without stored credential got
    ready answer - only because server had token.
    """
    if target == "local":
        if local_publish_root() is not None:
            return None
        return PublishIssue(
            "local_publish_root_unconfigured",
            "no local publish root is configured for target 'local'",
        )

    if resolution is not None:
        if resolution.refused:
            return PublishIssue(
                "credential_required",
                f"target {target!r} requires a credential stored for this principal; "
                "this deployment does not lend out the server credential",
            )
        if resolution.values or resolution.not_required:
            return None
        return PublishIssue(
            "credential_unavailable",
            f"no credential is available for target {target!r}",
        )

    # Callers without resolution (CLI/tests) see only server environment as before.
    if target == "huggingface" and _huggingface_credential_configured():
        return None
    if target == "kaggle" and _kaggle_credential_configured():
        return None
    return PublishIssue(
        "credential_unavailable",
        f"no server-side credential is configured for target {target!r}",
    )


def resolve_gold_artifacts(
    output_root: Path, run_id: str, manifest: dict[str, object]
) -> ResolvedArtifacts | PublishIssue:
    """Interpret canonical Gold artifact files of this run.

    Among files actually recorded in ``manifest.outputs``, consider only those
    under known (non-failed) source ``gold_source_dir`` as candidates (#488 stage
    helper reuse) - do not recursive glob output_root.

    manifest.outputs records not just gold outputs but also this run bronze/silver
    originals, dataset card, BuildSpec snapshot and other stage legitimate outputs
    (see pipeline/orchestrator.py `_record_output_paths` call) - such items outside
    gold_source_dir is "normal" so silently exclude from candidates (gold files only).
    However if canonical manifest.outputs entry points outside this run own workspace
    (``{output_root}/{run_id}``) (gold root escape, symlink escape, invalid/unresolvable
    path included) - that cannot be legitimate other stage output, so fail-closed
    (#491 guideline 2). Even if valid gold artifact present, silently skip only that
    invalid entry and do not publish rest - entire canonical publish artifact set must
    be valid.
    """
    known = stages_service.known_source_keys(manifest)
    failed = stages_service.failed_source_keys(manifest)
    candidate_sources = [key for key in known if key not in failed]

    outputs_raw = manifest.get("outputs")
    output_paths = (
        [Path(p) for p in outputs_raw if isinstance(p, str)]
        if isinstance(outputs_raw, list)
        else []
    )

    run_dir = output_root / run_id

    gold_dirs: list[Path] = []
    for source_key in candidate_sources:
        try:
            gold_dir = gold_source_dir(output_root, run_id, source_key)
        except ValueError:
            continue
        if gold_dir.is_dir():
            gold_dirs.append(gold_dir)

    if not gold_dirs:
        return PublishIssue(
            "gold_unavailable", "no successful Gold output is available for this run"
        )

    # Classify all output paths against all actually-existing gold_dirs in this run
    # first (no nested per-source check) - so source B owned path not misclassified
    # as "invalid" when compared against source A gold_dir.
    gold_files: list[Path] = []
    for path in output_paths:
        matched = False
        for gold_dir in gold_dirs:
            try:
                ensure_within(gold_dir, path, label="gold artifact")
            except ValueError:
                continue
            matched = True
            break
        if matched:
            # Directory is also valid Gold artifact. ``kind: huggingface`` export creates
            # layout directory not single file, manifest output path also points to
            # directory - when only checking ``is_file()`` normally-finished builds
            # blocked as artifact_missing. HuggingFacePublisher already handles
            # directory as upload_folder.
            if not path.is_file() and not path.is_dir():
                return PublishIssue(
                    "artifact_missing",
                    f"expected Gold artifact is missing on disk: {path.name}",
                )
            gold_files.append(path)
            continue

        try:
            ensure_within(run_dir, path, label="run output")
        except ValueError:
            # Canonical output pointing outside this run own workspace
            # (path policy violation, symlink escape, unresolvable included) - cannot
            # be legitimate other stage output, so not silently skipped, immediately
            # fail-closed.
            return PublishIssue(
                "artifact_invalid",
                "a canonical manifest output failed the path-safety check and cannot be published",
            )
        # Inside run_dir but belongs to no gold_dir - legitimate non-gold
        # outputs like bronze/silver, dataset card, BuildSpec snapshot etc.
        # Not publish target (gold only) so silently exclude.

    if not gold_files:
        return PublishIssue(
            "gold_unavailable", "no successful Gold output is available for this run"
        )

    unique_sorted_files = sorted(set(gold_files))

    return ResolvedArtifacts(paths=tuple(unique_sorted_files), expects_directory=False)


def validate_destination(target: str, destination: object) -> str | None:
    """Allow only canonical 'owner/name' identifier form required by target.

    Never interpret as filesystem path - reject URL, scheme, absolute/relative
    paths, parent traversal (``..``), control chars, leading/trailing space
    (#491 guideline 6).
    """
    if not isinstance(destination, str):
        return "'destination' must be a string"
    if not destination:
        return "'destination' must not be empty"
    if destination != destination.strip():
        return "'destination' must not have leading/trailing whitespace"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in destination):
        return "'destination' must not contain control characters"
    if "://" in destination:
        return "'destination' must not be a URL"
    if destination.startswith(("/", "\\")):
        return "'destination' must not be an absolute path"
    if ".." in destination.replace("\\", "/").split("/"):
        return "'destination' must not contain path traversal segments"
    if not _DESTINATION_PATTERN.match(destination):
        return f"'destination' must look like 'owner/name' for target {target!r}"
    return None


def validate_options(target: str, options: object) -> tuple[str | None, dict[str, object]]:
    """(error_message, normalized_options). Do not silently ignore unsupported options."""
    if options is None:
        return None, dict(_DEFAULT_OPTIONS.get(target, {}))
    if not isinstance(options, dict):
        return "'options' must be an object", {}
    allowed = _ALLOWED_OPTIONS.get(target, {})
    for key, value in options.items():
        if not isinstance(key, str) or key not in allowed:
            return f"unsupported option for target {target!r}: {key!r}", {}
        expected_type = allowed[key]
        if expected_type is bool and not isinstance(value, bool):
            return f"option {key!r} must be a boolean", {}
    normalized = dict(_DEFAULT_OPTIONS.get(target, {}))
    normalized.update(options)
    return None, normalized


def build_readiness(
    *,
    run_id: str,
    target: str,
    destination: str,
    status: RunStatus,
    manifest: dict[str, object] | None,
    spec: BuildSpec | None,
    output_root: Path,
    credentials: PublishCredentialResolution | None = None,
    options: dict[str, object] | None = None,
    terms_lookup: TermsLookup = kpubdata_terms,
) -> ReadinessResult:
    """Single deterministic decision shared by readiness/POST.

    ``ready`` is always ``not blockers`` - no separate calculation path. Both GET
    and POST call only this function to reach same conclusion (TOCTOU re-verification,
    #491 guideline 3/4).
    """
    blockers: list[PublishIssue] = []
    redistribution: BuildVerdict | None = None

    status_issue = run_status_blocker(status)
    if status_issue is not None:
        blockers.append(status_issue)

    artifacts: ResolvedArtifacts | None = None
    if manifest is None:
        if status_issue is None:
            # Abnormal state: terminal (succeeded) but no manifest - fail-closed.
            blockers.append(PublishIssue("gold_unavailable", "run manifest is unavailable"))
    else:
        resolved = resolve_gold_artifacts(output_root, run_id, manifest)
        if isinstance(resolved, PublishIssue):
            blockers.append(resolved)
        else:
            artifacts = resolved

            if target == "kaggle":
                # Kaggle packaging's dataset-metadata.json id must match destination (#550).
                # KagglePublisher receives package directory, convert artifact bundle to
                # directory form.
                # packaging absence is destination-independent blocker, but id mismatch
                # check only
                # when destination is provided (GET readiness optional, POST final re-validates).
                package = kaggle_package_id(resolved)
                if isinstance(package, PublishIssue):
                    blockers.append(package)
                elif package is None:
                    blockers.append(
                        PublishIssue(
                            "kaggle_metadata_missing",
                            "run has no Kaggle packaging (dataset-metadata.json) to publish",
                        )
                    )
                else:
                    package_dir, metadata_id = package
                    if destination and metadata_id != destination:
                        blockers.append(
                            PublishIssue(
                                "kaggle_destination_mismatch",
                                f"packaged Kaggle dataset id {metadata_id!r} does not match"
                                f" destination {destination!r}",
                            )
                        )
                    else:
                        artifacts = ResolvedArtifacts(paths=(package_dir,), expects_directory=True)

            if target == "local" and destination:
                resolved_local = resolve_local_destination(destination)
                if isinstance(resolved_local, PublishIssue):
                    blockers.append(resolved_local)

        blockers.extend(effective_publish_policy_blockers(spec))

        license_issue = license_blocker(spec)
        if license_issue is not None:
            blockers.append(license_issue)

        # The source terms (#688): forbidden never publishes, unknown never publishes
        # publicly, non-commercial needs confirming (and a marker when public).
        redistribution = build_verdict(spec, terms_lookup)
        effective = options if options is not None else dict(_DEFAULT_OPTIONS.get(target, {}))
        blockers.extend(
            PublishIssue(issue.code, issue.message)
            for issue in publish_issues(
                redistribution,
                public=is_public(target, effective),
                confirmed_non_commercial=effective.get("confirm_non_commercial") is True,
                spec=spec,
            )
        )

    credential_issue = credential_blocker(target, credentials)
    if credential_issue is not None:
        blockers.append(credential_issue)

    return ReadinessResult(
        target=target,
        ready=not blockers,
        blockers=tuple(blockers),
        warnings=(),
        artifacts=artifacts,
        redistribution=redistribution,
    )


__all__ = [
    "HTTP_PUBLISH_TARGETS",
    "PublishClaimStatus",
    "PublishIssue",
    "PublishReceipt",
    "PublishReceiptStore",
    "ReadinessResult",
    "ResolvedArtifacts",
    "RunStatus",
    "build_readiness",
    "credential_blocker",
    "effective_publish_policy_blockers",
    "license_blocker",
    "resolve_gold_artifacts",
    "resolve_target",
    "run_status_blocker",
    "validate_destination",
    "validate_options",
]
