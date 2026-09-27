"""base exporter contract for artifact output generation.

This module defines common interface exporters must follow and helpers for
preparing output directories.

Main components:
    - ExportResult: generated file metadata
    - ensure_output_dir: prepare safe output file path
    - BaseExporter: exporter abstract base class
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ..stages._path_safety import safe_output_path


@dataclass(frozen=True)
class ExportResult:
    """holds export result metadata.

    Attributes:
        output_path: generated file path.
        file_size: file size in bytes.
        format: exporter identifier.
    """

    output_path: Path
    file_size: int
    format: str


def ensure_output_dir(output_dir: Path, relative_output_path: str) -> Path:
    """ensures parent directory of output file and returns final path.

    Args:
        output_dir: build's base output directory.
        relative_output_path: relative path exporter will write to.

    Returns:
        Path: actual file path to write to.

    Raises:
        ExportError: if directory creation fails.
        PathTraversalError: if relative_output_path escapes output_dir (#210).
    """
    destination = safe_output_path(output_dir, relative_output_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Failed to prepare output directory for {destination}: {exc}") from exc
    return destination


class BaseExporter(ABC):
    """abstract base class for all artifact exporters.

    Implementations must provide name property and export method, and
    correspond 1:1 to BuildSpec's ExportTarget.kind.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """returns exporter identifier used in registry and spec."""

    @abstractmethod
    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """exports artifact to actual file format.

        Args:
            artifact: standard dataset output to export.
            target: output spec with kind, output_path, options.
            output_dir: base directory for all outputs.

        Returns:
            ExportResult: generated file metadata.
        """
        pass


__all__ = ["BaseExporter", "ExportResult", "ensure_output_dir"]
