# API Contract — KPubData Builder

## 1. Single Source of Truth

The single source of truth for KPubData Builder's (package `kpubdata-builder`) HTTP wire contract is [contract/builder-api.yaml](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/contract/builder-api.yaml).

- Endpoints, request bodies, response bodies, status codes and security schemes follow the OpenAPI document.
- `info.version` in `contract/builder-api.yaml` must match `kpubdata_builder.service.API_CONTRACT_VERSION`.
- `tests/unit/test_service_contract.py` verifies the version match, static route/status alignment, and wire-level conformance of actual `dispatch()` responses.
- Consumers like Studio base compatibility on the OpenAPI SSOT and `GET /version`, not on this document.
- Version bumps, Studio compatibility ranges, and release freeze procedures follow [ADR 0013](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/docs/adrs/0013-api-contract-release-policy.md).

### Contract Version Summary

- The contract version on `main` is always a stable SemVer; the OpenAPI document and the code use the same value.
- Additive wire changes are minor, contract-error fixes that preserve existing semantics are patch, breaking changes are major.
- Same-wire changes (examples, descriptions, internal refactors) do not bump the version.
- The two rules above are **CI-enforced** (#693). On every PR, `scripts/check_contract_compat.py` compares against the base branch's contract and fails when a normative change (anything other than description, summary, example, `x-*`) does not raise `info.version`, or when a breaking change — removing an operation, status code, media type, property, schema or enum value; changing a type or `$ref`; adding a new required parameter — arrives without a major bump. The explicit approval of an intended breaking change is the major bump itself.
- Studio checks the same major plus a per-feature minimum SemVer, not exact equality. It bumps its schema/client and minimum feature version only when it actually consumes a new operation.
- Completing Epic #484 is the point to freeze the final contract and record it in the release manifest and tag, not to defer version changes during development.

### The operations table (#1109)

Which method and path is which `operationId`, and which operations read a credential header, is written once: in `contract/builder-api.yaml`. The contract is not shipped in the wheel, so `scripts/generate_operations.py` writes what the service needs of it into `src/kpubdata_builder/service/_contract_operations.py` — a module nobody edits — and `service/operations.py` answers "which operation is this request" from it. `route_reads_provider_keys` and `route_reads_publish_credentials` read their answer there; neither spells out a path any more.

When an operation is added, removed or renamed in the contract, or gains or loses the `ProviderKey` / `PublishCredential` parameter, run `uv run python scripts/generate_operations.py` and commit the result. Two tests hold the pieces together:

- `tests/unit/test_contract_operations.py` fails when the generated module is stale, and holds the table and the lookup to the contract.
- `tests/unit/test_dispatch_answers_only_declared_operations.py` (#1054) asks the service itself: every declared operation is taken by a route, and no other method or assembled path is.

Routing is still done by the route adapters, by hand. What an operation may answer (`_OPERATION_STATUS_CODES` in `tests/unit/test_service_contract.py`) is also still declared by hand: the contract says what is allowed, and only reading the code says what is returned.

### Client Compatibility Rules (#814)

These are the rules the reading side follows. They pair with the server-side rules above.

- **Minor versions may add optional fields to responses.** Unknown fields are ignored. The contract's `additionalProperties: false` describes *what this version of Builder sends*, not an instruction for the client to reject extra fields. A strict response parser breaks on additive changes — Studio's `silverColumnInfoSchema.strict()` did exactly that when 1.30.0 added `logical_type`/`wire_encoding` (#735).
- **Required field names and types do not change until the next major.** A missing required field or a wrong type is still rejected. Leniency applies to unknown fields only.
- Unknown enum values (e.g. a new `wire_encoding`) are treated as opaque text, not guessed as numbers.
- `logical_type: identifier` (1.61.0, #702) is a text column the kpubdata spec declares as `semantic_kind: code` (legal district codes, PNU, postal codes). Its `wire_encoding` is always `string`; a client must not coerce it with `Number()` — leading zeros vanish and JOINs misalign. Which columns are identifiers comes from the kpubdata declaration; Builder never guesses from the values.

These rules ship as fixtures so machines can check them. `contract/fixtures/responses.json` holds three bodies for every 2xx named response example in the contract:

| Key | Meaning | Client expectation |
| :--- | :--- | :--- |
| `current` | Exactly what this contract version sends | Pass |
| `with_additive_fields` | `future_optional_field` added to every object that declares properties (`additive_paths` marks the location) | Pass |
| `required_type_broken` | One top-level required field (`broken_path`) with its type changed | Reject |

`contract_version` in the file header is the contract version the fixtures were generated from. `scripts/generate_response_fixtures.py` generates this file from the contract; it is not hand-edited. `tests/unit/test_response_fixtures.py` checks that the committed file matches regeneration, that `current` conforms to the contract, that additive-field bodies are rejected by a strict parser and accepted by a lenient one, and that required-field type errors are rejected by both. When a contract example changes, re-run the script and commit both. Studio's contract tests read this file to check their own response schemas (companion issue).

Error responses carry the same three bodies separately in `error_fixtures` (1.72.0, #947, #951), split from `fixtures` so a client that maps 2xx to a success parser never receives an error body. Every non-2xx named example appears once — operation-specific responses under `operation_id`/`method`/`path`/`status`, shared responses (`components.responses`: `Unauthorized`, `SignupNotApproved`, etc.) once under `response`/`status`. Examples: `saveRevision`/`revertRevision` 409 `RevisionConflict` (with `current_revision`), `SignupNotApproved` 403 `SignupPending`/`SignupRejected`. `SignupNotApproved` is not attached per-operation (many already declare their own 403); it is written on `bearerAuth` and `Error.code` as something any authenticated operation can return, with the status code in `x-status`.

This document is the operational guide humans read. It does not restate wire shapes.

### Builder-Owned Vocabulary (#831)

Every enum in the contract belongs to Builder (Independence Rule 7). Even when the values currently match kpubdata's, Builder is the owner, and kpubdata enum values are never passed through to the wire as-is. `src/kpubdata_builder/service/vocabulary.py` explicitly maps each kpubdata value to a Builder value, and any value not in the mapping becomes the declared fallback.

| Contract field | Builder vocabulary | Current value source | kpubdata value not in mapping |
| :--- | :--- | :--- | :--- |
| `DatasetStatusAxes.access` | `AccessStatus` | kpubdata probe classification + `unknown` | `unknown` |
| `CatalogDataset.representation` | `Representation` | kpubdata `Representation` | `other` |
| `CatalogDataset.operations[]` | `Operation` | kpubdata `Operation` | Removed from the list |
| `CatalogQuerySupport.pagination` | `PaginationMode` | kpubdata `PaginationMode` | `query_support` becomes `null` entirely |

When kpubdata adds values, the wire does not change. Builder decides what to call the new value in this mapping; adding a value to the wire bumps the contract version. `tests/unit/test_wire_vocabulary.py` verifies the mapping matches the contract enums, covers every value of the installed kpubdata, and that unknown values become the fallback.

## 2. Execution Model

The v0.4 Builder service keeps a synchronous execution model.

| Scope | Model | Reference |
| :--- | :--- | :--- |
| `/validate`, `/preview`, `/build`, read endpoints | Request-response, synchronous | ADR 0002 |
| Async job model (`POST /builds`, `GET /builds/{run_id}`, `POST /builds/{run_id}/cancel`) | Accept and return immediately; poll for status | ADR 0008 / #334 |

Principles:

- `POST /build` runs the pipeline within the current request and returns the success or failure result.
- `POST /builds` / `GET /builds/{run_id}` / `POST /builds/{run_id}/cancel` follow ADR 0008 (#334): state machine, idempotency, cooperative cancellation, and partial artifact rules.
- When Medallion stage-specific artifact/preview access is needed, a stage-specific endpoint is added to the OpenAPI SSOT first.

## 3. Response Policy

Detailed schemas and status codes follow the OpenAPI SSOT. The policy-level semantics are:

| Situation | Policy |
| :--- | :--- |
| Successful request | Per-endpoint success response, `200` |
| BuildSpec parse/load failure | Input error the client can fix |
| BuildSpec validation failure | Input error including a problem list |
| Preview source failure | The HTTP request itself can succeed; per-source `status`/`error` carries the failure |
| Build source failure | Treated as an upstream/source dependency failure; manifest preserved where possible |
| Per-run BuildSpec retrieval | `GET /builds/{run_id}/spec` returns the redacted canonical YAML and its bytes' digest |
| Built dataset retrieval | `GET /datasets`, `GET /datasets/{dataset_id}`, `GET /datasets/{dataset_id}/runs` return grouping by `BuildSpec.dataset_id`, latest run, and run history |
| Stage summary/preview | `GET /builds/{run_id}/stages`, `GET /builds/{run_id}/stages/{stage}` return per-source Bronze/Silver/Gold status with safe summaries |
| Structured quality/drift | `GET /builds/{run_id}/quality` returns per-source `quality_results`/`schema_drift` for the run; `GET /datasets/{dataset_id}/quality/history` returns the dataset's per-run PASS/WARN/FAIL aggregate history |
| Read-only query | `POST /query` registers the server-resolved Silver/Gold table as the logical `dataset` and runs within separate capacity/timeout limits |
| Composition (join) | When `BuildSpec.composition` is present, the `POST /build` response exposes a separate `composition` key (the join result) alongside per-source `outcomes` |
| Async job cancellation | `POST /builds/{run_id}/cancel` moves a `queued` job directly to `cancelled` before execution, and a `running` job through `cancelling` to `cancelled` at a safe stage boundary. Terminal jobs or jobs confirmed to have exited normally return `409` |
| Partial artifacts of a cancelled run | Preserved with a partial manifest (`status: cancelled`, `partial: true`). Stages that never ran are not recorded as successes; a cancellation is never labelled a failure, nor a failure a cancellation |
| Authentication failure | `401` means re-authenticate, `403` means request permission, `503` means a temporary JWKS outage. Repeated `401` from the same client becomes `429` (`code: "auth_throttled"`, `retry_after_seconds`) and is rejected without attempting authentication |

When policy and implementation disagree, do not add implementation footnotes. Fix in this order:

1. If the intended contract is different, update `contract/builder-api.yaml`.
2. If it is an implementation bug, fix the service code and conformance tests.
3. If Studio is affected, open a separate Studio client/docs PR.

### BuildSpec and Preview Contract Interpretation

- The `spec` field in HTTP requests is a YAML string, as now. The OpenAPI `BuildSpec` component defines the canonical domain structure that YAML represents, so type generators can read it.
- `metadata`, `sources[].params`, `exports[].options` values are standard JSON-compatible.
- Source preview returns `source_key`, `status`, `error`, `schema`, `sample`, `total_rows`, `statistics` for both success and failure. A failed source within HTTP 200 is represented by `status: failed`, empty schema/sample, zero-based statistics, and a string `error`.
- A failed run's manifest records each failure (#1120, contract 1.113.0): `failures` lists `source_key`, `stage`, a stable `code` and the public `summary`. The build index projects the first into `error`, so `GET /admin/runs` and a rebuilt index give the reason; before, the index's `error` was never written.
- A source a provider refused says why (#1187, contract 1.112.0): `BuildOutcome.reason` and `SourcePreview.reason` are a `SourceFailureReason` — `application_required`, `auth_unknown`, `params_invalid`, `rate_limited`, `temporarily_unavailable`, `network_error` or `retired`, the probe's words — and `error` is a fixed sentence saying what to do next. The provider's own text is never returned. A `source_fetch_failed` event carries the reason as `metrics.reason`. `reason` is null when no provider refused the source.
- In a multi-user deployment each owner may have `KPUBDATA_BUILDER_MAX_ACTIVE_BUILDS_PER_OWNER` async builds (default 2; `0` is off) queued, running or cancelling (#1189, contract 1.111.0). One more is refused by `POST /builds` with 429 `build_owner_limit` and `limit`; nothing is recorded for the run. The service-wide `build_queue_full` still applies; a single-user deployment and the synchronous `POST /build` have no owner limit.
- A build that finds no rows is not committed over a table that has a snapshot (#1186, contract 1.110.0): the build answers 409 with `warehouse_failures` reason `empty_result`, and the current snapshot stays. A source declaring `allow_empty: true` commits the empty result, with the current snapshot's columns when it found none. A first build of a table is committed either way.
- A preview reads a `public_api` source up to `limit` records or three pages, across its `param_grid` combinations (#1185, contract 1.109.0). `total_rows`, `statistics` and the quality results count the records read; `fetch_complete` says whether those are the whole source, and `source_reported_total` gives the provider's own count for a single call. A file or URL source is read whole. A build requests pages of the dataset's `max_page_size`.
- The removed `transforms`, top-level `normalization_mode`, and `sources[].normalization_mode` are not contract fields; the parser rejects them explicitly.
- A spec that passes validation is atomically saved to `{output_root}/{run_id}/buildspec.yaml` before entering the pipeline. For legacy runs without a snapshot, the API returns an unavailable `404` rather than guessing from the manifest.
- Snapshot redaction applies only to explicitly-mapped credential keys. Inline secrets are replaced with `<redacted>`, so a snapshot alone cannot re-execute a run that needs credentials; credentials must be supplied again from environment/service configuration.
- `spec_digest` is the SHA-256 of the **redacted canonical snapshot bytes actually stored**, not of the original object. Two specs that differ only in credential values intentionally share the same snapshot/digest.

### Built Dataset and Stage Summary/Preview (#488)

- **Identity**: a built dataset's identity is solely its `BuildSpec.dataset_id`. Directory names and source catalog names are never used to guess. Legacy runs without a `buildspec.yaml` snapshot (#487) have no guessable `dataset_id` and are silently excluded from `GET /datasets*` grouping (they still appear in `GET /builds`).
- **Latest run**: each dataset is summarized by the run with the latest `finished_at` among runs the principal can access. Ties at the same `finished_at` are broken deterministically by descending `run_id`. Ownership filtering applies before latest-run selection, so another user's run with the same `dataset_id` never appears in latest candidates or metadata.
- **row_count**: a multi-source dataset's row count is not collapsed to a single scalar. `row_counts` (a per-source_key map) and `total_row_count` (the sum) are provided together.
- **quality**: the `quality` field is always `null` until #486 (structured quality gates) is implemented. Log-only quality warnings are not arbitrarily converted to PASS/WARN/FAIL — unevaluated is not PASS.
- **stage status**: four states — `completed`/`failed`/`not_run`/`unavailable`. Success is never guessed from filesystem presence alone; manifest failure records and sidecar completeness are checked together so partial/failed runs still distinguish "Bronze succeeded → Silver failed → Gold never ran".
- **Secret/path non-exposure**: Bronze `fetch_params`/`provenance.fetch_params`, Gold export `options`/`output_path`, and every response never expose filesystem paths. Silver `sample` is returned within the preview limit persisted at build time (default 5 rows) and does not read the full Parquet.
- `dataset_id` can contain characters that cannot be used verbatim in a URL path (slashes, spaces), so clients must percent-encode the `dataset_id` in `GET /datasets/{dataset_id}`. No new constraint is added to `BuildSpec.dataset_id` itself for this API.

### Structured Quality/Schema Drift and History (#486)

- **The manifest is the source of truth**: `quality_results` (per-source_key list of `QualityCheckResult`) and `schema_drift` (per-source_key list of `SchemaDriftFinding`) are recorded in manifest.json at build time; `GET /builds/{run_id}/quality` exposes them as-is. Nothing is recomputed.
- **`availability` distinguishes "zero checks" from "never computed" (#514)**: an empty `quality_results: {}` alone cannot tell whether there were no rules to evaluate or whether the quality stage never ran. `GET /builds/{run_id}/quality` now also returns `availability` (`available`/`partial`/`unavailable`) and `evaluated_checks` (an integer). `available` means every source the run attempted (`manifest.inputs`) has quality results, though `evaluated_checks` may be zero (no rules configured). `partial` means only some sources have results (e.g., one source's Silver failed in a multi-source run). `unavailable` means no results at all — including legacy runs predating #486 (no `quality_results` field) and new runs where the field exists (as `{}`) but no attempted source has a result (e.g., every source failed before the quality stage — the manifest writer always records an empty `{}` even when nothing was computed). This field is additive; the existing `quality_results`/`schema_drift` shapes are unchanged.
- **Full preservation including PASS**: `QualityCheckResult` contains only checks that were actually evaluated, including PASS. Rules not configured or not evaluable (missing column, denominator zero) are excluded entirely — never faked as PASS.
- **Extended rule condition preservation**: a `range` result's `threshold` preserves `min`/`max`; a `compare_columns` result preserves `operator`/`right_column`. When the column exists but the dtype is incompatible, the rule is not omitted — a WARN/FAIL at the declared severity is recorded with a safe `detail`.
- **WARN/FAIL gate**: WARN lets the build continue and records the result. FAIL fails the source before Gold; the already-computed `quality_results` are still preserved in the manifest.
- **Preview/Build same evaluation**: each `SourcePreview.quality_results` from `POST /preview` is the result of the same evaluator (`quality.evaluate_quality`) the build uses. Preview does not include drift (it writes nothing to the workspace, so there is nothing to compare against a previous run).
- **Schema drift comparison scope**: `detect_drift` compares only against the previous **successful** run of the same `dataset_id` and `source_key`, not "any previous run". It never compares against another dataset/source's silver, which would create fake drift.
- **Quality History aggregate**: `GET /datasets/{dataset_id}/quality/history` reuses #488's dataset-to-run lookup helpers to return per-run pass/warn/fail counts, `evaluated_checks`, `rule_pass_rate` (`pass_count / evaluated_checks`, `null` when `evaluated_checks == 0`), and `validated_rows` for accessible runs. `validated_rows` reuses the `row_counts` sum semantics #488 already defined (per-source Silver row_count), not a sum over `QualityCheckResult.evaluated_rows` multiplied by rule count.
- **Legacy/partial/failed runs**: a legacy run without `quality_results` in its manifest is represented as `evaluated_checks=0, rule_pass_rate=null` — unevaluated is not interpreted as "all PASS". Partial and failed runs with structured results are included in history (not excluded by policy).
- **Ownership**: History and detail share the same ownership semantics as `/datasets/{dataset_id}` and `/datasets/{dataset_id}/runs`. Another user's runs never mix in, even with the same `dataset_id`.
- **AI interpretation does not affect gates**: even when drift cause interpretation (#448, advisory) exists, it does not participate in PASS/WARN/FAIL decisions or dataset quality summaries.
- The `quality` field in `GET /datasets` and `GET /datasets/{dataset_id}` responses is still always `null`. This is intentional: no arbitrary composite quality score is invented; structured results live in the two dedicated endpoints above.

### Read-only Query (#504)

- `/query` allows only a single SELECT/CTE referencing the `dataset` physical relation at least once. CTE `dataset` shadowing, recursive CTEs, external tables/table functions, filesystem/network access, and DML/DDL are rejected.
- Queries use a bounded capacity separate from the HTTP worker pool. The query timeout actually terminates the child process; 429/504 errors are distinguished by stable `code` values.
- The SQL dialect is DuckDB's (contract 1.74.0, #874). A query runs on a locked DuckDB connection that can read only the pinned snapshot file.

### The DuckDB cutover and client compatibility (ADR 0021, #877)

Builder's tabular engine moved from Polars to DuckDB (#864–#877). The engine's name is not part of the wire. A client — Studio included (kpubdata-studio#565) — needs to know only the contract changes below; to a user it is "Builder SQL". Name DuckDB compatibility only where the dialect itself has to be explained.

| Contract | What changed | What a client does |
| :--- | :--- | :--- |
| 1.60.0 (#867) | `provenance[].data_checksum` is `canonical-multiset-v2`, named by `data_checksum_algorithm`. The byte digest (`artifacts[].artifact_digest`) is separate. `artifact_writer` is the engine that wrote Gold — `duckdb` since #876 | Never compare checksums of different algorithms |
| 1.70.0 (#871) | Ratio splits are `hash-sort-v2` (manifest `split_algorithm`). With the same seed, rows land in different splits than under `shuffle-v1` | Do not compare split membership row by row with an earlier run |
| 1.74.0 (#874) | SQL, rows, aggregate, profile and export run on DuckDB. Result types are DuckDB's in Builder's dtype names: `COUNT(*)` is `int64`, an integer `SUM` is `int128` (a number or exact decimal text, by its values), an unnamed aggregate gets DuckDB's name (`count_star()`), `DESC` puts nulls last, a zoned datetime is sent in UTC. Settings and version functions and nondeterministic SQL (`random`, `now`, sampling) are `unsafe_query` | Read column names and types from each response's `columns`/`column_meta` instead of hard-coding them, and decode values by `wire_encoding` |
| 1.76.0 (#875) | `SavedAnalysis` gains `sql_dialect` (`duckdb` or `legacy-polars`), `engine`, `engine_version`, `query_contract_version` and `migration_required` | For an analysis with `migration_required`, ask the user to review its SQL and save it as a new analysis instead of running it — a run answers 409 `analysis_migration_required` |

The error codes (`query_busy` 429, `query_timeout` 504, `query_execution_failed`, `unsafe_query`, `invalid_request`) did not change. Since contract 1.107.0 (#961) a query over the deployment's memory or spill limit answers 400 `query_resource_limit` with the same fixed sentence a build or preview over its limits gives, never DuckDB's own text; it answered `query_execution_failed` before.

### Declared PII in Silver and Bronze Reads (#900)

Gold masks declared PII (kpubdata `license.pii_columns` + BuildSpec `sources[].gold.pii_columns`) (#689) but Silver and Bronze preserve the original values (#611). So every service path that reads Silver or Bronze masks or refuses **the same columns in the same way** as Gold (contract 1.68.0).

| Path | Behaviour |
| :--- | :--- |
| `POST /query` `stage: silver` | Queries run over a masked copy of Silver. Expressions like `upper(col)`, `substr`, `WHERE col = '…'` never see the original values. DuckDB writes the copy with the original's Builder dtypes and real column names in the file metadata (#891), so the response's `columns`/`column_meta` match an unmasked query (including all-null, Duration, Int128, zone). The response's `masked_columns` lists the masked columns. `stage: gold` is already masked at build time and does not change |
| `POST /preview` | Masks each source's `sample`, `source_sample` (original field names — walking back through `schema.coalesce` and `rename`), and those columns' `diffs`; records `masked_columns` |
| `GET /builds/{run_id}/stages/silver/{source}` | Masks `sample`, records `masked_columns`. Bronze stage detail has no rows |
| `GET /artifacts/{run_id}/{file_path}` | `bronze/{source}/…` and `silver/{source}/…` files for sources with declared columns are **all 403 `declared_pii_withheld`** (with column names in `columns`). Gold files and the manifest are served as-is |

- **Masking method** is the same as Gold's (#902): text values become `[masked]`, non-text dtypes become null, null stays null. `masked_columns` appears only when columns were actually masked.
- **Which columns are masked** is decided by one function: `stages/gold/pii.py`'s `columns_withheld_from_silver`. It is the `declared_pii_columns` declaration interpretation Gold uses, minus `gold.publish_unmasked` — only columns published in plaintext in Gold are plaintext in reads. Columns removed by `gold.select` are absent from Gold but present in Silver, so they are masked. `pii.allow_columns` is a scan-gate switch and does not unmask declared columns. Declarations are read with the same client factory as the build, and columns recorded in the run manifest's `pii_masking.masked` are added — a later catalog removing a declaration does not unmask runs built while it existed.
- **When the declaration cannot be read, refuse (fail closed)**: if the kpubdata declaration lookup for a public_api source fails, which columns are personal is unknown (#688 — "unknown is not permission"). The manifest record cannot substitute: it lists only what Gold masked, missing columns excluded by `gold.select` or columns from runs that never reached Gold. So that source's `/query` (`stage: silver`), `/preview`, and Bronze/Silver file downloads are **refused with 503 `pii_declaration_unavailable`** (with the unreadable dataset in `dataset`); Silver stage detail gives metadata but omits rows with `sample: []`, `sample_withheld: pii_declaration_unavailable` (the same shape as #892's `redistribution_forbidden`). 503 because the failure is a temporary server-side lookup, not a client error — it can be retried. File and url sources and BuildSpec-only `gold.pii_columns` need no lookup and are unaffected; Gold reads are also unchanged. When the lookup succeeds but the result has changed, the union with the manifest record is used as-is.
- **Why the original files are refused, not masked in transit**: rewriting `raw_records.jsonl`, `table.parquet`, or `preview.json` on the fly would export files that are not the artifacts under artifact names. When a masked table is needed, take Gold's. Sources without Silver (Bronze-only) are judged against all declared columns available; a stage directory whose source cannot be determined is judged against all sources of the run — unknown is not permission.
- **The same checkpoints as #892 (redistribution gate)**: artifact download and stage detail run through `BuilderService.serve_artifact_file` and `get_run_stage_detail`; `/query` through `QueryApiService.query` — right where #892 put the terms check, and **after** it. When `forbidden`, nothing leaves, so there is nothing to mask. `/preview` checks terms before fetch (from the spec alone) and PII after fetch (needing fetched results and the declarations the client read), so it masks within the same `SpecApiService.preview`, right after fetch. The policy modules were not merged (`service/redistribution.py` and `service/pii_reads.py`): terms **refuse** reads, declarations **mask** them — different answers.
- Warehouse reads, exports and profiles read the Gold snapshot, so they are not in scope for this change.

### Composition/Join (#506)

- `BuildSpec.composition` joins the validated Silver of two sources and produces a separate combined Gold dataset (`gold/{composition.name}/`) as an addition. Per-source independent Gold is preserved; existing multi-source BuildSpecs without `composition` are unaffected.
- Sources referenced by `composition.join.left`/`right` must declare an `alias`, and within a BuildSpec that uses `composition`, the declared `alias` values must differ — this rule does not apply to BuildSpecs without `composition`.
- Structural problems — alias references, `join.type`/`join.on_duplicate_key` vocabulary — are rejected by `validate_spec` immediately after parsing. Join key column existence and dtype compatibility (exact match only, no automatic casting) require both sources to have passed Silver, so they are the build pipeline's runtime gate.
- When both join keys have duplicate values (many-to-many), the output rows can multiply explosively — the default `on_duplicate_key: warn` produces the result and logs a warning; `fail` fails the composition.
- The `composition` key in `POST /build` responses is `{name, status, error}` where `status` is one of `ok`/`failed` (the join itself failed)/`skipped` (a referenced source failed, so the join was never attempted). When only the composition fails and all sources succeed, the top-level `error` summary derives from the composition's error.
- `manifest.json`'s `composition` (`CompositionProvenance`, additive) records the join conditions and both sides' row counts, distinct key counts, and output row count, so the sources and join conditions are traceable. Legacy manifests lack this field or have null — they are read as "runs without composition".

### Stable Owner Identity (#505)

- Display identity (human-readable labels) and persistent resource ownership identity are separated. `manifest.json`'s `created_by` keeps the existing (#388) display/legacy label; the new `owner_id` (additive) is the canonical stable identity used for ownership decisions.
- `owner_id` is a domain-separated SHA-256 hash per principal kind. For OIDC, `sha256(kind + "\0" + issuer + "\0" + subject)` hashes the full issuer/subject (no truncation) — the value does not change when email or display name changes, and never collides with another user whose subject prefix happens to overlap. Raw `sub`/email and other sensitive claims are never left in `owner_id` or in logs.
- Ownership decisions (`_check_ownership`, `/query`, dataset/quality/stage listings) compare `owner_id` first when both the record and the principal have one. When either side lacks it (e.g., legacy runs predating #505), they fall back to the existing `created_by`/label comparison — existing resources do not become immediately inaccessible. Records with neither `owner_id` nor `created_by` are not treated as "accessible to everyone" but are refused (fail-closed).
- `owner_id` is internal to ownership decisions. It is stored in the on-disk `manifest.json` and derived `BuildIndex`, but removed from HTTP responses including `/builds` listings and `GET /builds/{run_id}/manifest`. It is therefore not a public property of the OpenAPI `BuildManifest`, and neither the wire contract nor the API contract version changes.
- Subject-prefix collision prevention applies to new resources recorded with `owner_id` after #505. Pre-#505 legacy resources without `owner_id` fall back to the existing `created_by` label for compatibility, so already-stored truncated subject prefix collisions cannot be retroactively resolved.
- Adopting a specific new IdP or email/password login is outside this section's scope — the stable owner identity computation was settled first, independent of any future IdP decision (#515).

### System Resource and Build Statistics API (#516)

`GET /monitoring/summary` and `GET /monitoring/builds` provide the system/aggregate observability Studio's Monitoring view needs. Per-run events (#496) are a separate scope.

- **"Unknown" and "zero" are distinguished.** Values that were never measured are not faked as `0`/`healthy` but expressed as `null` with `available`/`partial`/`unavailable` (reusing the vocabulary quality.py already defines).
- **Aggregate status** (`MonitoringSummaryResponse.status`): a deterministic judgment computed from the `availability` of required subsystems (`api`/`queue`/`workers`/`artifact_store`). All four `available` → `healthy`; any `partial`/`unavailable` → `degraded`. Latency SLA thresholds (e.g., p95 100ms/500ms/1s) are not used without evidence (ADR/config) — `sample_count=0`/`p95_latency_ms=null` itself, or an actual zero count in `queue`/`workers`, is not a degraded reason (only actual `unavailable`/`partial` availability is). Provider status (#492) is optional and not in this judgment.
- **Builder API status**: `dispatch()` execution times are recorded in a bounded ring buffer of the most recent 1000 requests, and p95 is computed nearest-rank (no interpolation). `sample_count=0` → `p95_latency_ms=null`. When the collector itself is broken and samples cannot be read (#527): `availability=unavailable` + `sample_count=null` + `p95_latency_ms=null` — "healthy with no samples" and "cannot measure" are not conflated, and this subsystem's failure does not fail the original request or the monitoring response. Healthy/degraded latency threshold judgments are not invented without evidence (ADR/config) — only raw `sample_count`/`p95_latency_ms` are provided.
- **Queue/Worker**: the async build execution model is implemented with `AsyncBuildExecutor`/`AsyncBuildJobRegistry` (#511/#513); `BuilderService` always creates it and uses it for `POST /builds` (async) submissions. `queue`/`workers` directly reflect this executor's read-only snapshot (`AsyncBuildExecutor.stats()`), so in a normal runtime they are always `availability: available`. `waiting` is the count of active jobs with status=`queued`, `running` with status=`running`, and `total = waiting + running` — terminal (`succeeded`/`failed`/`cancelled`) job history the registry retains is not mixed in. `workers.active` equals `running` (one worker runs one job); `workers.capacity` is the `max_workers` preserved at executor creation, not read from `ThreadPoolExecutor`'s private fields. `workers.utilization` is `active / capacity` (0.0–1.0). `availability: unavailable` is kept only as a fallback for (currently unreachable) genuinely async-unsupported configurations, and only then are the remaining fields `null` — "zero jobs" and "cannot check" are distinguished. `BoundedThreadingHTTPServer`'s `ThreadPoolExecutor` (#253) is the HTTP connection concurrency limit, unrelated to this.
- **Artifact store**: the mere existence of the `output_root` folder does not count as `available` — a BuildIndex query must also succeed. `last_write_at` is obtained only from the `finished_at` of the most recent successful (`ok`) build recorded in BuildIndex; without a success record it is `available` with `null` (distinguishing zero from unknown). Filesystem paths are never exposed.
- **Build statistics** (`/monitoring/builds`): timezone is UTC, bucket boundaries are half-open `[start, end)`, and the bucket timestamp is `finished_at` (BuildIndex records only completed builds, ADR 0003). Currently only `window=24h`/`bucket=hour` is supported; other values return 400. Malformed timestamps (parse failure, containing NULL) are excluded from aggregation and reflected in `excluded_count`, making the overall `availability` `partial` (a BuildIndex query failure is `unavailable`; a normal aggregate result of zero is `available`). Bucket count wire fields are `total`/`success`/`failed`/`cancelled` — the internal BuildIndex status value `ok` is kept as-is (no change); only the external Monitoring API field name maps to `success` (#527).
- **Provider status** is not included in this version's Monitoring responses because it would trigger real network probes per request (#492).
- **Ownership**: the system aggregates (`api`/`queue`/`workers`/`artifact_store`) contain no individual run's dataset/owner/credential information, so no filtering is needed. `/monitoring/builds` bucket aggregates and recent runs are filtered with the same policy as `principal_owns()` (#505) when `ENFORCE_OWNERSHIP`+oidc principal applies, so another user's runs never mix in. Bucket aggregates fetch the whole window first and filter with no loss; `recent_runs` is a fixed LIMIT-10 query, so the filter is applied in SQL **before** the LIMIT (`BuildIndex.list_recent_owned`, #527) — otherwise another principal's recent runs could fill the LIMIT and omit the requester's own.

### File and URL Source Ingestion (#498)

Public API, file and URL sources share the same canonical source contract and Bronze→Silver→Gold pipeline. `BuildSpec.sources[].kind` distinguishes `public_api` (default) / `file` / `url`; omitting `kind` always means `public_api`, preserving existing behaviour.

- **File upload**: `POST /uploads` accepts a raw binary body, not JSON (binary upload instead of multipart). `format`/`encoding`/`filename` are passed as query parameters; the server validates parseability immediately (corrupt or empty files return 400) for fail-fast. Stored content is isolated by the requesting principal's `owner_id`; no API returns the original content — `GET /uploads/{upload_id}` returns metadata only. An upload referenced by `BuildSpec.sources[].upload_id` must have the same owner as the principal requesting the build/preview; otherwise it is treated identically to not-found without distinguishing existence (fail-closed, same ownership pattern as #505). `sources[].format`/`encoding` must exactly match the values validated at upload time. Upload content is stored in SQLite; at 8 MiB or above it spills to a server-named file (#622) — either way, the user-supplied filename/path is never used in the storage path, so there is no path traversal surface, and `upload_id` is an opaque server-issued identifier (`upl_<hex32>`) — users cannot reference filenames/paths directly.
- **URL fetch (P0)**: a `kind="url"` source supports only GET with Auth=None over safe HTTP(S) (Bearer credential integration is P1 after #492). As SSRF defence, schemes other than `https` and URLs containing userinfo are refused; hostnames are resolved directly via DNS and connections to non-global addresses (loopback, private, link-local, reserved) are blocked (any non-global address refuses the whole request). The actual TCP connection opens directly to the validated IP, preventing DNS rebinding between validation and connection; each redirect repeats the same validation (max 5). Response size and connect/read timeouts are capped. The BuildSpec contract has no header/POST/PUT/PATCH fields at all, so arbitrary headers or non-GET methods cannot be expressed. **`url` sources are disabled in multi-user deployments (#685).** `POST /preview`, `POST /build`, `POST /builds` refuse with `403 url_source_forbidden` before sending any request — see `BUILD_SPEC.md`.
- **Provenance/manifest non-exposure**: a file source's provenance contains only the `upload_id`, never the local filesystem path. A url source's provenance/manifest contains only the endpoint with the query string removed, so no incidentally-embedded secret survives. Both kinds reuse the existing `SourceProvenance` shape (provider/dataset fields) — file fills `provider="file", dataset=upload_id`; url fills `provider="url", dataset=<path-safe slug based on host+path>` (the human-readable original endpoint stays separately in `fetch_params.endpoint`).
- **Preview/Build same path**: `POST /preview` and `POST /build` share the same source resolver, so file/url sources go through the same schema/sample/quality evaluation flow as public_api sources.

## 4. Authentication and CORS

CORS is default-deny for browser clients (Studio, etc.).

- Without `KPUBDATA_BUILDER_ALLOWED_ORIGINS` set, cross-origin requests are refused.
- Same-origin requests are allowed.
- Preflight allows `GET, POST, PUT, DELETE, OPTIONS` and `Content-Type, X-API-Key, Authorization, X-Provider-Key, X-Publish-Credential` for allowed origins.

Authentication supports two paths:

| Method | Header | Use | Environment |
| :--- | :--- | :--- | :--- |
| API Key | `X-API-Key: <secret>` | Service accounts, scheduled workflows | `KPUBDATA_BUILDER_API_KEY` |
| Bearer (OIDC) | `Authorization: Bearer <jwt>` | Human users, Studio | `OIDC_ISSUER` + `OIDC_AUDIENCE` |

OIDC is enabled only when `OIDC_ISSUER`/`OIDC_AUDIENCE` and allowlists (`OIDC_ALLOWED_HD`, `OIDC_ALLOWED_SUBJECTS`, `OIDC_ALLOWED_EMAILS`) are configured.

**Whether a provider key is stored depends on the deployment mode**, so "keys are not stored" is true of one mode only:

| Mode | Provider key |
| :--- | :--- |
| Multi-user (OIDC, or `KPUBDATA_BUILDER_ENFORCE_OWNERSHIP`) | Not stored. Sent per request in `X-Provider-Key` and held in memory for the request or the async job only (#683); `PUT /providers/{provider}/credential` answers 403 `credential_storage_disabled` |
| Single-user (API key) | Stored, encrypted with `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` (AES-GCM, ADR 0012), per principal |

## 5. CLI Correspondence

CLI and HTTP service mode share the same domain contract.

| CLI | API operation |
| :--- | :--- |
| `kpubdata-builder validate spec.yaml` | `validateSpec` |
| `kpubdata-builder preview spec.yaml` | `previewBuild` |
| `kpubdata-builder build spec.yaml` | `createBuild` |

When CLI and HTTP response semantics diverge, check the OpenAPI and service conformance tests first.

## 6. Python API — BuilderService

Python code can call the same service logic without HTTP through `BuilderService`.

```python
from pathlib import Path

from kpubdata_builder.service import BuilderService

service = BuilderService(
    output_root=Path("./dist"),
    client_factory=lambda: my_kpubdata_client,
)

validate_response = service.validate(spec_yaml_str)
build_response = service.build(spec_yaml_str, run_id="my-run-001")
```

The returned objects' body shapes are also subject to the OpenAPI SSOT and conformance tests.

## 7. Related Documents

| Document | Role |
| :--- | :--- |
| [contract/builder-api.yaml](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/contract/builder-api.yaml) | HTTP wire contract SSOT |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec input contract |
| [BUILD_STATE.md](./BUILD_STATE.md) | Build state model |
| [BOUNDARY.md](./BOUNDARY.md) | Builder–Studio boundary |
| [docs/adrs/0002-build-execution-model.md](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/docs/adrs/0002-build-execution-model.md) | v0.4 synchronous build model decision |
| [docs/adrs/0005-api-contract-single-source.md](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/docs/adrs/0005-api-contract-single-source.md) | OpenAPI SSOT decision |
