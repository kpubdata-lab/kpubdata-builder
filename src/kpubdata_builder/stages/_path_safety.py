"""shared path safety utilities for stage artifact persistence (#46/#47 review).

consolidates path segment validation and workspace containment checks shared by
bronze/silver/gold persist. segment rules changes only need one edit location.

main functions:
    - validate_path_segment: reject segments that could escape workspace
    - ensure_within: verify resolved path is under root
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..errors import PathTraversalError

_SAFE_PATH_SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def validate_path_segment(value: str, *, field_name: str) -> None:
    """Reject path segments that could escape the workspace.

    Args:
        value: path segment to validate.
        field_name: field name for error messages.

    Raises:
        ValueError: if empty or contains disallowed characters.
    """
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading/trailing whitespace")
    if not _SAFE_PATH_SEGMENT.match(value):
        raise ValueError(
            f"{field_name} contains unsafe characters: {value!r}. "
            "Only alphanumeric, dot, hyphen, and underscore are allowed."
        )


def _strip_windows_extended_prefix(path: Path) -> Path:
    """Remove Windows ``\\\\?\\`` extended path prefix for consistent comparison.

    On Windows, ``Path.resolve()`` returns an extended path with ``\\\\?\\``
    prefix if a file handle can be opened to the target. When concurrent
    disk I/O temporarily fails handle opening, it falls back to a prefix-free
    manually normalized path. Although the logical path is identical, different
    fallback outcomes produce different strings. In multi-threaded scenarios
    (#247) writing multiple sources concurrently, ``is_relative_to`` comparisons
    occasionally reported false-positive traversal violations (#506, confirmed
    during composition work). On POSIX, paths never start with this prefix,
    so this is a no-op.
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[len("\\\\?\\UNC\\") :])
    if text.startswith("\\\\?\\"):
        return Path(text[len("\\\\?\\") :])
    return path


def ensure_within(root: Path, target: Path, *, label: str) -> None:
    """Verify that resolved target is contained under resolved root.

    String prefix comparison (`startswith`) can falsely pass `/tmp/root2` as
    under `/tmp/root`, so resolved paths use ``Path.is_relative_to`` for
    accurate containment checks.

    Args:
        root: allowed root directory.
        target: target path to validate.
        label: target description for error messages.

    Raises:
        ValueError: if target escapes root.
    """
    resolved_root = _strip_windows_extended_prefix(root.resolve())
    resolved_target = _strip_windows_extended_prefix(target.resolve())
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(f"Resolved {label} {resolved_target} escapes output_root {resolved_root}")


def safe_output_path(base_dir: Path, relative_path: str | os.PathLike[str]) -> Path:
    """Create and return an output path constrained under base_dir (#210).

    Exporters write files to user-controlled output_path from spec. If
    absolute paths (``/etc/passwd``) or parent traversal (``../../etc``) are
    mixed in, files can be created/overwritten at arbitrary locations outside
    the build workspace. Only return the path after verifying combined and
    resolved path is inside base_dir.

    Args:
        base_dir: base directory that output must remain under.
        relative_path: user-controlled output path relative to base_dir.

    Returns:
        Path: combined path verified to be under base_dir (original form).

    Raises:
        PathTraversalError: if resolved path escapes base_dir.
    """
    candidate = base_dir / relative_path
    resolved_base = _strip_windows_extended_prefix(base_dir.resolve())
    resolved_target = _strip_windows_extended_prefix(candidate.resolve())
    if not resolved_target.is_relative_to(resolved_base):
        raise PathTraversalError(
            f"output path {os.fspath(relative_path)!r} escapes base directory "
            f"{resolved_base} (resolved to {resolved_target})"
        )
    return candidate
