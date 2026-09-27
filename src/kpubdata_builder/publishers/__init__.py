"""Artifact publisher tool registry (#28).

Enable finding publishers by kind string that register/upload files created by
Exporter to external/local destination. Remote targets like HuggingFace/GitHub
added in follow-up issues.

Key components:
    - PublishResult: publish result metadata
    - BasePublisher: abstract base class for publishers
    - LocalPublisher: local registry registration publisher
    - PUBLISHER_REGISTRY: name -> publisher instance mapping
"""

from __future__ import annotations

from .base import BasePublisher, PublishResult
from .huggingface import HuggingFacePublisher
from .kaggle import KagglePublisher
from .local import LocalPublisher

PUBLISHER_REGISTRY: dict[str, BasePublisher] = {
    "local": LocalPublisher(),
    "huggingface": HuggingFacePublisher(),
    "kaggle": KagglePublisher(),
}

__all__ = [
    "PUBLISHER_REGISTRY",
    "BasePublisher",
    "HuggingFacePublisher",
    "KagglePublisher",
    "LocalPublisher",
    "PublishResult",
]
