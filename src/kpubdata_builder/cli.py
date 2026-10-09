"""Command-line entry point for kpubdata-builder.

This module configures an argparse-based CLI and provides entry points for
validate/preview/build/publish/serve commands.

Key functions:
    - build_parser: ArgumentParser with subcommands
    - dispatch: Route parsed command to actual execution function
    - main: Top-level entry point for CLI process
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from . import __version__, logging_redaction
from .errors import PublishError, SpecLoadError, ValidationError
from .pipeline import preview_build, run_build
from .publishers import PUBLISHER_REGISTRY
from .replay import BUNDLED_FIXTURES, REPLAY_DIR_ENV, enable_replay, export_fixtures
from .service.redistribution import (
    TermsLookup,
    build_verdict,
    is_public,
    kpubdata_terms,
    needs_private_destination,
    publish_issues,
    visibility_issue,
)
from .spec import load_spec
from .spec.validator import validate_spec
from .stages.bronze.build import SourceClient
from .tabular import DEFAULT_PREVIEW_LIMIT
from .warehouse import (
    CATALOG_FILENAME,
    HOLD_KINDS,
    BackupInvalid,
    HoldKind,
    SnapshotStateError,
    TableCatalog,
    WarehouseError,
)
from .warehouse import backup as warehouse_backup
from .warehouse import gc as warehouse_gc


def build_parser() -> argparse.ArgumentParser:
    """Create CLI-exclusive ArgumentParser.

    Registers validate, preview, build subcommands and also exposes common --version option.

    Returns:
        argparse.ArgumentParser: Configured parser object.

    Example:
        >>> parser = build_parser()
        >>> parser.prog
        'kpubdata-builder'
    """
    parser = argparse.ArgumentParser(
        prog="kpubdata-builder",
        description="KPubData Builder command-line interface.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")

    validate_cmd = subparsers.add_parser(
        "validate",
        help="Validate a BuildSpec YAML file.",
    )
    validate_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")

    preview_cmd = subparsers.add_parser(
        "preview",
        help="Preview a BuildSpec: schema and sample rows without writing artifacts.",
    )
    preview_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")
    preview_cmd.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PREVIEW_LIMIT,
        help=f"Maximum sample rows per source (default: {DEFAULT_PREVIEW_LIMIT}).",
    )

    build_cmd = subparsers.add_parser(
        "build",
        help="Execute a BuildSpec through the Medallion pipeline.",
    )
    build_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")
    build_cmd.add_argument(
        "--output-dir",
        default="build",
        help="Run workspace root directory (default: build).",
    )
    build_cmd.add_argument(
        "--run-id",
        default=None,
        help="Run identifier (default: generated timestamp).",
    )
    build_cmd.add_argument(
        "--warehouse",
        default=None,
        metavar="DIR",
        help=(
            "Commit each source's Gold output as a table snapshot under DIR, so the "
            "build ends at a queryable table. Needs no publish credential. Omitted, "
            "nothing is written to a catalog."
        ),
    )
    build_cmd.add_argument(
        "--workspace-id",
        default="ws_personal",
        help="Owning workspace for materialised tables (default: ws_personal).",
    )
    build_cmd.add_argument(
        "--warehouse-keep",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Keep the N most recent snapshots of each table this build commits and "
            "reclaim the rest (default: 3). Use -1 to keep every snapshot. Ignored "
            "without --warehouse."
        ),
    )

    publish_cmd = subparsers.add_parser(
        "publish",
        help="Publish build artifacts to a local or remote destination.",
    )
    publish_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")
    publish_cmd.add_argument(
        "--target",
        choices=sorted(PUBLISHER_REGISTRY.keys()),
        default="local",
        help="Publish target (default: local).",
    )
    publish_cmd.add_argument(
        "--destination",
        required=True,
        help="Local directory path (local) or HF repo id (huggingface).",
    )
    publish_cmd.add_argument(
        "--artifacts-dir",
        required=True,
        help="Directory whose files will be published.",
    )
    publish_cmd.add_argument(
        "--public",
        action="store_true",
        help="Create new datasets as public (kaggle only; default: private).",
    )
    publish_cmd.add_argument(
        "--confirm-non-commercial",
        action="store_true",
        help=(
            "Confirm that data whose terms allow non-commercial use only is published "
            "for that use (#688)."
        ),
    )

    serve_cmd = subparsers.add_parser(
        "serve",
        help="Run the Builder HTTP service.",
    )
    serve_cmd.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1).",
    )
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port (default: 8000). 0 lets the operating system choose; the port is "
        "then printed as 'listening on http://<host>:<port>'.",
    )
    serve_cmd.add_argument(
        "--output-dir",
        default="build",
        help="Run workspace root directory (default: build).",
    )
    serve_cmd.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Max concurrent request threads (default: 10, or KPUBDATA_BUILDER_MAX_WORKERS).",
    )
    serve_cmd.add_argument(
        "--max-builds",
        type=int,
        default=None,
        help=(
            "Max builds running at once, async jobs and synchronous POST /build together "
            "(default: KPUBDATA_BUILDER_MAX_BUILDS, else the request thread count)."
        ),
    )
    serve_cmd.add_argument(
        "--max-previews",
        type=int,
        default=None,
        help=(
            "Max previews running at once; further ones wait "
            "(default: KPUBDATA_BUILDER_MAX_PREVIEWS, else no limit)."
        ),
    )
    serve_cmd.add_argument(
        "--warehouse",
        default=None,
        metavar="DIR",
        help=(
            "Table catalog root. POST /build then commits each source's Gold output as "
            "a table snapshot and reports it under `materialized`. Needs no publish "
            "credential. Default: KPUBDATA_BUILDER_WAREHOUSE, or no catalog."
        ),
    )
    replay_group = serve_cmd.add_mutually_exclusive_group()
    replay_group.add_argument(
        "--replay",
        action="store_true",
        help=(
            "Serve provider responses from the fixtures bundled with this package "
            "instead of the live API, for client end-to-end tests (#837)."
        ),
    )
    replay_group.add_argument(
        "--replay-dir",
        default=None,
        metavar="DIR",
        help=(
            "Like --replay, from a fixture directory (see `fixtures export`). "
            "Default: KPUBDATA_BUILDER_REPLAY_DIR, or no replay."
        ),
    )

    fixtures_cmd = subparsers.add_parser(
        "fixtures",
        help="Export the replay fixtures bundled with this package (#837).",
    )
    fixtures_actions = fixtures_cmd.add_subparsers(dest="fixtures_action", required=True)
    fixtures_export = fixtures_actions.add_parser(
        "export", help="Copy the bundled fixtures into DIR; existing files are never overwritten."
    )
    fixtures_export.add_argument("destination", metavar="DIR")

    rebuild_cmd = subparsers.add_parser(
        "rebuild-index",
        help="Rebuild the build index from filesystem scans.",
    )
    rebuild_cmd.add_argument(
        "--output-dir",
        default="build",
        help="Run workspace root directory (default: build).",
    )

    # -- Agent pipeline commands --

    discover_cmd = subparsers.add_parser(
        "discover",
        help="Discover API metadata from a data.go.kr URL.",
    )
    discover_cmd.add_argument(
        "url",
        help="data.go.kr API detail page URL.",
    )
    discover_cmd.add_argument(
        "--dataset-id",
        default=None,
        help="Override the generated dataset ID (e.g. datago.ocean_buoy).",
    )
    discover_cmd.add_argument(
        "--output",
        default=None,
        help="Write generated spec YAML to this file path.",
    )

    monitor_cmd = subparsers.add_parser(
        "monitor",
        help="Check pending dataset applications for approval.",
    )
    monitor_cmd.add_argument(
        "--state-file",
        default=".kpubdata-monitor.yaml",
        help="Path to monitor state file (default: .kpubdata-monitor.yaml).",
    )
    monitor_cmd.add_argument(
        "--add",
        default=None,
        help="Add a dataset ID to the pending list.",
    )
    monitor_cmd.add_argument(
        "--check",
        action="store_true",
        help="Check all pending datasets for approval status.",
    )

    pipeline_cmd = subparsers.add_parser(
        "pipeline",
        help="Run automated onboarding pipeline for a dataset.",
    )
    pipeline_cmd.add_argument(
        "dataset",
        help="Dataset ID to process (e.g. datago.ocean_buoy).",
    )
    pipeline_cmd.add_argument(
        "--kpubdata-root",
        default=None,
        help="Path to kpubdata repository root (default: auto-detect).",
    )
    pipeline_cmd.add_argument(
        "--skip-pr",
        action="store_true",
        help="Stop after verification, do not create a PR.",
    )

    verify_cmd = subparsers.add_parser(
        "verify",
        help="Verify dataset specs against live APIs.",
    )
    verify_cmd.add_argument(
        "dataset",
        nargs="?",
        default=None,
        help="Dataset ID to verify (e.g. datago.apt_trade). Omit for --all.",
    )
    verify_cmd.add_argument(
        "--all",
        action="store_true",
        dest="verify_all",
        help="Verify all discovered spec datasets.",
    )
    verify_cmd.add_argument(
        "--output",
        default=None,
        help="Write machine-readable YAML results to this file.",
    )
    verify_cmd.add_argument(
        "--page-size",
        type=_positive_int,
        default=10,
        help="Number of records to request per test (default: 10).",
    )
    verify_cmd.add_argument(
        "--hashes",
        default=None,
        help="Path to previous schema hashes YAML for drift detection.",
    )

    prune_cmd = subparsers.add_parser(
        "prune-cancelled",
        help="List (and optionally delete) cancelled partial-run artifacts past a TTL (#549).",
    )
    prune_cmd.add_argument(
        "--output-dir",
        default="build",
        help="Run workspace root directory (default: build).",
    )
    prune_cmd.add_argument(
        "--ttl-hours",
        type=float,
        default=None,
        help=(
            "Retention window in hours for cancelled partial runs. "
            "Defaults to KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS; "
            "when unset, nothing is ever a deletion candidate."
        ),
    )
    prune_cmd.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete matching run workspaces. Without this flag the command is a dry run.",
    )

    gc_cmd = subparsers.add_parser(
        "warehouse-gc",
        help="Reclaim snapshots and staging directories nothing needs any more (#738).",
    )
    gc_cmd.add_argument(
        "warehouse",
        metavar="DIR",
        help="Table catalog root, the same directory `build --warehouse` was given.",
    )
    gc_cmd.add_argument(
        "--keep",
        type=int,
        default=3,
        metavar="N",
        help="Committed snapshots to retain per table, newest first (default: 3).",
    )
    gc_cmd.add_argument(
        "--stale-hours",
        type=float,
        default=24.0,
        metavar="H",
        help=(
            "Treat an uncommitted snapshot older than H hours as a crashed build and "
            "mark it reclaimable (default: 24). The catalog cannot tell a crashed "
            "build from a slow one, so this is a statement about how long a build of "
            "yours may take, not a fact the catalog knows."
        ),
    )
    gc_cmd.add_argument(
        "--workspace-id",
        default=None,
        help="Only collect tables of this workspace (default: every workspace).",
    )

    backup_cmd = subparsers.add_parser(
        "warehouse-backup",
        help="Back up the table catalog and its snapshot files together (#705).",
    )
    backup_cmd.add_argument("warehouse", metavar="DIR", help="Table catalog root.")
    backup_cmd.add_argument(
        "destination",
        metavar="DEST",
        help="Backup directory. Must not exist or be empty; nothing is overwritten.",
    )

    restore_cmd = subparsers.add_parser(
        "warehouse-restore",
        help=(
            "Restore a warehouse backup into an empty directory, after checking the "
            "catalog and the snapshot files against each other (#705)."
        ),
    )
    restore_cmd.add_argument("backup", metavar="BACKUP", help="A warehouse-backup directory.")
    restore_cmd.add_argument(
        "warehouse",
        metavar="DIR",
        help="Where to restore. Must not exist or be empty; nothing is overwritten.",
    )

    hold_cmd = subparsers.add_parser(
        "warehouse-hold",
        help=(
            "Place, release or list holds that keep a snapshot past garbage collection "
            "(#705, #797)."
        ),
    )
    hold_cmd.add_argument("warehouse", metavar="DIR", help="Table catalog root.")
    hold_actions = hold_cmd.add_subparsers(dest="hold_action", required=True)
    hold_place = hold_actions.add_parser("place", help="Hold a committed snapshot.")
    hold_place.add_argument("snapshot_id", metavar="SNAPSHOT")
    hold_place.add_argument("--kind", required=True, choices=HOLD_KINDS)
    hold_place.add_argument(
        "--reason",
        required=True,
        help="What the hold is for, so whoever finds it later knows whether to release it.",
    )
    hold_place.add_argument(
        "--expires-at",
        default=None,
        metavar="ISO8601",
        help="When the hold lapses, with a UTC offset (default: until released).",
    )
    hold_release = hold_actions.add_parser("release", help="Release a hold.")
    hold_release.add_argument("hold_id", metavar="HOLD")
    hold_list = hold_actions.add_parser("list", help="List a snapshot's live holds.")
    hold_list.add_argument("snapshot_id", metavar="SNAPSHOT")

    return parser


#: Why a client that must not use the operator's keys cannot be built (#990).
ENV_KEYS_UNSUPPORTED = (
    "the installed kpubdata cannot keep the environment's provider keys out of a client "
    "(Client has no env_keys option; it came with 0.9.0), so a request without its "
    "own key would run on the operator's. Install a kpubdata release that has it, or "
    "turn KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL and multi-user mode off."
)


def client_keeps_environment_keys_out() -> bool:
    """Whether the installed kpubdata ``Client`` takes ``env_keys`` (#990).

    kpubdata 0.8.0 has no such option and accepts any keyword without an error, so
    passing ``env_keys=False`` to it does nothing and says nothing. Asking the signature
    is the only way to know the option is honoured. Every release Builder's pin allows
    has it; the check stays for an environment that installed something older anyway.
    """
    import inspect

    from kpubdata import Client

    return "env_keys" in inspect.signature(Client).parameters


def _create_client(
    *,
    provider_keys: dict[str, str] | None = None,
    timeout: float | None = None,
    cache: bool | None = None,
    environment_keys: bool = True,
) -> SourceClient:
    """Create kpubdata client with configuration.

    Separated as standalone function for monkeypatch replacement in tests. Actual
    network calls happen during build execution (run_build).
    """
    from kpubdata import Client

    if not environment_keys:
        # The service asked for a client that must not carry the operator's keys
        # (REQUIRE_OWN_PROVIDER_CREDENTIAL, #786). Leaving them out of provider_keys
        # is not enough: a kpubdata client looks a missing key up in the environment
        # when it is used, so the client has to be told not to (#990).
        if not client_keeps_environment_keys_out():
            raise RuntimeError(ENV_KEYS_UNSUPPORTED)
        return cast(
            SourceClient,
            Client(
                provider_keys=dict(provider_keys or {}),
                timeout=timeout if timeout is not None else 30.0,
                cache=bool(cache),
                env_keys=False,
            ),
        )
    # Since kpubdata #276, from_env accepts only explicit parameters (**kwargs removed).
    return cast(
        SourceClient,
        Client.from_env(
            provider_keys=provider_keys,
            timeout=timeout,
            cache=cache,
        ),
    )


def _run_validate(spec_path: str) -> int:
    """Load and validate specified BuildSpec file.

    Args:
        spec_path: YAML file path string to check.

    Returns:
        int: 0 on success, 1 on load/validation failure.

    Raises:
        Does not propagate exceptions directly; converts to error message and exit code.
    """
    try:
        spec = load_spec(Path(spec_path))
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"spec is valid: {spec.dataset_id}")
    return 0


def _run_build(
    spec_path: str,
    *,
    output_dir: str,
    run_id: str | None,
    warehouse: str | None = None,
    workspace_id: str = "ws_personal",
    warehouse_keep: int = 3,
) -> int:
    """Load and validate BuildSpec, then execute Medallion pipeline.

    Args:
        spec_path: BuildSpec YAML path to build.
        output_dir: Execution workspace root.
        run_id: Execution identifier. If None, generated from timestamp.
        warehouse: Table catalog root. When given, each successful source's Gold
            output is committed as a table snapshot and the run ends at a queryable
            table (#703). Omitted, nothing is written to a catalog — and the output
            says so, because "nothing was committed" and "committing was not asked
            for" are different outcomes.
        workspace_id: Owning workspace for materialised tables.
        warehouse_keep: Snapshots to keep per table once this build commits a new
            one; a negative number keeps every snapshot. Committing without
            reclaiming grows the warehouse by a whole copy of Gold per refresh
            (#738).

    Returns:
        int: 0 if all sources succeed, 1 on load/validation/build failure.
    """
    try:
        spec = load_spec(Path(spec_path))
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    client = _create_client()
    catalog = TableCatalog(Path(warehouse)) if warehouse else None
    result = run_build(
        spec,
        client=client,
        output_root=Path(output_dir),
        run_id=run_id,
        catalog=catalog,
        workspace_id=workspace_id,
        warehouse_keep=None if warehouse_keep < 0 else warehouse_keep,
    )

    print(f"build: {spec.dataset_id} (run {result.context.run_id})")
    for outcome in result.outcomes:
        stages = ", ".join(outcome.stages_completed) or "-"
        print(f"  - {outcome.source_key}: {outcome.status} [{stages}]")
    print(f"manifest: {result.manifest_path}")
    if catalog is not None:
        for committed in sorted(result.materialized.values(), key=lambda c: c.table.logical_name):
            print(
                f"  table {committed.table.logical_name}: "
                f"snapshot {committed.snapshot.id} (revision {committed.table.revision})"
            )
        if not result.materialized:
            print("  no table committed")

    if result.status != "ok":
        print("error: build failed for one or more sources", file=sys.stderr)
        for outcome in result.outcomes:
            if outcome.status == "failed":
                print(f"  - {outcome.source_key}: {outcome.error}", file=sys.stderr)
        return 1
    return 0


def _run_preview(spec_path: str, *, limit: int) -> int:
    """Load and validate BuildSpec, then print only schema and sample for each source.

    Does not create actual artifact files.

    Args:
        spec_path: Path to BuildSpec YAML file.
        limit: Max sample rows per source.

    Returns:
        int: 0 on success, 1 on load/validation failure.
    """
    try:
        spec = load_spec(Path(spec_path))
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    try:
        client = _create_client()
        result = preview_build(spec, client=client, limit=limit)
    except ValueError as exc:
        # User input error like limit < 1.
        print(f"error: invalid preview input: {exc}", file=sys.stderr)
        return 1

    print(f"preview: {spec.dataset_id}")
    failed_sources: list[str] = []
    for source in result.previews:
        if source.status != "ok":
            failed_sources.append(source.source_key)
            continue
        columns = ", ".join(f"{c.name} ({c.dtype})" for c in source.schema.columns)
        print(f"  - {source.source_key}: {columns}")
        print(f"    sample ({len(source.preview.rows)} of {source.preview.total_rows} rows):")
        for row in source.preview.rows:
            print(f"      {row}")

    if failed_sources:
        # Source fetch failure → stderr + exit 1 — prevents CI/automation misinterpretation.
        print("error: preview failed for one or more sources", file=sys.stderr)
        for source in result.previews:
            if source.status != "ok":
                print(f"  - {source.source_key}: {source.error}", file=sys.stderr)
        return 1
    return 0


#: Run workspace artifacts not for publication. When artifacts_dir passed as run root,
#: ``rglob("*")`` sweeps up bronze original and BuildSpec snapshot too —
#: we only want gold.
_NON_PUBLISHABLE_DIRS = frozenset({"bronze", "silver"})
_NON_PUBLISHABLE_FILES = frozenset({"manifest.json", "buildspec.yaml"})


def _is_non_publishable(path: Path, root: Path) -> bool:
    """Whether this file is a run workspace artifact and not a publication target."""
    relative = path.relative_to(root)
    if relative.parts and relative.parts[0] in _NON_PUBLISHABLE_DIRS:
        return True
    return relative.name in _NON_PUBLISHABLE_FILES


def _run_publish(
    spec_path: str,
    *,
    target: str,
    destination: str,
    artifacts_dir: str,
    public: bool = False,
    confirm_non_commercial: bool = False,
    terms_lookup: TermsLookup = kpubdata_terms,
) -> int:
    """Load and validate BuildSpec, then publish artifacts to specified target.

    Args:
        spec_path: BuildSpec YAML path baseline for publishing.
        target: Publication target identifier (PUBLISHER_REGISTRY key).
        destination: Local directory path or remote repo id.
        artifacts_dir: Directory containing files to publish.
        public: Whether to make new Kaggle dataset public (ignored for other targets).
        confirm_non_commercial: The publisher's confirmation for non-commercial terms.
        terms_lookup: Each dataset's redistribution terms (#688).

    Returns:
        int: 0 on success, 1 on load/validation/publish failure, 2 when the source
        terms do not allow this publish.
    """
    try:
        spec = load_spec(Path(spec_path))
        # Validate with publish=True. If only validate_spec(spec) called, publication-only
        # rules (license declaration etc.) not applied, so specs blocked by HTTP publish
        # could be uploaded via CLI — same policy varied by path.
        validate_spec(replace(spec, publish=True))
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    # The same terms gate as the HTTP publish (#688): the CLI is no side door.
    options: dict[str, object] = {"public": public} if target == "kaggle" else {}
    verdict = build_verdict(spec, terms_lookup)
    issues = publish_issues(
        verdict,
        public=is_public(target, options),
        confirmed_non_commercial=confirm_non_commercial,
        spec=spec,
    )
    if not issues and needs_private_destination(
        verdict, public=is_public(target, options), spec=spec
    ):
        # A private-only publish to a destination that is already public would be
        # public: publishing never changes an existing destination's visibility.
        try:
            visibility: str | None = PUBLISHER_REGISTRY[target].destination_visibility(destination)
        except Exception:
            visibility = None
        issue = visibility_issue(visibility)
        if issue is not None:
            issues.append(issue)
    if issues:
        print("error: the source terms do not allow this publish:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue.code}: {issue.message}", file=sys.stderr)
        return 2

    artifacts_path = Path(artifacts_dir)
    if not artifacts_path.is_dir():
        print(f"error: no artifacts found in {artifacts_dir}", file=sys.stderr)
        return 1

    publisher = PUBLISHER_REGISTRY[target]

    # Layout-based (Kaggle) passes directory itself, file-based (local/HF) passes individual files.
    # Resolves per-publisher input contract mismatch (#176).
    paths: tuple[Path, ...]
    if publisher.expects_directory:
        paths = (artifacts_path,)
    else:
        candidates = sorted(p for p in artifacts_path.rglob("*") if p.is_file())
        paths = tuple(p for p in candidates if not _is_non_publishable(p, artifacts_path))
        skipped = [p for p in candidates if p not in paths]
        if skipped:
            # Do not silently exclude — publisher must know what goes up.
            print(f"note: skipping {len(skipped)} non-dataset file(s):", file=sys.stderr)
            for path in skipped[:10]:
                print(f"  - {path.relative_to(artifacts_path)}", file=sys.stderr)
            if len(skipped) > 10:
                print(f"  ... and {len(skipped) - 10} more", file=sys.stderr)
        if not paths:
            print(f"error: no artifacts found in {artifacts_dir}", file=sys.stderr)
            return 1

    publish_kwargs: dict[str, object] = {"destination": destination}
    if target == "kaggle":
        publish_kwargs["public"] = public

    try:
        result = publisher.publish(paths, **publish_kwargs)  # type: ignore[arg-type]
    except (PublishError, RuntimeError) as exc:
        print(f"error: publish failed: {exc}", file=sys.stderr)
        return 1

    print(f"publish: {spec.dataset_id} -> {target}")
    print(f"  target: {result.reference}")
    print(f"  artifacts: {result.artifact_count}")
    return 0


def _run_serve(
    *,
    output_dir: str,
    host: str,
    port: int,
    max_workers: int | None,
    max_builds: int | None = None,
    max_previews: int | None = None,
    warehouse: str | None = None,
    replay: bool = False,
    replay_dir: str | None = None,
) -> int:
    """Run BuilderService as HTTP server (#249).

    Args:
        output_dir: Execution workspace root.
        host: Binding host.
        port: Binding port.
        max_workers: Max concurrent request threads. If None, use KPUBDATA_BUILDER_MAX_WORKERS env,
            else default (10) (#374).
        max_builds: Max builds running at once, on either path (#1028). If None, use
            KPUBDATA_BUILDER_MAX_BUILDS env, else ``max_workers``.
        max_previews: Max previews running at once (#1028). If None, use
            KPUBDATA_BUILDER_MAX_PREVIEWS env, else no limit.
        warehouse: Table catalog root (#703). If None, use KPUBDATA_BUILDER_WAREHOUSE env,
            else no catalog — builds then end at Gold and commit no snapshot.
        replay: Serve provider responses from the bundled fixtures (#837).
        replay_dir: Serve them from this directory. If None and ``replay`` is off, use
            KPUBDATA_BUILDER_REPLAY_DIR env, else no replay.

    Returns:
        int: Exit code. 0 on graceful shutdown via Ctrl-C/SIGTERM, 1 when a setting
        cannot be used as written (#1108), when a state store was written by a newer
        release (#1096), when the replay fixtures cannot be used, or
        when the deployment requires each request's own provider key and the installed
        kpubdata cannot keep the operator's out (#990).
    """
    from .service import BuilderService
    from .service.app import DEFAULT_BUILD_WAIT_SECONDS
    from .service.http import _DEFAULT_MAX_WORKERS, serve

    # Every setting is read once before anything is built from them (#1108): what
    # cannot be used stops the start here, all of it in one message.
    from .service.startup_settings import check_settings

    # A flag takes the place of its variable, whose value is then never read. ``--port``
    # always has one: the variable is the entrypoint's, which passes it as the flag.
    overridden = {"KPUBDATA_BUILDER_PORT"}
    if max_workers is not None:
        overridden.add("KPUBDATA_BUILDER_MAX_WORKERS")
    if max_builds is not None:
        overridden.add("KPUBDATA_BUILDER_MAX_BUILDS")
    if max_previews is not None:
        overridden.add("KPUBDATA_BUILDER_MAX_PREVIEWS")
    report = check_settings(overridden=overridden)
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if report.problems:
        for problem in report.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    # Priority: --max-workers flag > KPUBDATA_BUILDER_MAX_WORKERS env > default.
    if max_workers is None:
        env_workers = _count_from_env("KPUBDATA_BUILDER_MAX_WORKERS")
        max_workers = env_workers if env_workers is not None else _DEFAULT_MAX_WORKERS
    if max_workers < 1:
        raise SystemExit(f"max_workers must be >= 1, got {max_workers}")

    # How many builds run at once is its own setting (#1028). One value used to size
    # both the request threads and the async build workers, so lowering it to bound
    # builds starved every other request, and synchronous builds on the request
    # threads ran on top of the async ones. Priority: --max-builds flag >
    # KPUBDATA_BUILDER_MAX_BUILDS env > the request thread count (what a deployment that
    # only set MAX_WORKERS had as its async worker count).
    if max_builds is None:
        env_builds = _count_from_env("KPUBDATA_BUILDER_MAX_BUILDS")
        max_builds = env_builds if env_builds is not None else max_workers
    if max_builds < 1:
        raise SystemExit(f"max_builds must be >= 1, got {max_builds}")
    # Priority: --max-previews flag > KPUBDATA_BUILDER_MAX_PREVIEWS env > no limit.
    if max_previews is None:
        max_previews = _count_from_env("KPUBDATA_BUILDER_MAX_PREVIEWS")
    if max_previews is not None and max_previews < 1:
        raise SystemExit(f"max_previews must be >= 1, got {max_previews}")
    # How long a synchronous build waits for a slot before 429 build_queue_full (#1040).
    # KPUBDATA_BUILDER_BUILD_WAIT_SECONDS; 0 turns a build away at once when none is free.
    env_wait = os.environ.get("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "").strip()
    try:
        build_wait_seconds = float(env_wait) if env_wait else DEFAULT_BUILD_WAIT_SECONDS
    except ValueError:
        raise SystemExit(
            f"KPUBDATA_BUILDER_BUILD_WAIT_SECONDS must be a number, got {env_wait!r}"
        ) from None
    # ``nan < 0`` is false, so a plain sign check let ``nan`` and ``inf`` through; with
    # every slot taken one never returned and the other raised OverflowError (#1068).
    if not math.isfinite(build_wait_seconds) or build_wait_seconds < 0:
        raise SystemExit(
            f"KPUBDATA_BUILDER_BUILD_WAIT_SECONDS must be a finite number >= 0, got {env_wait!r}"
        )

    # Priority: --warehouse flag > KPUBDATA_BUILDER_WAREHOUSE env > none. Without this
    # the HTTP service could not reach the materialise-only end state at all: the
    # service accepted a catalog root, but nothing that starts it passed one (#703).
    if warehouse is None:
        warehouse = os.environ.get("KPUBDATA_BUILDER_WAREHOUSE") or None

    # Priority: --replay / --replay-dir > KPUBDATA_BUILDER_REPLAY_DIR env > live API.
    if not replay and replay_dir is None:
        replay_dir = os.environ.get(REPLAY_DIR_ENV) or None
    replay_root = BUNDLED_FIXTURES if replay else Path(replay_dir) if replay_dir else None
    if replay_root is not None:
        try:
            placeholders = enable_replay(replay_root)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            f"replay mode: provider responses come from {replay_root}; requests it has "
            "no recording for go to the live API"
            + (f" (placeholder key for: {', '.join(placeholders)})" if placeholders else ""),
            flush=True,
        )

    # A deployment that promises not to use the operator's keys must be able to keep
    # that promise before it takes a request, not find out on the first build (#990).
    from .service.providers import require_own_provider_credential

    if require_own_provider_credential() and not client_keeps_environment_keys_out():
        print(f"error: {ENV_KEYS_UNSUPPORTED}", file=sys.stderr)
        return 1

    from .store import bring_index_up_to_date
    from .store.schema_version import UnsupportedSchemaVersionError, says_unreachable

    try:
        # Before the service opens the index: an older one would be emptied there, and
        # the server would answer as healthy with every earlier run missing (#1096).
        indexed = bring_index_up_to_date(Path(output_dir))
        if indexed:
            print(f"rebuilt the build index from the manifests: {indexed} run(s)", flush=True)
        service = BuilderService(
            output_root=Path(output_dir),
            client_factory=_create_client,
            async_max_workers=max_builds,
            max_concurrent_builds=max_builds,
            max_concurrent_previews=max_previews,
            build_wait_seconds=build_wait_seconds,
            warehouse_root=Path(warehouse) if warehouse is not None else None,
        )
    except (UnsupportedSchemaVersionError, SnapshotStateError) as exc:
        # A state store a newer release wrote: said in one line, and left as it is
        # (#1096). This is what a rolled-back deployment meets. The catalog says it
        # with its own error, as it did before it was opened at start.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        # A state store that could not be reached to begin with — locked by another
        # process, on a disk that cannot be written. Said in one line as the refusals
        # above are; the store is as it was (#1157). Any other database error is not
        # this — a migration that failed, a statement this release got wrong — and
        # keeps its traceback: "could not be opened" would be a wrong answer to it.
        if not says_unreachable(exc):
            raise
        print(f"error: a state store could not be opened: {exc}", file=sys.stderr)
        return 1
    # Long-running command, so flush immediately to avoid startup logs lost in pipe buffering.
    print(
        f"serving kpubdata-builder on http://{host}:{port} "
        f"(output: {output_dir}, max_workers: {max_workers}, max_builds: {max_builds}, "
        f"max_previews: {max_previews if max_previews is not None else 'unlimited'}, "
        f"warehouse: {warehouse or 'none'})",
        flush=True,
    )
    try:
        serve(service, host=host, port=port, max_workers=max_workers)
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    return 0


def _count_from_env(name: str) -> int | None:
    """The variable as an integer, or None when it is unset or empty.

    Raises:
        SystemExit: It is not an integer. ``int()`` on its own ended the start with a
            traceback that did not name the variable (#1108).
    """
    # Stripped, as the start-up check reads it: a value of nothing but spaces is not
    # set, here as there.
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


def _run_fixtures_export(*, destination: str) -> int:
    """Copy the bundled replay fixtures into ``destination`` (#837)."""
    try:
        copied = export_fixtures(Path(destination))
    except FileExistsError as exc:
        print(f"error: {exc}; nothing was copied", file=sys.stderr)
        return 1
    print(f"exported {len(copied)} file(s) to {destination}")
    return 0


def _run_rebuild_index(output_dir: str) -> int:
    """Rebuild build index by filesystem scan (#309, ADR 0003).

    Args:
        output_dir: Build output root directory.

    Returns:
        int: 0 on success, 1 on failure.
    """
    from .store import rebuild_index

    output_root = Path(output_dir)
    print(f"rebuilding build index from {output_root}...", flush=True)

    try:
        count = rebuild_index(output_root)
        print(f"rebuilt index with {count} build(s)", flush=True)
        return 0
    except Exception as exc:
        print(f"error: failed to rebuild index: {exc}", file=sys.stderr)
        return 1


def _run_warehouse_backup(*, warehouse: str, destination: str) -> int:
    """Back up a warehouse; exit 1 with every problem when the copy would not be whole."""
    root = Path(warehouse)
    if not (root / CATALOG_FILENAME).is_file():
        print(f"error: no table catalog under {root}", file=sys.stderr)
        return 1
    try:
        report = warehouse_backup.backup(TableCatalog(root), Path(destination))
    except BackupInvalid as exc:
        print("error: backup refused:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"backed up {report.tables} table(s), {len(report.snapshots)} snapshot(s) "
        f"to {report.path}"
        + (f"; left out {len(report.left_out)} unreadable snapshot(s)" if report.left_out else "")
    )
    return 0


def _run_warehouse_restore(*, backup: str, warehouse: str) -> int:
    """Restore a backup; exit 1 with every problem rather than restore part of it."""
    try:
        catalog = warehouse_backup.restore(Path(backup), Path(warehouse))
    except BackupInvalid as exc:
        print("error: restore refused — nothing was restored:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    tables = catalog.list_tables()
    print(f"restored {len(tables)} table(s) into {warehouse}")
    return 0


def _run_warehouse_hold(
    *,
    warehouse: str,
    action: str,
    snapshot_id: str | None = None,
    hold_id: str | None = None,
    kind: str | None = None,
    reason: str | None = None,
    expires_at: str | None = None,
) -> int:
    """Place, release or list snapshot holds (#797).

    Holds existed only as a Python API, so keeping a snapshot for an audit or a saved
    analysis meant writing code against the catalog.
    """
    root = Path(warehouse)
    if not (root / CATALOG_FILENAME).is_file():
        print(f"error: no table catalog under {root}", file=sys.stderr)
        return 1
    catalog = TableCatalog(root)
    try:
        if action == "place":
            hold = catalog.place_hold(
                snapshot_id or "",
                kind=cast(HoldKind, kind),
                reason=reason or "",
                expires_at=expires_at,
            )
            print(hold.hold_id)
        elif action == "release":
            catalog.release_hold(hold_id or "")
            print(f"released {hold_id}")
        else:
            for live in catalog.live_holds(snapshot_id or ""):
                print(
                    f"{live.hold_id}\t{live.kind}\t{live.expires_at or 'until released'}"
                    f"\t{live.reason}"
                )
    except WarehouseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        catalog.close()
    return 0


def _run_warehouse_gc(
    *,
    warehouse: str,
    keep: int,
    stale_hours: float,
    workspace_id: str | None,
) -> int:
    """Reclaim what the warehouse no longer needs, across every table.

    A build reclaims the table it just committed, which is the common case. Two kinds
    of garbage survive that, and this command is what reaches them:

    * a table nobody has rebuilt since its snapshots went stale — the build never runs,
      so the build-time pass never runs either;
    * a crashed build's ``staging`` row, which is age-based and so cannot be judged
      from inside the build that would have finished it.

    The second is the one that hides: the catalog knows about that directory, so
    ``collect_orphan_staging`` deliberately leaves it alone, and without a cut-off
    nothing ever marks it.

    Args:
        warehouse: Table catalog root.
        keep: Committed snapshots to retain per table, newest first.
        stale_hours: How old an uncommitted snapshot must be to count as abandoned.
        workspace_id: Restrict to one workspace, or every workspace when None.

    Returns:
        int: 0 always, unless the warehouse directory does not exist. Reclaiming
        nothing is a normal outcome, not a failure.
    """
    root = Path(warehouse)
    if not root.is_dir():
        print(f"error: no such warehouse directory: {root}", file=sys.stderr)
        return 1

    before = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()
    catalog = TableCatalog(root)
    tables = catalog.list_tables(workspace_id)
    if not tables:
        print(f"warehouse {root}: no tables")
        return 0

    total_removed = 0
    for table in sorted(tables, key=lambda t: t.logical_name):
        marked = warehouse_gc.abandon_stale(catalog, table.id, before=before)
        report = warehouse_gc.collect(catalog, table.id, keep=keep)
        total_removed += report.removed_count
        # Say what was kept and why. A caller who cannot tell "nothing to do" from
        # "everything was in use" cannot diagnose a warehouse that stops reclaiming.
        details = [f"removed {report.removed_count}"]
        if marked:
            details.append(f"marked {len(marked)} stale")
        if report.kept_leased:
            details.append(f"kept {len(report.kept_leased)} leased")
        if report.kept_held:
            details.append(f"kept {len(report.kept_held)} held")
        if report.kept_current:
            details.append(f"kept {len(report.kept_current)} current")
        print(f"  {table.logical_name}: " + ", ".join(details))

    unit = "y" if total_removed == 1 else "ies"
    print(f"warehouse {root}: reclaimed {total_removed} director{unit}")
    return 0


def _run_prune_cancelled(*, output_dir: str, ttl_hours: float | None, apply: bool) -> int:
    """List/clean up cancelled+partial run artifacts past TTL (#549).

    Default is dry-run; ``--apply`` is needed for deletion. TTL argument takes
    precedence; if absent, use ``KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS`` env var,
    if that too absent, disabled (no targets) — misconfiguration never loses evidence.
    """
    import os

    from .retention import CANCELLED_RUN_TTL_ENV, prune_cancelled_runs

    output_root = Path(output_dir)
    effective_ttl = ttl_hours
    if effective_ttl is None:
        raw_env = os.environ.get(CANCELLED_RUN_TTL_ENV, "").strip()
        if raw_env:
            try:
                effective_ttl = float(raw_env)
            except ValueError:
                print(
                    f"error: {CANCELLED_RUN_TTL_ENV}={raw_env!r} is not a number of hours",
                    file=sys.stderr,
                )
                return 1

    mode = "APPLY (deleting)" if apply else "dry run (nothing will be deleted)"
    print(f"pruning cancelled partial runs under {output_root} — {mode}", flush=True)

    report = prune_cancelled_runs(output_root, ttl_hours=effective_ttl, apply=apply)

    for candidate in report.kept:
        print(f"kept: {candidate.run_id}", flush=True)
    for run_id in report.deleted:
        print(f"deleted: {run_id}", flush=True)
    print(
        f"scanned {report.scanned} cancelled partial run(s), deleted {report.deleted_count}",
        flush=True,
    )
    return 0


def _run_discover(url: str, *, dataset_id: str | None, output: str | None) -> int:
    """Discover API metadata from a data.go.kr URL.

    Exit 3 means the portal was unreachable from this origin — an
    environment problem with the next step in the message, not a
    discovery bug (exit 1). Argparse already owns exit 2 for usage
    errors, and a pipeline must not retry those.
    """
    from .agent.discover import PortalUnreachable, discover_from_url

    try:
        result = discover_from_url(url)
    except PortalUnreachable as exc:
        print(f"error: discovery unreachable: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"error: discovery failed: {exc}", file=sys.stderr)
        return 1

    if dataset_id:
        result.dataset_id = dataset_id

    print(f"Discovered: {result.title}")
    print(f"  Endpoint: {result.base_url}/{result.operation}")
    print(f"  Params:   {[p.name for p in result.params]}")
    print()

    spec_yaml = result.to_spec_yaml()

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(spec_yaml)
        print(f"Spec written to {out_path}")
    else:
        print(spec_yaml)

    return 0


def _run_monitor(*, state_file: str, add: str | None, check: bool) -> int:
    """Manage and check pending dataset applications."""
    from .agent.monitor import MonitorState, check_approval

    state_path = Path(state_file)
    state = MonitorState.load(state_path)

    if add:
        state.add(add)
        state.save(state_path)
        print(f"Added {add} to pending list")
        return 0

    if check:
        if not state.pending:
            print("No pending datasets")
            return 0

        print(f"Checking {len(state.pending)} pending dataset(s)...\n")
        changed = False
        for p in list(state.pending):
            status = check_approval(p.dataset_id)
            old_status = p.status
            p.status = status
            p.last_checked = (
                __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )

            icon = "APPROVED" if status == "HEALTHY" else status
            print(f"  {p.dataset_id:40s} {old_status} -> {icon}")
            if status != old_status:
                changed = True

        if changed:
            state.save(state_path)
            print(f"\nState updated: {state_path}")
        return 0

    # Default: list pending
    if not state.pending:
        print("No pending datasets")
    else:
        print(f"Pending datasets ({len(state.pending)}):\n")
        for p in state.pending:
            print(f"  {p.dataset_id:40s} {p.status}")
    return 0


def _run_pipeline(
    dataset: str,
    *,
    kpubdata_root: str | None,
    skip_pr: bool,
) -> int:
    """Run the automated onboarding pipeline for a dataset."""
    from .agent.pipeline import run_pipeline

    root = Path(kpubdata_root) if kpubdata_root else _find_kpubdata_root()
    if root is None:
        print("error: cannot find kpubdata root. Use --kpubdata-root.", file=sys.stderr)
        return 1

    print(f"Running pipeline for {dataset} (root: {root})\n")
    result = run_pipeline(dataset, kpubdata_root=root, skip_pr=skip_pr)

    print(f"  Step reached: {result.step_reached}")
    print(f"  Success:      {result.success}")
    if result.detail:
        print(f"  Detail:       {result.detail}")
    if result.pr_url:
        print(f"  PR:           {result.pr_url}")

    return 0 if result.success else 1


def _find_kpubdata_root() -> Path | None:
    """Try to find the kpubdata repo root relative to this package."""
    # Common layout: kpubdata-builder and kpubdata are siblings
    builder_root = Path(__file__).resolve().parents[2]
    candidate = builder_root.parent / "kpubdata"
    if (candidate / "src" / "kpubdata").is_dir():
        return candidate
    return None


def _positive_int(value: str) -> int:
    """argparse type for options that must be a positive count."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {parsed}")
    return parsed


def _load_previous_hashes(hashes_file: Path) -> dict[str, str]:
    """Read a schema-hash baseline, accepting both file shapes.

    ``--output`` writes ``{"results": [...], "hashes": {...}}``, so feeding that
    file straight back to ``--hashes`` used to stringify the two top-level values
    and silently skip drift detection in the workflow the CLI advertises. Read
    the nested ``hashes`` mapping when it is there, and keep supporting a flat
    ``{dataset_id: hash}`` file.
    """
    import yaml

    with open(hashes_file, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        return {}
    nested = raw.get("hashes")
    source = nested if isinstance(nested, dict) else raw
    return {str(key): str(value) for key, value in source.items() if isinstance(value, str)}


def _run_verify(
    *,
    dataset: str | None,
    verify_all: bool,
    output: str | None,
    page_size: int,
    hashes_path: str | None,
) -> int:
    """Verify dataset spec(s) against live APIs.

    Parameters:
        dataset: Single dataset ID to verify. None requires --all.
        verify_all: If True, verify all discovered specs.
        output: Optional path to write YAML results.
        page_size: Records per test request.
        hashes_path: Optional path to previous schema hashes YAML.
    """
    from kpubdata import discover_specs, find_spec

    from .verify import runner as _verify_runner

    # Load previous schema hashes if provided
    previous_hashes: dict[str, str] = {}
    if hashes_path:
        hashes_file = Path(hashes_path)
        if not hashes_file.is_file():
            # Silently proceeding with an empty baseline turns a typo or a
            # missing CI artifact into "schema drift detection is off", while
            # the command still reports HEALTHY.
            print(f"error: hashes file not found: {hashes_file}", file=sys.stderr)
            return 1
        previous_hashes = _load_previous_hashes(hashes_file)

    if dataset:
        spec = find_spec(dataset)
        if spec is None:
            print(f"error: spec not found: {dataset}", file=sys.stderr)
            return 1
        result = _verify_runner.verify_dataset(
            spec,
            previous_hash=previous_hashes.get(dataset),
            page_size=page_size,
        )
        print(result.format_report())
        results = [result]
    elif verify_all:
        specs = discover_specs()
        if not specs:
            print("error: no specs discovered", file=sys.stderr)
            return 1
        results = _verify_runner.verify_datasets(
            specs,
            previous_hashes=previous_hashes,
            page_size=page_size,
        )
        # Print summary
        healthy = sum(1 for r in results if r.passed)
        print(f"\nVerification Summary: {healthy}/{len(results)} healthy\n")
        for r in results:
            icon = "pass" if r.passed else "FAIL"
            print(f"  [{icon}] {r.dataset_id:40s} {r.status.value}")
        print()
    else:
        print("error: specify a dataset ID or use --all", file=sys.stderr)
        return 2

    # Write machine-readable output
    if output:
        import yaml

        out_path = Path(output)
        data = {
            "results": [r.to_dict() for r in results],
            "hashes": {r.dataset_id: r.schema_hash for r in results if r.schema_hash},
        }
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        print(f"Results written to {out_path}")

    failed = any(not r.passed for r in results)
    return 1 if failed else 0


def dispatch(args: argparse.Namespace) -> int:
    """Pass parsed argparse result to actual command execution function.

    Args:
        args: Namespace generated by argparse.

    Returns:
        int: CLI exit code.

    Example:
        >>> parser = build_parser()
        >>> dispatch(parser.parse_args(["preview"]))
        1
    """
    command = args.command
    if command == "validate":
        return _run_validate(args.spec)
    if command == "preview":
        return _run_preview(args.spec, limit=args.limit)
    if command == "build":
        return _run_build(
            args.spec,
            output_dir=args.output_dir,
            run_id=args.run_id,
            warehouse=args.warehouse,
            workspace_id=args.workspace_id,
            warehouse_keep=args.warehouse_keep,
        )
    if command == "publish":
        return _run_publish(
            args.spec,
            target=args.target,
            destination=args.destination,
            artifacts_dir=args.artifacts_dir,
            public=args.public,
            confirm_non_commercial=args.confirm_non_commercial,
        )
    if command == "serve":
        return _run_serve(
            output_dir=args.output_dir,
            host=args.host,
            port=args.port,
            max_workers=args.max_workers,
            max_builds=args.max_builds,
            max_previews=args.max_previews,
            warehouse=args.warehouse,
            replay=args.replay,
            replay_dir=args.replay_dir,
        )
    if command == "fixtures":
        return _run_fixtures_export(destination=args.destination)
    if command == "verify":
        return _run_verify(
            dataset=args.dataset,
            verify_all=args.verify_all,
            output=args.output,
            page_size=args.page_size,
            hashes_path=args.hashes,
        )
    if command == "rebuild-index":
        return _run_rebuild_index(output_dir=args.output_dir)
    if command == "warehouse-gc":
        return _run_warehouse_gc(
            warehouse=args.warehouse,
            keep=args.keep,
            stale_hours=args.stale_hours,
            workspace_id=args.workspace_id,
        )
    if command == "warehouse-backup":
        return _run_warehouse_backup(warehouse=args.warehouse, destination=args.destination)
    if command == "warehouse-restore":
        return _run_warehouse_restore(backup=args.backup, warehouse=args.warehouse)
    if command == "warehouse-hold":
        return _run_warehouse_hold(
            warehouse=args.warehouse,
            action=args.hold_action,
            snapshot_id=getattr(args, "snapshot_id", None),
            hold_id=getattr(args, "hold_id", None),
            kind=getattr(args, "kind", None),
            reason=getattr(args, "reason", None),
            expires_at=getattr(args, "expires_at", None),
        )
    if command == "prune-cancelled":
        return _run_prune_cancelled(
            output_dir=args.output_dir,
            ttl_hours=args.ttl_hours,
            apply=args.apply,
        )
    if command == "discover":
        return _run_discover(args.url, dataset_id=args.dataset_id, output=args.output)
    if command == "monitor":
        return _run_monitor(
            state_file=args.state_file,
            add=args.add,
            check=args.check,
        )
    if command == "pipeline":
        return _run_pipeline(
            args.dataset,
            kpubdata_root=args.kpubdata_root,
            skip_pr=args.skip_pr,
        )
    # Cannot reach via normal CLI path (argparse rejects unknown subcommands),
    # but kept as defensive fallback for programmatic callers.
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Act as the topmost entry point of CLI process.

    Args:
        argv: Argument list for tests or programmatic calls. Uses sys.argv if None.

    Returns:
        int: Exit code to pass to OS.

    Raises:
        Converts SystemExit raised by argparse to exit code internally.

    Example:
        >>> main(["--version"]) in {0, 2}
        True
    """
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 2
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    # Provider keys ride in request URLs, and the HTTP library logs those URLs (#686).
    logging_redaction.install()
    return dispatch(args)


__all__ = ["build_parser", "dispatch", "main"]
