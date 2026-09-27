"""Verify exception hierarchy and ValidationError auxiliary data preservation."""

from __future__ import annotations

from kpubdata_builder import (
    BuildError,
    ExportError,
    ManifestError,
    SpecLoadError,
    ValidationError,
)


def test_all_builder_errors_inherit_from_build_error() -> None:
    # Verify all public exceptions belong to BuildError-based hierarchy.
    assert issubclass(SpecLoadError, BuildError)
    assert issubclass(ValidationError, BuildError)
    assert issubclass(ExportError, BuildError)
    assert issubclass(ManifestError, BuildError)


def test_validation_error_keeps_problem_list() -> None:
    # Verify ValidationError preserves both issue list and string representation.
    error = ValidationError(["problem one", "problem two"])

    assert error.problems == ["problem one", "problem two"]
    assert str(error) == "Validation failed: problem one; problem two"
