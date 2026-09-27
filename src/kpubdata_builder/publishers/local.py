"""Local filesystem publisher tool (#28).

Copy generated artifact files to local registry directory for registration.
Simplest publisher that validates Exporter/Publisher boundary without remote
upload.

Key components:
    - LocalPublisher: local directory registration publisher
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

from ..errors import PublishError
from .base import BasePublisher, PublishResult


class LocalPublisher(BasePublisher):
    """Publisher copying/registering artifacts to local registry directory.

    Failure policy:
        - Reject directory artifacts explicitly (PublishError). Directory layout
          publication is separate publisher responsibility, not supported as silent
          copy here.
        - Reject artifacts with different paths but conflicting basenames — choose
          explicit failure to prevent data loss.
        - Wrap copy failures: propagate OSError wrapped as PublishError.
    """

    @property
    def name(self) -> str:
        """Publisher tool identifier."""
        return "local"

    def publish(
        self,
        artifact_paths: tuple[Path, ...],
        *,
        destination: str,
        credentials: Mapping[str, str] | None = None,
    ) -> PublishResult:
        """Copy artifact files to destination directory and return result.

        Parameters:
            artifact_paths: Artifact file paths to copy.
            destination: Target local directory path.

        Returns:
            PublishResult: Publish location and count.

        Raises:
            PublishError: Directory artifact / basename conflict / copy I/O failure.
        """
        # Reject directory: shutil.copy2 cannot handle directories, so pre-check with clear error.
        directories = [p for p in artifact_paths if p.is_dir()]
        if directories:
            offenders = ", ".join(str(p) for p in directories)
            raise PublishError(
                f"directory artifacts are not supported by LocalPublisher: {offenders}"
            )

        # Reject basename collision: in flat copy policy, same name
        # hides one side, so explicit fail.
        names: dict[str, Path] = {}
        for path in artifact_paths:
            existing = names.get(path.name)
            if existing is not None and existing != path:
                raise PublishError(
                    f"duplicate artifact basename {path.name!r}: "
                    f"{existing} and {path} cannot share the destination directory"
                )
            names[path.name] = path

        dest_dir = Path(destination)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for path in artifact_paths:
            try:
                _ = shutil.copy2(path, dest_dir / path.name)
            except OSError as exc:
                raise PublishError(f"failed to copy {path} → {dest_dir}: {exc}") from exc
        return PublishResult(
            publisher=self.name,
            reference=str(dest_dir),
            artifact_count=len(artifact_paths),
        )


__all__ = ["LocalPublisher"]
