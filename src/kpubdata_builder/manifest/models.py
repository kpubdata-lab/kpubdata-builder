"""Build manifest data model (Medallion reorganization: split from old manifest.py).

This module defines only immutable data classes holding audit info like
input/output/warning/error/row counts. Disk recording handled by writer.py.

Key components:
    - BuildManifest: Execution summary data class
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..quality.models import DriftEvaluation, QualityCheckResult, SchemaDriftFinding
from ..spec import JsonValue
from .composition import CompositionProvenance
from .environment import BuildEnvironment
from .provenance import SourceProvenance
from .schema_summary import SchemaSummary

# Version of manifest serialization format. When format becomes incompatible,
# increment major so consumers can safely reject or branch compat layer (#211).
MANIFEST_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class BuildManifest:
    """Execution summary artifact for build audit.

    Attributes:
        build_id: Execution identifier.
        started_at: Execution start time.
        finished_at: Execution end time.
        schema_version: Manifest format version (semver). Default MANIFEST_SCHEMA_VERSION.
        status: Run terminal state (#481, additive). ``"ok"``/``"failed"`` match existing
            derivation from ``errors`` presence; ``"cancelled"`` is graceful
            cancellation. Legacy manifest lacks this field — reader
            if absent, derive from ``errors`` presence as before
            (``manifest.status_from_manifest`` is canonical for that rule).
            cancelled run also doesn't clear ``errors``, so if some source failed then
            cancelled, run preserves failure reasons (doesn't swallow failure with cancellation).
        partial: Run terminated before normal completion, ``outputs`` is partial artifact
            means (#481, additive). Currently True only for run with ``status == "cancelled"``
            — only actual recorded artifacts up to cancellation, unexecuted
            steps not recorded as success. Failed run partiality still recorded as before
            via ``status``/``errors``, not using this flag (don't change
            meaning for existing consumers).
        inputs: List of input files or source identifiers.
        outputs: List of generated artifact paths.
        warnings: List of warning messages.
        errors: List of failure or partial failure messages.
        row_counts: Record count summary by stage or artifact.
        schema_summaries: Schema summary per source (artifact) key. Uses same keys as row_counts.
        provenance: List of detailed source provenance (fetch time/params/record count/checksum).
        build_environment: Execution environment that generated build
            (Python/kpubdata/builder version).
        inputs_fingerprint: Reproducibility fingerprint of all input data
            ("sha256:..."). None if no inputs.
        created_by: Display/legacy label of principal who requested build (#388). Human-readable
            for display; ownership decision uses backward-compat fallback only.
        owner_id: canonical stable persistent owner identity (#505, additive).
            computed via ``service.auth.compute_owner_id()``; raw claim (sub/email
            etc) can't be restored. Legacy manifest lacks this field or is null
            (writer may serialize unset value as null) — reader must treat absent and
            null both as "owner_id unsupported run"; ownership
            decision must fallback to ``created_by``/label comparison (see ``principal_owns()``
            reference).
        quality_results: Structured QualityCheckResult list per source_key (#486, additive).
            Contains only actually evaluated checks including PASS. Legacy manifest lacks this
            field — reader must interpret absence as "unevaluated" (0 items), not "all PASS"
            interpretation.
        schema_drift: Structured SchemaDriftFinding list per source_key (#486, additive).
            Drift itself is deterministic detection result; doesn't affect PASS/WARN/FAIL gate.
            **Don't interpret empty list as "normal"** — can't distinguish from no
            baseline to compare. That distinction is made by
            ``drift_evaluation`` below.
        drift_evaluation: Whether axis (schema/volume) evaluated per source_key (#700, additive).
            While ``schema_drift`` says what changed, this field says **who looked
            at it**. Legacy manifest lacks this field — reader must interpret absence
            as "unknown", not "normal".
        composition: Provenance tracking info of join result of two sources
            via BuildSpec.composition
            (#506, additive). None if no composition or not executed
            — legacy manifest reader must interpret absent or null field
            as "composition unused run".
    """

    build_id: str
    started_at: datetime
    finished_at: datetime
    schema_version: str = MANIFEST_SCHEMA_VERSION
    status: str = "ok"
    partial: bool = False
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)
    schema_summaries: dict[str, SchemaSummary] = field(default_factory=dict)
    provenance: tuple[SourceProvenance, ...] = ()
    build_environment: BuildEnvironment | None = None
    inputs_fingerprint: str | None = None
    created_by: str | None = None
    owner_id: str | None = None
    quality_results: dict[str, tuple[QualityCheckResult, ...]] = field(default_factory=dict)
    schema_drift: dict[str, tuple[SchemaDriftFinding, ...]] = field(default_factory=dict)
    drift_evaluation: dict[str, tuple[DriftEvaluation, ...]] = field(default_factory=dict)
    composition: CompositionProvenance | None = None
    #: Sources whose warehouse commit failed after they built, by reason (#788).
    warehouse_failures: dict[str, dict[str, str]] = field(default_factory=dict)
    #: What each source's ``gold`` selection did (#659): Silver rows in, Gold rows out,
    #: and the rule. ``row_counts`` stays Silver's — the count quality was measured on.
    gold_selection: dict[str, dict[str, JsonValue]] = field(default_factory=dict)
    #: Per Gold directory, the declared PII columns Gold masked and those published
    #: unmasked by ``gold.publish_unmasked``, each with where it was declared (#689).
    pii_masking: dict[str, dict[str, JsonValue]] = field(default_factory=dict)
    #: Present only when a source resumed from a checkpoint (#648): the run is not
    #: reproducible, and the R1 comparison leaves it out.
    reproducibility: dict[str, JsonValue] | None = None
    #: What made ``inputs_fingerprint`` (#867); None when there is no fingerprint.
    inputs_fingerprint_algorithm: str | None = None
    #: What made the ratio splits (#871, ``stages.gold.split``); None without them.
    split_algorithm: str | None = None
    #: Per Gold directory (source key or composition name): ``artifact_digest`` — the
    #: digest of its files' bytes — and ``artifact_writer``, the engine that wrote them
    #: (#867). Logical identity is ``provenance[].data_checksum``; bytes are this.
    artifacts: dict[str, dict[str, JsonValue]] = field(default_factory=dict)


__all__ = ["MANIFEST_SCHEMA_VERSION", "BuildManifest"]
