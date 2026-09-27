"""Public package surface of kpubdata-builder.

This module re-exposes the core types and exceptions that external users
first encounter in one place.

Key components:
    - ArtifactDataset: Standard artifact representation before export
    - BuildSpec / SourceRef / ExportTarget: Declarative build specification models
    - BuildManifest: Manifest model that records execution results
    - validate_spec: Entry point for build specification validation
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

from .artifact import ArtifactDataset
from .errors import (
    BuildError,
    ExportError,
    ManifestError,
    SpecLoadError,
    ValidationError,
)
from .manifest import BuildManifest, manifest_writer
from .spec import BuildSpec, ExportTarget, SourceRef
from .spec.validator import validate_spec

# The authoritative version is only in pyproject.toml's `version`. Redefining the string here
# causes the two values to diverge — indeed, while CHANGELOG describes v0.4, this constant
# and deployment image tag remained 0.1.0 (#592). From installed package metadata,
# the authoritative version is read to reduce maintenance to one place.
try:
    __version__ = _metadata_version("kpubdata-builder")
except PackageNotFoundError:  # pragma: no cover - only in installed source tree
    __version__ = "0.0.0+unknown"

__all__ = [
    "ArtifactDataset",
    "BuildError",
    "BuildManifest",
    "BuildSpec",
    "ExportError",
    "ManifestError",
    "ExportTarget",
    "SourceRef",
    "SpecLoadError",
    "ValidationError",
    "__version__",
    "manifest_writer",
    "validate_spec",
]
