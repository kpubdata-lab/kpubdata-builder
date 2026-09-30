"""Base publisher tool contract for remote/local artifact publication (#28).

Exporter / Publisher responsibility boundary:
    - Exporter: **Creates** files or structures (kpubdata_builder.exporters).
    - Publisher: **Upload/register** generated artifacts to external/local destination.

This module defines minimum interface publisher implementations must follow and
PublishResult value object for reporting publish result.

Key components:
    - PublishResult: Publish result metadata
    - BasePublisher: Abstract base class for publishers
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PublishResult:
    """Publication result metadata.

    Attributes:
        publisher: Publisher identifier that performed publication.
        reference: Publication location reference (local path, URL, registry ID etc).
        artifact_count: Number of published artifacts.
        status: Publish status ("ok" etc).
    """

    publisher: str
    reference: str
    artifact_count: int
    status: str = "ok"


class BasePublisher(ABC):
    """Abstract interface for publication backend.

    Implementations must provide name identifier and publish method. publish does not
    create files (Exporter's responsibility), receives already-created artifact paths
    registers/uploads to destination and returns PublishResult.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return publisher tool identifier."""

    @property
    def expects_directory(self) -> bool:
        """Whether publish expects directory (layout) instead of individual files as input.

        Default is ``False``, caller passes individual file paths. Like Kaggle
        Publishers requiring directory unit with ``dataset-metadata.json``
        override this to ``True`` (#176).
        """
        return False

    def destination_visibility(
        self, destination: str, *, credentials: Mapping[str, str] | None = None
    ) -> str:
        """Whether ``destination`` already exists and is public: ``"public"``,
        ``"private"`` or ``"absent"`` (#688).

        Publishing to an existing destination never changes its visibility, so a
        "private" publish to a public one would be public. A publisher that cannot tell
        raises; the caller treats that as not knowing, which never allows a private-only
        publish.
        """
        raise NotImplementedError(f"{self.name} cannot tell a destination's visibility")

    @abstractmethod
    def publish(
        self,
        artifact_paths: tuple[Path, ...],
        *,
        destination: str,
        credentials: Mapping[str, str] | None = None,
    ) -> PublishResult:
        """Publish generated artifact paths to specified destination.

        Args:
            artifact_paths: Tuple of file paths to publish.
            destination: Publish target identifier (local path, remote repo id etc).
            credentials: Credential to use for this publication (#635). If given, takes
                takes precedence. If ``None``, read environment variable as before — single-user
                deployment behavior unchanged.

                This arg exists because publish credential is server-global not
                per-requester. Then any authenticated user can publish as
                **server owner's account**.

        Returns:
            PublishResult: Publish result metadata.
        """


__all__ = ["BasePublisher", "PublishResult"]
