"""Provider credential domain model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialMetadata:
    """Storage metadata that does not include plaintext credential."""

    provider: str
    configured: bool
    masked: str | None
    updated_at: str | None


__all__ = ["CredentialMetadata"]
