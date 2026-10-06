"""Build manifest package (Medallion reorganization).

Re-exports public surface of manifest model (models.py) and recorder (writer.py).

Key components:
    - BuildManifest: Execution summary dataclass
    - FieldSummary / SchemaSummary / build_schema_summary: Schema summary (#11)
    - SourceProvenance / build_source_provenance / compute_data_checksum: Detailed provenance (#12)
    - manifest_writer / write_manifest: Disk recording functions
    - status_from_manifest: Single rule for reading run terminal state from recorded manifest (#481)
    - run_status_from_manifest: The outcome a caller is told, a failed table commit included (#1106)
"""

from __future__ import annotations

from .composition import CompositionProvenance, JoinKeyProvenance
from .environment import BuildEnvironment, capture_build_environment
from .models import MANIFEST_SCHEMA_VERSION, BuildManifest
from .provenance import (
    FetchCoverage,
    SourceProvenance,
    SourceReportedTotal,
    build_source_provenance,
    compute_data_checksum,
    compute_inputs_fingerprint,
    snapshot_coverage,
    summarize_reported_totals,
)
from .schema_summary import FieldSummary, SchemaSummary, build_schema_summary
from .status import run_status_from_manifest, status_from_manifest
from .writer import manifest_writer, write_manifest

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "BuildEnvironment",
    "BuildManifest",
    "CompositionProvenance",
    "FetchCoverage",
    "FieldSummary",
    "JoinKeyProvenance",
    "SchemaSummary",
    "SourceProvenance",
    "SourceReportedTotal",
    "build_schema_summary",
    "build_source_provenance",
    "capture_build_environment",
    "compute_data_checksum",
    "compute_inputs_fingerprint",
    "manifest_writer",
    "snapshot_coverage",
    "run_status_from_manifest",
    "status_from_manifest",
    "summarize_reported_totals",
    "write_manifest",
]
