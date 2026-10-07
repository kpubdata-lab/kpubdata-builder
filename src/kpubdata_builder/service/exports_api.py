"""Export a pinned query's result through a policy-checked path (#819).

Build artifacts have exporters, and publishing has a licence gate, but what a user
queried had no way out except the client turning rows into a CSV itself. That skips
everything: the licence, the PII policy, which snapshot the rows came from, and whether
the rows were all of them. This module is the one way out for a query result:

- **Pinned.** ``current`` is resolved to a snapshot id once, under a lease, like every
  warehouse read. The export records the SQL, the snapshot id, the table revision and
  the snapshot's artifact digest, so what it holds can be read again and compared.
- **Policy first.** A source whose licence forbids derivatives (KOGL type 3 or 4, a
  ``-nd`` Creative Commons licence) is refused before the query runs: an export of a
  user's query is a derived work, and Builder cannot prove any particular query is not.
  After the query, text values that match the build's PII patterns are refused unless
  the source's PII policy is ``allow`` or lists the column in ``allow_columns``.
- **All or nothing.** Over ``max_rows`` or the byte limit, nothing is kept and the
  answer is 422 — never a file cut short that looks complete.
- **Terms travel with the file.** The download is a zip of the data file,
  ``manifest.json`` (snapshot, provenance, collection coverage, licence terms verbatim,
  ``user_derived``, every altered value counted) and ``NOTICE.md``. Licence terms are
  copied as the build spec declared them; they are never restated as another licence.
- **Checked at download.** The caller must own the export (another owner's is absent,
  a 404, like their tables), it must not have expired (410), and the file must still be
  the one that was written (409). Exports expire after ``EXPORT_RETENTION`` and a
  workspace holds at most ``MAX_LIVE_EXPORTS``.

An export runs synchronously within ``EXPORT_TIMEOUT_SECONDS``. Queued exports with
cancellation are a later step; the status vocabulary already names them.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from kpubdata_builder.query.engine import (
    QueryExecutionError,
    QueryResourceLimitError,
    QueryTimeoutError,
)
from kpubdata_builder.query.export import (
    DEFAULT_EXPORT_MAX_ROWS,
    MAX_EXPORT_BYTES,
    MAX_EXPORT_ROWS,
    ExportFormat,
    ExportPlan,
    ExportProfile,
    sha256_file,
)
from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql
from kpubdata_builder.query.service import QueryBusyError, QueryService
from kpubdata_builder.service import ownership
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.column_semantics import (
    describe_json_columns,
    spec_semantics,
    table_key,
)
from kpubdata_builder.service.datasets import read_manifest, read_snapshot_spec
from kpubdata_builder.service.redistribution import (
    TermsLookup,
    build_verdict,
    forbidden_response,
    kpubdata_terms,
)
from kpubdata_builder.service.responses import FileResponse, ServiceResponse
from kpubdata_builder.service.warehouse_api import _coverage, _pin, _readable_table
from kpubdata_builder.spec import BuildSpec, JsonValue
from kpubdata_builder.stages.bronze.resolve import source_identity
from kpubdata_builder.warehouse import (
    SnapshotNotFound,
    SnapshotStateError,
    TableCatalog,
    TableNotFound,
    TableRow,
)

EXPORT_RETENTION = timedelta(hours=24)
MAX_LIVE_EXPORTS = 20
MANIFEST_VERSION = 1
_FIELDS = {"table", "snapshot", "sql", "format", "profile", "max_rows"}
_ID = re.compile(r"^exp_[0-9a-f]{32}$")
_BUNDLE = "export.zip"
_RECORD = "export.json"
#: Licence words that forbid derivatives: KOGL types 3 and 4 ("no modification"), and
#: Creative Commons "ND". Matched on the licence identifier and name as declared.
_NO_DERIVATIVES = re.compile(
    r"kogl[-_ ]?(type)?[-_ ]?[34]\b|제\s*[34]\s*유형|변경\s*금지"
    r"|(^|[-_ ])nd([-_ ]|$)|no[-_ ]?deriv",
    re.IGNORECASE,
)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SourceTerms:
    """The source's terms as its build spec declared them, verbatim."""

    dataset_id: str | None
    license: str | None
    license_name: str | None
    license_link: str | None
    attribution: str | None

    @property
    def status(self) -> str:
        if self.dataset_id is None:
            return "unknown"  # the run's spec could not be read
        return "declared" if self.license and self.license.strip() else "undeclared"

    @property
    def forbids_derivatives(self) -> bool:
        return any(
            value and _NO_DERIVATIVES.search(value) for value in (self.license, self.license_name)
        )

    def body(self) -> dict[str, JsonValue]:
        return {
            "status": self.status,
            "license": self.license,
            "license_name": self.license_name,
            "license_link": self.license_link,
            "attribution": self.attribution,
        }


def source_terms(spec: BuildSpec | None) -> SourceTerms:
    if spec is None:
        return SourceTerms(None, None, None, None, None)
    return SourceTerms(
        spec.dataset_id, spec.license, spec.license_name, spec.license_link, spec.attribution
    )


def pii_blockers(spec: BuildSpec | None, findings: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """Findings the source's PII policy does not let out of Builder.

    ``allow`` lets everything out, and a column in ``allow_columns`` was accepted by
    whoever wrote the spec. Every other finding — under ``block``, under ``warn``, and
    under no policy at all — blocks the export: ``warn`` let the rows into the
    warehouse, which is not the same as letting them leave it.
    """
    policy = spec.pii if spec is not None else None
    if policy is not None and policy.mode == "allow":
        return []
    allowed = set(policy.allow_columns) if policy is not None else set()
    return [cast(JsonValue, f) for f in findings if f.get("column") not in allowed]


def _provenance(
    spec: BuildSpec | None, manifest: Mapping[str, object] | None, logical_name: str
) -> list[JsonValue]:
    """The run's provenance entries for the source this table was built from.

    A table built from one source gets that source's entries; a composed table, or one
    whose source cannot be told, gets every entry of the run.
    """
    if manifest is None:
        return []
    raw = manifest.get("provenance")
    entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    if spec is not None and logical_name.startswith(f"{spec.dataset_id}."):
        key = logical_name[len(spec.dataset_id) + 1 :]
        for source in spec.sources:
            identity = source_identity(source)
            if (source.alias or ".".join(identity)) == key:
                entries = [e for e in entries if (e.get("provider"), e.get("dataset")) == identity]
                break
    kept = (
        "provider",
        "dataset",
        "fetched_at",
        "api_version",
        "params",
        "fetched_row_count",
        "source_reported_total",
        "coverage",
    )
    return [cast(JsonValue, {k: e[k] for k in kept if k in e}) for e in entries]


def _notice(manifest: Mapping[str, JsonValue]) -> str:
    source = cast(dict[str, JsonValue], manifest["source"])
    terms = cast(dict[str, JsonValue], source["terms"])
    snapshot = cast(dict[str, JsonValue], manifest["snapshot"])
    output = cast(dict[str, JsonValue], manifest["output"])
    altered = cast(list[dict[str, JsonValue]], output["values_altered"])
    lines = [
        "# KPubData Builder export",
        "",
        f"- Table: `{snapshot['logical_name']}`, snapshot `{snapshot['snapshot_id']}`"
        f" (revision {snapshot['revision']}, build run `{snapshot['run_id']}`)",
        f"- Rows: {output['row_count']} — the complete query result",
        "- This file is the result of a user's query over the source data: a derived work,"
        " not the source itself. 이 파일은 사용자 질의의 결과로, 원천 자료 자체가 아닌 파생물이다.",
        "",
        "## Terms of the source / 원천 이용 조건",
        "",
    ]
    if terms["status"] == "declared":
        lines.append(f"- License: {terms['license']}")
        for key, label in (
            ("license_name", "License name"),
            ("license_link", "Terms"),
            ("attribution", "Attribution"),
        ):
            if terms.get(key):
                lines.append(f"- {label}: {terms[key]}")
        lines.append(
            "- Copied as the build spec declared them. They are not restated as any other"
            " licence. 빌드 명세에 적힌 그대로이며 다른 라이선스로 다시 표기하지 않는다."
        )
    else:
        lines.append(
            "- No licence was declared for this source. Check the provider's terms before"
            " reusing it. 이용 조건이 선언되지 않았다 — 재사용 전에 제공기관의 조건을 확인하라."
        )
    lines += ["", "## Format", ""]
    if output["profile"] == "spreadsheet":
        lines += [
            "- Spreadsheet profile: UTF-8 with a BOM. The BOM only tells a spreadsheet the"
            " encoding; it does not stop it from dropping leading zeros, rounding integers"
            " beyond 15 digits or turning text into dates. Import columns as text to keep"
            " them. BOM 은 인코딩 표시일 뿐, 앞자리 0·큰 정수·날짜 변환을 막지 않는다.",
        ]
        if altered:
            lines.append(
                "- Text cells starting with = + - @, a tab or a CR were prefixed with an"
                " apostrophe so a spreadsheet does not run them as formulas. Those values"
                " were changed: "
                + ", ".join(f"`{a['column']}` ({a['count']})" for a in altered)
                + ". 수식으로 실행되지 않도록 작은따옴표를 붙인 값이다."
            )
    else:
        lines.append(
            "- Machine profile: UTF-8 without a BOM. Every value is written exactly as"
            " Builder sends it: codes keep leading zeros, decimals and large integers are"
            " exact text, dates are ISO 8601. No value was altered."
        )
    lines += ["", "`manifest.json` has the provenance, coverage and column types.", ""]
    return "\n".join(lines)


class ExportStore:
    """Export records and bundles on disk, one directory per export."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def directory(self, export_id: str) -> Path | None:
        if not _ID.match(export_id):
            return None
        return self._root / export_id

    def create(self) -> tuple[str, Path]:
        export_id = f"exp_{secrets.token_hex(16)}"
        directory = self._root / export_id
        directory.mkdir(parents=True)
        return export_id, directory

    def save(self, record: Mapping[str, JsonValue]) -> None:
        directory = self._root / str(record["export_id"])
        tmp = directory / f"{_RECORD}.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(directory / _RECORD)

    def load(self, export_id: str) -> dict[str, JsonValue] | None:
        directory = self.directory(export_id)
        if directory is None:
            return None
        path = directory / _RECORD
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cast(dict[str, JsonValue], record) if isinstance(record, dict) else None

    def records(self) -> list[dict[str, JsonValue]]:
        if not self._root.is_dir():
            return []
        found = (self.load(entry.name) for entry in sorted(self._root.iterdir()))
        return [record for record in found if record is not None]

    def remove(self, export_id: str) -> None:
        directory = self.directory(export_id)
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


def _not_found(export_id: str) -> ServiceResponse:
    return ServiceResponse(
        404, {"error": f"no such export: {export_id}", "code": "export_not_found"}
    )


class ExportsApiService:
    """Create, read, download and delete the caller's query exports."""

    def __init__(
        self,
        *,
        output_root: Path,
        table_catalog: Callable[[], TableCatalog | None],
        engine: QueryService,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        terms_lookup: TermsLookup = kpubdata_terms,
    ) -> None:
        self._output_root = output_root
        self._terms_lookup = terms_lookup
        self._store = ExportStore(output_root / ".service" / "exports")
        self._table_catalog = table_catalog
        self._engine = engine
        self._now = now

    # ------------------------------------------------------------------ helpers

    def _expired(self, record: Mapping[str, JsonValue]) -> bool:
        return self._now() >= _parse_utc(str(record["expires_at"]))

    def _body(self, record: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        expired = self._expired(record)
        export_id = str(record["export_id"])
        return {
            "export_id": export_id,
            "status": "expired" if expired else record["status"],
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "request": record["request"],
            "manifest": record["manifest"],
            "bundle": None if expired else record["bundle"],
            "download_path": None if expired else f"/warehouse/exports/{export_id}/download",
        }

    def _owned(self, export_id: str, principal: Principal) -> dict[str, JsonValue] | None:
        record = self._store.load(export_id)
        workspace = ownership.warehouse_workspace(principal.owner_id)
        if record is None or record.get("workspace_id") != workspace:
            return None
        return record

    def _live(self, workspace: str) -> list[dict[str, JsonValue]]:
        """The workspace's unexpired exports; expired ones lose their files here."""
        live: list[dict[str, JsonValue]] = []
        for record in self._store.records():
            if record.get("workspace_id") != workspace:
                continue
            if self._expired(record):
                bundle = self._store.directory(str(record["export_id"]))
                if bundle is not None:
                    (bundle / _BUNDLE).unlink(missing_ok=True)
                continue
            live.append(record)
        return live

    # ------------------------------------------------------------------ operations

    def create(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        try:
            if body is None:
                raise ValueError("request body is required")
            if not set(body).issubset(_FIELDS):
                raise ValueError("request contains unknown fields")
            name = body.get("table")
            snapshot = body.get("snapshot", "current")
            sql = body.get("sql")
            fmt = body.get("format", "csv")
            profile = body.get("profile", "machine")
            max_rows = body.get("max_rows", DEFAULT_EXPORT_MAX_ROWS)
            if not isinstance(name, str) or not name:
                raise ValueError("table must be a non-empty string")
            if not isinstance(snapshot, str) or not snapshot:
                raise ValueError("snapshot must be 'current' or a snapshot id")
            if not isinstance(sql, str) or not sql:
                raise ValueError("sql must be a non-empty string")
            if fmt not in ("csv", "jsonl"):
                raise ValueError("format must be csv or jsonl")
            if profile not in ("machine", "spreadsheet"):
                raise ValueError("profile must be machine or spreadsheet")
            if profile == "spreadsheet" and fmt != "csv":
                raise ValueError("the spreadsheet profile is for csv only")
            if (
                not isinstance(max_rows, int)
                or isinstance(max_rows, bool)
                or not 1 <= max_rows <= MAX_EXPORT_ROWS
            ):
                raise ValueError(f"max_rows must be an integer from 1 to {MAX_EXPORT_ROWS}")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})
        try:
            canonical_sql = validate_read_only_sql(sql).canonical_sql
        except UnsafeQueryError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "unsafe_query"})

        catalog = self._table_catalog()
        if catalog is None:
            return ServiceResponse(
                404,
                {"error": "this deployment has no warehouse", "code": "warehouse_not_configured"},
            )
        workspace = ownership.warehouse_workspace(principal.owner_id)
        table = next((t for t in catalog.list_tables(workspace) if t.logical_name == name), None)
        if table is None:
            return ServiceResponse(
                404, {"error": f"no such table: {name}", "code": "table_not_found"}
            )
        if len(self._live(workspace)) >= MAX_LIVE_EXPORTS:
            return ServiceResponse(
                429,
                {
                    "error": f"at most {MAX_LIVE_EXPORTS} exports are kept; delete one first",
                    "code": "export_quota_exceeded",
                },
            )
        try:
            pin = _pin(catalog, table, snapshot)
        except (TableNotFound, SnapshotNotFound) as exc:
            return ServiceResponse(404, {"error": str(exc), "code": "snapshot_not_found"})
        except SnapshotStateError as exc:
            return ServiceResponse(409, {"error": str(exc), "code": "snapshot_unavailable"})
        try:
            return self._create_pinned(
                catalog,
                table,
                pin.snapshot_id,
                pin.revision,
                request={
                    "table": name,
                    "snapshot": snapshot,
                    "sql": sql,
                    "format": fmt,
                    "profile": profile,
                    "max_rows": max_rows,
                },
                plan_fields=(canonical_sql, cast(ExportFormat, fmt), cast(ExportProfile, profile)),
                max_rows=max_rows,
                principal=principal,
            )
        finally:
            catalog.release(pin.lease_id)

    def _create_pinned(
        self,
        catalog: TableCatalog,
        table: TableRow,
        snapshot_id: str,
        revision: int,
        *,
        request: dict[str, JsonValue],
        plan_fields: tuple[str, ExportFormat, ExportProfile],
        max_rows: int,
        principal: Principal,
    ) -> ServiceResponse:
        snapshot = catalog.get_snapshot(snapshot_id)
        spec = read_snapshot_spec(self._output_root, snapshot.run_id)
        terms_refusal = forbidden_response(
            build_verdict(spec, self._terms_lookup), what="an export"
        )
        if terms_refusal is not None:
            return terms_refusal
        terms = source_terms(spec)
        if terms.forbids_derivatives:
            return ServiceResponse(
                403,
                {
                    "error": "the source's licence forbids derivative works, and an export of "
                    "a query result is one",
                    "code": "export_forbidden_by_license",
                    "terms": terms.body(),
                },
            )
        table_path = _readable_table(catalog, table, snapshot_id)
        if table_path is None:
            return ServiceResponse(
                404,
                {"error": "the snapshot holds no queryable table", "code": "artifact_unavailable"},
            )

        canonical_sql, fmt, profile = plan_fields
        export_id, directory = self._store.create()
        kept = False
        try:
            data_name = f"data.{fmt}"
            data_path = directory / data_name
            plan = ExportPlan(
                canonical_sql=canonical_sql,
                output_path=str(data_path),
                format=fmt,
                profile=profile,
                max_rows=max_rows,
                max_bytes=MAX_EXPORT_BYTES,
            )
            try:
                result = self._engine.execute_export(table_path, plan.to_json())
            except QueryBusyError:
                return ServiceResponse(429, {"error": "query is busy", "code": "query_busy"})
            except QueryTimeoutError:
                return ServiceResponse(504, {"error": "export timed out", "code": "query_timeout"})
            except QueryResourceLimitError as exc:
                return ServiceResponse(400, {"error": str(exc), "code": "query_resource_limit"})
            except QueryExecutionError:
                return ServiceResponse(
                    400, {"error": "export query failed", "code": "query_execution_failed"}
                )
            meta = result.meta
            refusal = meta.get("refusal")
            if isinstance(refusal, dict):
                what = "rows" if refusal.get("code") == "row_limit_exceeded" else "bytes"
                return ServiceResponse(
                    422,
                    {
                        "error": f"the result exceeds the export's limit of {refusal.get('limit')}"
                        f" {what}; nothing was exported",
                        "code": str(refusal.get("code")),
                        "limit": refusal.get("limit"),
                    },
                )
            findings = meta.get("pii")
            blockers = pii_blockers(
                spec,
                [f for f in findings if isinstance(f, dict)] if isinstance(findings, list) else [],
            )
            if blockers:
                return ServiceResponse(
                    403,
                    {
                        "error": "the result holds values that look like personal information, "
                        "and the source's PII policy does not allow them out",
                        "code": "export_blocked_pii",
                        "findings": blockers,
                    },
                )

            now = self._now()
            created_at, expires_at = _utc(now), _utc(now + EXPORT_RETENTION)
            altered = meta.get("altered")
            manifest: dict[str, JsonValue] = {
                "manifest_version": MANIFEST_VERSION,
                "export_id": export_id,
                "created_at": created_at,
                "expires_at": expires_at,
                "query": {
                    "sql": request["sql"],
                    # Any query can reshape, filter or compute; Builder cannot tell one
                    # that does not, so every query export is recorded as derived.
                    "user_derived": True,
                },
                "snapshot": {
                    "table_id": table.id,
                    "logical_name": table.logical_name,
                    "snapshot_id": snapshot_id,
                    "revision": revision,
                    "run_id": snapshot.run_id,
                    "artifact_digest": snapshot.artifact_digest,
                    "row_count": snapshot.row_count,
                    "coverage": _coverage(snapshot.coverage),
                },
                "source": {
                    "dataset_id": terms.dataset_id,
                    "terms": terms.body(),
                    "provenance": _provenance(
                        spec, read_manifest(self._output_root, snapshot.run_id), table.logical_name
                    ),
                },
                "output": {
                    "format": fmt,
                    "profile": profile,
                    "encoding": "utf-8",
                    "bom": profile == "spreadsheet",
                    "row_count": meta.get("row_count"),
                    "completeness": "full",
                    # Described like the query's columns (#702): a text code column is
                    # an identifier. The file's cells were written as the strings they are.
                    "columns": describe_json_columns(
                        meta.get("column_meta"),
                        spec_semantics(spec, table_key(spec, table.logical_name)),
                    ),
                    "values_altered": [
                        {"column": column, "count": count, "reason": "formula_prefix"}
                        for column, count in sorted(altered.items())
                    ]
                    if isinstance(altered, dict)
                    else [],
                    "file": {
                        "name": data_name,
                        "bytes": meta.get("bytes"),
                        "sha256": meta.get("sha256"),
                    },
                },
                "pii": {
                    "policy": spec.pii.mode if spec is not None and spec.pii is not None else None,
                    "allowed_findings": findings if isinstance(findings, list) else [],
                },
            }
            bundle = directory / _BUNDLE
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(data_path, data_name)
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )
                archive.writestr("NOTICE.md", _notice(manifest))
            data_path.unlink()
            record: dict[str, JsonValue] = {
                "export_id": export_id,
                "workspace_id": ownership.warehouse_workspace(principal.owner_id),
                "owner_id": principal.owner_id,
                "status": "completed",
                "created_at": created_at,
                "expires_at": expires_at,
                "request": request,
                "manifest": manifest,
                "bundle": {
                    "filename": _BUNDLE,
                    "media_type": "application/zip",
                    "bytes": bundle.stat().st_size,
                    "sha256": sha256_file(bundle),
                    "files": [data_name, "manifest.json", "NOTICE.md"],
                },
            }
            self._store.save(record)
            kept = True
            return ServiceResponse(200, self._body(record))
        finally:
            if not kept:
                self._store.remove(export_id)

    def list(self, *, principal: Principal) -> ServiceResponse:
        workspace = ownership.warehouse_workspace(principal.owner_id)
        records = sorted(self._live(workspace), key=lambda r: str(r["created_at"]), reverse=True)
        return ServiceResponse(200, {"exports": [self._body(r) for r in records]})

    def get(self, export_id: str, *, principal: Principal) -> ServiceResponse:
        record = self._owned(export_id, principal)
        if record is None:
            return _not_found(export_id)
        return ServiceResponse(200, self._body(record))

    def download(self, export_id: str, *, principal: Principal) -> ServiceResponse | FileResponse:
        """The bundle, after checking — now, not when it was made — who may have it."""
        record = self._owned(export_id, principal)
        if record is None:
            return _not_found(export_id)
        directory = self._store.directory(export_id)
        if self._expired(record) or directory is None:
            if directory is not None:
                (directory / _BUNDLE).unlink(missing_ok=True)
            return ServiceResponse(
                410, {"error": "the export has expired", "code": "export_expired"}
            )
        # Checked now, not when the export was made (#688): terms can be declared later.
        terms_refusal = self._terms_refusal(record)
        if terms_refusal is not None:
            return terms_refusal
        bundle = directory / _BUNDLE
        expected = cast(dict[str, JsonValue], record["bundle"])
        if (
            bundle.is_symlink()
            or not bundle.is_file()
            or sha256_file(bundle) != expected.get("sha256")
        ):
            return ServiceResponse(
                409,
                {
                    "error": "the export's file is missing or changed",
                    "code": "export_unavailable",
                },
            )
        return FileResponse(200, bundle, f"{export_id}.zip")

    def _terms_refusal(self, record: Mapping[str, JsonValue]) -> ServiceResponse | None:
        manifest = record.get("manifest")
        snapshot = manifest.get("snapshot") if isinstance(manifest, dict) else None
        run_id = snapshot.get("run_id") if isinstance(snapshot, dict) else None
        spec = read_snapshot_spec(self._output_root, run_id) if isinstance(run_id, str) else None
        return forbidden_response(build_verdict(spec, self._terms_lookup), what="an export")

    def delete(self, export_id: str, *, principal: Principal) -> ServiceResponse:
        if self._owned(export_id, principal) is None:
            return _not_found(export_id)
        self._store.remove(export_id)
        return ServiceResponse(200, {"export_id": export_id, "deleted": True})


__all__ = [
    "EXPORT_RETENTION",
    "MAX_LIVE_EXPORTS",
    "ExportStore",
    "ExportsApiService",
    "SourceTerms",
    "pii_blockers",
    "source_terms",
]
