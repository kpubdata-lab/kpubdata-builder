"""Build environment metadata (#211).

Records execution environment (Python/kpubdata/builder versions) that generated build in manifest
to aid reproducibility and debugging. If package metadata not found, "unknown" is used.

Key components:
    - BuildEnvironment: Execution environment snapshot
    - capture_build_environment: Create snapshot from current environment
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from importlib import metadata


@dataclass(frozen=True)
class BuildEnvironment:
    """Snapshot of execution environment that generated build.

    Attributes:
        python_version: Python version that ran build (e.g. "3.12.3").
        kpubdata_version: Installed kpubdata version. "unknown" if unavailable.
        builder_version: Installed kpubdata-builder version. "unknown" if unavailable.
    """

    python_version: str
    kpubdata_version: str
    builder_version: str


def _package_version(name: str) -> str:
    """Return installed package version, or "unknown" if not found."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def capture_build_environment() -> BuildEnvironment:
    """Create BuildEnvironment snapshot of current execution environment."""
    return BuildEnvironment(
        python_version=platform.python_version(),
        kpubdata_version=_package_version("kpubdata"),
        builder_version=_package_version("kpubdata-builder"),
    )


__all__ = ["BuildEnvironment", "capture_build_environment"]
