"""Error hierarchy for builder pipeline failures.

Defines exception types for distinguishing failures at key pipeline stages
(BuildSpec load, validation, export, manifest writing).

Main classes:
    - BuildError: Common base for all builder exceptions
    - ValidationError: Structured exception holding multiple validation issues
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .spec.validator import ValidationProblem


class BuildError(Exception):
    """Base exception for all builder errors.

    Note:
        Subexceptions inherit from this class, so callers can handle all
        builder-layer errors by catching BuildError alone.
    """


class SpecLoadError(BuildError):
    """Indicates failure to load or parse build specification."""


class ValidationError(BuildError):
    """Indicates build specification validation failure.

    Attributes:
        problems: List of individual error messages collected during validation.
        structured_problems: List of structured problem objects (#417). None if legacy.
    """

    def __init__(
        self,
        problems: list[str],
        *,
        structured: Sequence[ValidationProblem] | None = None,
    ) -> None:
        self.problems = problems
        self.structured_problems = structured
        super().__init__(f"Validation failed: {'; '.join(problems)}")


class ExportError(BuildError):
    """Indicates failure to export files or prepare output directory."""


class PathTraversalError(ExportError):
    """Indicates output path exceeds allowed base directory (#210).

    Raised when user-controlled output_path contains absolute path or ``..`` traversal,
    blocking attempt to write files to unintended location. Inherits from ExportError
    so handled by existing ``except ExportError`` paths.
    """


class ManifestError(BuildError):
    """Indicates failure to serialize manifest or write to disk."""


class PublishError(BuildError):
    """Indicates failure to publish artifacts (copy/upload/register)."""


class TabularError(BuildError):
    """Indicates data integrity issue in raw record table conversion/normalization.

    Raised for heterogeneous (mixed-type) columns or lossy conversions where declared
    casting silently nullifies values — cases that should fail explicitly instead of
    silently rewriting data.
    """


class DatasetValidationError(BuildError):
    """Indicates assembled dataset (Silver etc.) failed validation.

    Distinct from spec validation (ValidationError); used when orchestrator marks source
    as failed to prevent validated-failed dataset from flowing to downstream (Gold/packaging).

    Attributes:
        problems: List of individual violation messages collected during dataset validation.
    """

    def __init__(self, problems: list[str], *, structured: list[object] | None = None) -> None:
        self.problems = problems
        self.structured_problems = structured
        super().__init__(f"Dataset validation failed: {'; '.join(problems)}")


__all__ = [
    "BuildError",
    "ExportError",
    "ManifestError",
    "PathTraversalError",
    "PublishError",
    "SpecLoadError",
    "TabularError",
    "ValidationError",
    "DatasetValidationError",
]
