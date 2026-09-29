# Changelog

## [Unreleased]

### Security

- Dependencies with known vulnerabilities are upgraded in `uv.lock` (#691): pyarrow 17.0.0 → 25.0.1 (PYSEC-2026-113), idna 3.11 → 3.20, bleach 6.3.0 → 6.4.0, pytest 8.4.2 → 9.1.1, setuptools 82.0.1 → 84.0.0, mkdocs-material 9.7.6 → 9.7.7 and pymdown-extensions 10.21.2 → 12.1 — 18 advisories across 7 packages, none left. A new `Security` workflow audits the locked dependencies with pip-audit, scans the full git history with gitleaks (reviewed false positives are listed with a reason in `.gitleaksignore`) and runs CodeQL, on every pull request and weekly.
- Move to kpubdata 0.7 (`kpubdata>=0.7.0,<0.8`, #746). 0.4.0 was pinned below 0.7 and so installed kpubdata 0.6.x, which can leak provider API keys into logs and tracebacks, disables TLS verification for lofin, and would send a provider key to any host a spec named. See the [kpubdata 0.7.0 release](https://github.com/yeongseon/kpubdata/releases/tag/v0.7.0).

### Added

- Pull requests are checked for contract compatibility (#693). `scripts/check_contract_compat.py` compares `contract/builder-api.yaml` with the base branch's and fails the lint job when a normative change leaves `info.version` where it was, or when something a client relies on is removed or retyped — an operation, a status code, a media type, a property, a schema, an enum value, a `type` or `$ref` — or a parameter becomes required, without a major version raise. The major raise is the explicit approval for an intentional break. Replayed over the contract's history, it finds changes that shipped without a version raise, among them `/admin/runs` under 1.27.0 and `SourceRef.normalization_mode` removed under 1.3.0.
- Each release's notes end with what it was tested with: the kpubdata version `uv.lock` pinned while the release gates ran, and the Builder API contract version (#718). `scripts/release_facts.py` writes the line after the gates and before the tag, so the compatibility table in kpubdata records a tested version rather than the `pyproject.toml` range.
- `kpubdata-builder serve --warehouse DIR` (or `KPUBDATA_BUILDER_WAREHOUSE`) gives the HTTP service a table catalog, so `POST /build` commits each source's Gold output as a table snapshot and reports it under `materialized` (#703). The service accepted a catalog root before, but nothing that starts it passed one, so a deployed service could never reach that end state. No export target and no publish credential are needed.
- Warehouse snapshots can be held past garbage collection, and a warehouse can be backed up and restored (#705). `TableCatalog.place_hold(snapshot_id, kind=saved_analysis|retention|audit, reason=..., expires_at=...)` keeps a committed snapshot until the hold is released or lapses; collection refuses a held snapshot in the same transaction that would retire it, and reports it as `kept_held` (catalog schema 3 → 4, migrated in place). `kpubdata-builder warehouse-backup DIR DEST` copies the catalog through SQLite together with every readable snapshot, checking each copy against its digest; `warehouse-restore BACKUP DIR` restores into an empty directory only after checking that every snapshot's table exists, every current pointer names a committed snapshot, and every snapshot directory is present, non-empty and matches its digest — otherwise it restores nothing and lists every problem. `docs/deployment.md` documents what `--stale-hours` means.
- `GET /datasets/{dataset_id}/runs/{run_id}` finds one run by id, not only among the newest page `/runs` returns, so a permalink to an older run opens (studio#418, API contract 1.31.0). Membership and ownership are decided by the server: 404 when no run with that id belongs to the dataset, 403 when it does but not to the caller.

### Changed

- JOIN cardinality is checked on the keys that intersect, not inferred from each side on its own (#698, API contract 1.32.0). `composition.join` gains `keys` (a composite key as `{left, right}` column pairs; `left_key`/`right_key` stay as the single-pair shorthand, and exactly one form must be given), `cardinality` (`one_to_one`/`one_to_many`/`many_to_one`/`many_to_many`, a violation fails the build) and `on_null_key` (`warn`/`fail`). The manifest's `composition` records `keys`, `cardinality`, `observed_cardinality`, both sides' unmatched ratios, `expansion_ratio` and the rows dropped for a null key. **Behaviour change:** `duplicate_key_warning` now fires only when a key present on both sides repeats on both sides, so a spec whose keys are non-unique on both sides but never meet (left `A, A`, right `B, B`) with `on_duplicate_key: fail` no longer fails.

### Fixed

- Drift compares a row count only against a run that collected the same population under the same schema contract (#700). The manifest's `drift_evaluation` now carries a `volume` axis next to `schema`: the schema axis still takes the newest run of the same owner, while the volume axis takes the newest one whose coverage (provider, dataset, params, param grid; credentials redacted) and `schema` block match, and otherwise records `coverage_mismatch`, `coverage_unknown` or `schema_contract_changed` instead of a `row_count_jump` between two different populations. Snapshots a build commits to the warehouse catalog now record `coverage_fingerprint`, `source_params_fingerprint` and `schema_contract_version`. A "no baseline" reason no longer says how many runs other owners have, or that they have any.
- Published Hugging Face cards state the terms their source grants instead of `cc-by-4.0` (#758). The 19 legacy configs record `license: other` with `license_name` (`korea-public-data-unrestricted`, `kogl-type-1`, or `kogl-type-3` for `air_quality`) and a `license_link` to the source page, and `scripts/pipeline/package.py` writes both to the front matter. There is no default licence any more: a card without `license`, or `other` without its name and link, is refused before anything is packaged or staged for Kaggle. `seoul_apartment_rent` pointed at the wrong data.go.kr service (15126471, pre-sale rights resale) and now points at 15126474, and `seoul_apartment_trades` no longer describes its source as KOGL Type 1 under CC-BY-4.0. `korea_base_rate` is unchanged until the ECOS terms are confirmed.
- Values no longer lose precision on the way to a client (#735). `/query`, `/preview` and the silver stage sample send every Decimal column, and any integer column holding a value outside ±(2^53−1), as exact decimal text: `9007199254740993` used to arrive as `…992`, and `Decimal("0.1")` as `0.1000000000000000055…`. In-range integers and floats are still JSON numbers. Column metadata gains `logical_type` and `wire_encoding` so a client knows which columns arrive as text (`QueryResponse.column_meta`, API contract 1.30.0). Non-finite floats are sent as `null`, and a Decimal column no longer makes the silver sample write fail. **Wire change:** `/preview` dates and datetimes in `sample`, `source_sample` and the diff are now ISO 8601 (`2025-01-01T12:30:00`), matching `/query` and the stage sample; they were `str()` output with a space separator.

## v0.4.0 — 2026-09-28

### Added
- **CUBRID state backend (ADR 0016, #579)**: abstracts the persistence backend into a selectable one — `KPUBDATA_BUILDER_STORAGE_BACKEND` (`sqlite` default / `cubrid`) + `KPUBDATA_BUILDER_CUBRID_URL` (`cubrid+pycubrid://…`). Adds a CUBRID implementation based on SQLAlchemy (`sqlalchemy-cubrid[pycubrid]`) behind the `BuildIndex`/`CredentialRepository`/`ArtifactStore` Protocols (optional `cubrid` extra — the default sqlite/local path stays free of external dependencies, with no SQLAlchemy dependency). Moves manifest documents to CUBRID rows as the source of truth + an FS mirror (artifact bytes stay on the block volume; supersedes ADR 0003's "manifest.json is the source of truth" clause for the cubrid backend only), and adds an OCI Compute VM + Docker Compose deployment (`infra/oci/`). Verified by real CUBRID integration contract tests (`pytest -m cubrid`, `.github/workflows/cubrid.yml`). Strategy: sqlite for local development, cubrid for deployment.
- **Dashboard aggregate contract (#488/#486 follow-up, additive)**: adds `total` to the `GET /datasets` response (the count of accessible distinct dataset_ids after canonical grouping + ownership, before pagination) — so the Studio Home DATASETS KPI does not mistake the `datasets` length/limit for the total. The new `GET /quality/summary?window=24h` summarises the structured quality of accessible runs in the last 24h as `total_runs`/`evaluated_runs`/`pass_runs`/`warn_runs`/`fail_runs` (a bounded cross-run aggregate of domain Quality — kept separate from system observability `/monitoring/*`). Both queries apply ownership and do not count unavailable/0-check runs as PASS. API contract 1.21.0 → 1.22.0
- **Multi-source Join/Composition (#506)**: `BuildSpec.composition` (`CompositionSpec`/`JoinSpec`) equi-joins the validated Silver of two sources to produce a combined Gold dataset (`gold/{composition.name}/`). Required/duplicate alias validation, a runtime gate on join key existence/dtype match, a warn/fail gate on duplicate-key many-to-many blow-up, and the manifest `composition` (`CompositionProvenance`, additive) and the `POST /build` response `composition` key expose the combined result separately from the per-source results. API contract 1.11.0 → 1.12.0
- **Built Dataset Catalog·Detail·Stage Summary API (#488)**: `GET /datasets`, `GET /datasets/{dataset_id}`, `GET /datasets/{dataset_id}/runs` query grouping/latest run/run history per `BuildSpec.dataset_id`. `GET /builds/{run_id}/stages`, `GET /builds/{run_id}/stages/{stage}` query per-source Bronze/Silver/Gold status and a safe summary/preview
- **Authentication system (B2-B5)**: Principal abstraction (#384), Google OIDC Bearer verification (#385), allowlist gate (#386), API contract bearerAuth (#387)
- **Authorization (C1/C2)**: principal recorded in manifest·BuildIndex (#388), ENFORCE_OWNERSHIP flag (#389)
- **BuildSpec assistant (BL1-BL4)**: ADR 0011 (#415), GET /catalog (#416), structured /validate problems (#417), API 1.2.0 (#418)
- **Unauthenticated /healthz** + Dockerfile HEALTHCHECK (#372)
- **Graceful SIGTERM shutdown** + max_workers env/CLI (#374)
- **CORS Authorization header** + file response Origin (#382)
- **Dockerfile ARG EXTRAS** (#373)
- **Azure Bicep IaC** (#378)
- **Deployment guide** docs/deploy.md (#390)
- **Request ID tracing** (#379)
- **ADR 0008** async job model (#334)
- **ADR 0009** user authentication with Google OIDC (#383)
- **ADR 0010** ArtifactStore + state backend (#375)
- **ADR 0011** BuildSpec assistant grounding (#415)
- **Environment variable cross-check test** (#424)
- Container entrypoint fails closed (ADR 0006)

### Changed
- **Split out the quality domain service (#596 fifth slice)**: moves per-run structured quality (#486/#514) and the 24h window aggregate to `QualityApiService` in `service/quality_api.py`, leaving `BuilderService` as a thin delegate. The 24h aggregate's run set **reuses** `DatasetsApiService`'s canonical record collection — which is why the previous slice made that helper public. `monitoring_summary` does not come in here: it keeps the boundary of not mixing domain quality and system observability in one response. No wire contract change
- **Split out the dataset domain service (#596 fourth slice)**: moves the built dataset query surface (`/datasets`, `/datasets/{id}`, `/runs`, quality history) to `DatasetsApiService` in `service/datasets_api.py`, leaving `BuilderService` as a thin delegate. The run record collection helper is shared with the quality domain, so it is **exposed as public** so that the next slice can depend on it instead of duplicating it. The rule that the ownership filter applies before grouping/latest selection (#488 semantics D) is stated in the file docstring. No wire contract change
- **Split out the query domain service (#596 third slice)**: moves `POST /query` to `QueryApiService` in `service/query_service_api.py`, leaving `BuilderService` as a thin delegate. The request body parser moves with it, so which status code and `code` each of permission, missing artifact, context error, unsafe SQL, congestion, timeout and execution failure becomes reads in one place. The module is named `query_service_api` because `kpubdata_builder.query.service` already has the execution engine's `QueryService`. No wire contract change
- **Split out the upload domain service (#596 second slice)**: moves `create_upload`/`get_upload`/`delete_upload` to `UploadsService` in `service/uploads_service.py`, leaving `BuilderService` as a thin delegate. The store is passed as a **callable provider** (a lambda) rather than an object, preserving the lazy creation (#498) that keeps `.service/uploads.sqlite3` from appearing in a workspace that does not use uploads. No wire contract change
- **Split out the provider domain service (#596 first slice)**: starts splitting by domain the structure in which `BuilderService` held providers/uploads/query/builds/datasets/quality in one class. Moves provider listing, connection test and credential CRUD to `ProvidersService` in `service/providers_service.py`, leaving the corresponding `BuilderService` methods as thin delegates. The new service receives **only its own dependencies** (credential resolver, client factory, provider test settings) — injecting the whole `BuilderService` would only add a class and leave the coupling as it was. The wire contract (status codes, body keys, routing, auth gate) does not change
- **Pin CUBRID CI to actually verify CUBRID (#587)**: the dedicated job's engine fixture silently falls back to in-memory SQLite when `KPUBDATA_BUILDER_CUBRID_URL` is absent — when URL injection was missing, **the job passed green without touching a single line of the CUBRID dialect**. With `KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID=1` (set by the job) the fallback is forbidden, and a test asserting that the dialect/driver is `cubrid`/`pycubrid` is added. Also injects `KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid`, which the job was missing, and fixes the pre-startup configuration validation (fail-closed) test and the wrong ADR number in the `cubrid` marker description (0013 → 0016)
- **Align the package version with the CHANGELOG line (#592)**: raises `version` in `pyproject.toml` from `0.1.0` → `0.4.0.dev0` to match this document's v0.4 section. `kpubdata_builder.__version__` drops the hardcoded string and derives from the installed distribution metadata, so `pyproject.toml` is the single source of truth for the version — until now the GHCR image tag, the `--version` output and the manifest's `builder_version` all claimed 0.1.0. `tests/unit/test_version.py` prevents the three values from drifting again
- API contract 1.21.0 → 1.22.0 (adds `total` to `GET /datasets`, adds `GET /quality/summary`, additive, #488/#486 follow-up)
- Adds a Provider credential store (`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`) operations section to the README — master key required/reused, existing credentials cannot be decrypted after rotation, 503 (store not configured) distinguished from `configured:false` (not registered), secrets never exposed
- API contract 1.0.0 → 1.2.0 (/healthz + bearerAuth + /catalog + StructuredProblem)
- API contract 1.4.0 → 1.5.0 (adds the Dataset Catalog·Detail·Stage Summary API, additive, #488)
- BuildIndex schema v2 → v3 (created_by)
- BuildIndex schema v3 → v4 (dataset_id-derived search column, #488)
- created_by field in BuildManifest
- Adds structured_problems to ValidationError
- README authentication description corrected to match the fail-closed policy (#423)

### Fixed
- **Live publishing always failed because of a missing `--no-sources` (#625)**: only the last step of `publish-dataset.yml` called `uv run` bare. `uv run` re-resolves the environment before running, so the editable `../kpubdata` override in `[tool.uv.sources]`, which the `uv sync --no-sources` just above ignored, came back on that line, and with no sibling directory on the runner it died with `Distribution not found`. Without secrets the guard before it exits with `exit 0`, so the defect surfaced **from the moment secrets were added**. `[tool.uv.sources]` stays as is — CONTRIBUTING.md documents it as the local development mechanism and `cross-repo-contract.yml` stands on it. Also aligns the 3 places in `CONTRIBUTING.md` and the `cubrid.yml` comment that wrote the `kpubdata` pin as `>=0.5.0,<0.6` with the actual `pyproject.toml` pin (`>=0.6.0,<0.7`). The same string in ADR 0007 is a record of the decision at that time, so it is not changed
- **CI guard that catches `uv.lock` drift (#625)**: `uv.lock` records the `--no-sources` resolution (what CI and deployment install), but the local command CONTRIBUTING recommends, `uv sync --extra dev`, runs with sources on, flips the lock's `kpubdata` from `registry` → `editable "../kpubdata"` and erases the distribution hashes. Once that state is committed, what CI installs and what the lock describes diverge, and no check caught it. Adds `uv lock --check --no-sources` to the lint job and documents in CONTRIBUTING how to avoid committing a lock dirtied locally
- Fixes a bug where `stages/_path_safety.ensure_within` intermittently reported a false traversal on Windows when building several sources in parallel (ThreadPoolExecutor) — caused by an asymmetry in which only one of `root`/`target` got the `\\?\` extended prefix from `Path.resolve()` (found while investigating #506; a pre-existing bug unrelated to composition)

### Removed
- **Coverage data and personal agent settings committed at the root (#627)**: untracks `.coverage.devbox.pid2864159.*` (an 80 KiB SQLite left by coverage.py parallel mode, introduced in #600) and `.claude/settings.local.json` (containing another contributor's absolute paths). `.gitignore` had only `.coverage`, which did not catch the `.<host>.<pid>.<rand>` suffix parallel mode appends, so `.coverage.*` is added. Only `settings.local.json` is ignored, not all of `.claude/` — shared settings and skills must remain trackable
- Untrack .omc/state/sessions (#380)
- Move PLAN.md to .github/ (#425)

## v0.3

Plugin ecosystem and advanced build features.

- Plugin exporter API — register_exporter_factory/instance (#310, ADR 0004)
- Separate the Exporter / Publisher boundary (#28)
- Split support (train/validation/test, by key)
- Kaggle dataset export
- Snapshot-aware builds (#15)
- Build diff/compare tools (#16)
- Reusable build templates (#14)

## v0.2

Export expansion, Dataset Identity, CLI build execution.

- Markdown / JSONL / Parquet / HuggingFace layout exporter
- stage-aware exporters (Gold-based)
- Publish command (#10)
- Promote the manifest to a dataset release record (#7)
- Schema summary in manifest (#11)
- Provenance tracking (#12)
- Dataset card generation
- Build / Validate / Preview CLI command (#1-4)
- Polars-based tabular engine
- Seoul apartment transaction price end-to-end example

## v0.1

Medallion pipeline foundation.

- Stabilise the BuildSpec contract (YAML parsing, validation)
- Medallion directory structure (stages/bronze, stages/silver, stages/gold)
- Bronze/Silver/Gold stage implementation
- Pipeline orchestrator
- BuildError error hierarchy
- Stabilise the manifest schema
