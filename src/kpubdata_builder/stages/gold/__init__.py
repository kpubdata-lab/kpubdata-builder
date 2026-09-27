"""Gold stage package (#47).

Exposes Gold stage implementation that makes Silver refined tables into export-ready
packages (GoldPackage).

Main components:
    - GoldPackage / ExportPlan: output and export plan models
    - build_gold_package: SilverDataset → GoldPackage
    - persist_gold_package: persist Gold output
    - DatasetCard / build_dataset_card / render_dataset_card: dataset card (#37)
"""

from __future__ import annotations

from .build import build_gold_package
from .card import CardField, DatasetCard, build_dataset_card, render_dataset_card
from .compose import CompositionError, CompositionStats, build_composed_gold_package
from .models import ExportPlan, GoldPackage
from .persist import GoldPersistResult, persist_gold_package
from .split import apply_splits, apply_splits_to_frame

__all__ = [
    "CardField",
    "CompositionError",
    "CompositionStats",
    "DatasetCard",
    "ExportPlan",
    "GoldPackage",
    "GoldPersistResult",
    "apply_splits",
    "apply_splits_to_frame",
    "build_composed_gold_package",
    "build_dataset_card",
    "build_gold_package",
    "persist_gold_package",
    "render_dataset_card",
]
