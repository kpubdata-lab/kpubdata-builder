"""Build manifest package (Medallion reorganization).

Re-exports public surface of manifest model (models.py) and recorder (writer.py).

Key components:
    - BuildManifest: Execution summary dataclass
    - FieldSummary / SchemaSummary / build_schema_summary: Schema summary (#11)
    - SourceProvenance / build_source_provenance / compute_data_checksum: Detailed provenance (#12)
    - manifest_writer / write_manifest: Disk recording functions
    - status_from_manifest: Single rule for reading run terminal state from recorded manifest (#481)
"""

from __future__ import annotations

from .composition import CompositionProvenance
from .environment import BuildEnvironment, capture_build_environment
from .models import MANIFEST_SCHEMA_VERSION, BuildManifest
from .provenance import (
    SourceProvenance,
    build_source_provenance,
    compute_data_checksum,
    compute_inputs_fingerprint,
)
from .schema_summary import FieldSummary, SchemaSummary, build_schema_summary
from .status import status_from_manifest
from .writer import manifest_writer, write_manifest

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "BuildEnvironment",
    "BuildManifest",
    "CompositionProvenance",
    "FieldSummary",
    "SchemaSummary",
    "SourceProvenance",
    "build_schema_summary",
    "build_source_provenance",
    "capture_build_environment",
    "compute_data_checksum",
    "compute_inputs_fingerprint",
    "manifest_writer",
    "status_from_manifest",
    "write_manifest",
]
