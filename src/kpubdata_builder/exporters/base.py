"""base exporter contract for artifact output generation.

This module defines common interface exporters must follow and helpers for
preparing output directories.

Main components:
    - ExportResult: generated file metadata
    - ensure_output_dir: prepare safe output file path
    - export_path_parts / export_layout_conflicts: what an export may claim (#1320)
    - BaseExporter: exporter abstract base class
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ..stages._path_safety import path_collision_key, safe_output_path

# Entries the Gold stage and the dataset card write into the directory exports share
# (#1320): ``persist_gold_package`` writes the first three, ``write_card`` the rest.
GOLD_RESERVED_NAMES = ("table.parquet", "package.json", "splits", "README.md", "card.json")
# Files a Kaggle export writes next to its CSV. ``write_card`` copies the card into a
# Kaggle package below the Gold directory, so those land there too.
_KAGGLE_METADATA = "dataset-metadata.json"
_KAGGLE_CARD_FILES = ("README.md", "card.json")
_DRIVE = re.compile(r"^[A-Za-z]:")


def export_path_parts(output_path: str) -> tuple[str, ...]:
    """Split an export ``output_path`` into the segments it names below the Gold directory.

    Both ``/`` and ``\\`` separate segments, since Windows reads either. Empty and ``.``
    segments are dropped (``./data.csv`` is ``data.csv``); what is left must name an
    entry strictly below the Gold directory (#1320).

    Raises:
        ValueError: for an absolute path, a ``..`` segment, a segment no filesystem keeps
            (only dots or spaces), or a path naming the Gold directory itself.
    """
    if output_path.startswith(("/", "\\")) or _DRIVE.match(output_path):
        raise ValueError(f"output_path {output_path!r} must be relative to the Gold directory")
    parts = tuple(part for part in re.split(r"[\\/]", output_path) if part not in ("", "."))
    if not parts:
        raise ValueError(
            f"output_path {output_path!r} names the Gold directory itself, not an entry in it"
        )
    for part in parts:
        if part == "..":
            raise ValueError(f"output_path {output_path!r} must not contain '..'")
        if not path_collision_key(part):
            raise ValueError(f"output_path {output_path!r} has a segment that names no entry")
    return parts


def _overlaps(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Whether one folded path equals the other or is a directory containing it."""
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


def export_layout_conflicts(
    layouts: Sequence[tuple[int, str, tuple[str, ...]]],
) -> list[tuple[int, str, str]]:
    """Find exports that would overwrite a Gold artifact or each other (#1320).

    Each layout is an export's ``(index, kind, parts)``: its position in
    ``BuildSpec.exports``, its kind, and ``parts`` as ``export_path_parts`` returns them.
    Paths are compared in ``path_collision_key`` form, so names that one
    case-insensitive or normalizing filesystem (macOS, Windows) maps to one entry
    collide. An export claims its path and everything below it; one export's path
    must not equal or contain another's. A Kaggle export also writes
    ``dataset-metadata.json`` (and, below the Gold directory, the card files) beside its
    CSV: Kaggle exports may share those, since they merge into them, but no other
    export may claim them.

    Returns:
        ``(index, code, message)`` for every conflicting export, in order. ``code`` is
        ``reserved_export_path`` for a Gold artifact and ``export_path_collision`` for
        another export.
    """
    reserved = {path_collision_key(name): name for name in GOLD_RESERVED_NAMES}
    claims: list[tuple[str, ...]] = []
    shared: list[list[tuple[str, ...]]] = []
    for _index, kind, parts in layouts:
        key = tuple(path_collision_key(part) for part in parts)
        claims.append(key)
        side: list[tuple[str, ...]] = []
        if kind == "kaggle":
            parent = key[:-1]
            side.append((*parent, path_collision_key(_KAGGLE_METADATA)))
            if parent:
                side.extend((*parent, path_collision_key(name)) for name in _KAGGLE_CARD_FILES)
        shared.append(side)

    conflicts: list[tuple[int, str, str]] = []
    for i, (index, _kind, parts) in enumerate(layouts):
        field = f"exports[{index}].output_path {'/'.join(parts)!r}"
        if claims[i][0] in reserved:
            conflicts.append(
                (
                    index,
                    "reserved_export_path",
                    f"{field} would overwrite the Gold artifact {reserved[claims[i][0]]!r}",
                )
            )
            continue
        for j, (other, _other_kind, other_parts) in enumerate(layouts):
            if j < i and _overlaps(claims[i], claims[j]):
                message = f"overlaps exports[{other}].output_path {'/'.join(other_parts)!r}"
            elif any(_overlaps(claims[i], side) for side in shared[j]):
                owner = "itself" if j == i else f"exports[{other}]"
                message = f"collides with a Kaggle metadata or card file written by {owner}"
            else:
                continue
            conflicts.append((index, "export_path_collision", f"{field} {message}"))
            break
    return conflicts


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


__all__ = [
    "GOLD_RESERVED_NAMES",
    "BaseExporter",
    "ExportResult",
    "ensure_output_dir",
    "export_layout_conflicts",
    "export_path_parts",
]
