"""built-in exporter implementations and plugin registry.

Registers built-in exporters to registry at import time and also exposes plugin API
(registry.py) for third-party exporter registration and discovery.

Main components:
    - _EXPORTER_FACTORIES: kind -> factory mapping (ADR 0004)
    - EXPORTER_REGISTRY: kind -> exporter instance mapping (legacy compatibility)
    - register_exporter_factory / register_exporter_instance: registration API
    - get_exporter / load_entry_point_exporters: lookup/discovery API
"""

from __future__ import annotations

from .base import BaseExporter, ExportResult, ensure_output_dir
from .csv import CsvExporter
from .huggingface import HuggingFaceExporter
from .jsonl import JsonlExporter
from .kaggle import KaggleExporter
from .markdown import MarkdownExporter
from .parquet import ParquetExporter
from .registry import (
    EXPORTER_ENTRY_POINT_GROUP,
    EXPORTER_REGISTRY,
    clear_exporter_registry,
    get_exporter,
    load_entry_point_exporters,
    register_exporter,
    register_exporter_factory,
    register_exporter_instance,
    registered_exporter_kinds,
)

# register built-in exporters (ADR 0004 recommendation: factory pattern).
# override=True allows overwriting on re-import (development convenience).
# register to instance registry for backward compatibility (#325).
register_exporter_factory("csv", CsvExporter, override=True)
register_exporter_factory("huggingface", HuggingFaceExporter, override=True)
register_exporter_factory("jsonl", JsonlExporter, override=True)
register_exporter_factory("markdown", MarkdownExporter, override=True)
register_exporter_factory("kaggle", KaggleExporter, override=True)
register_exporter_factory("parquet", ParquetExporter, override=True)

# register instance for legacy code that directly queries EXPORTER_REGISTRY
register_exporter_instance(CsvExporter(), override=True)
register_exporter_instance(HuggingFaceExporter(), override=True)
register_exporter_instance(JsonlExporter(), override=True)
register_exporter_instance(MarkdownExporter(), override=True)
register_exporter_instance(KaggleExporter(), override=True)
register_exporter_instance(ParquetExporter(), override=True)

__all__ = [
    "EXPORTER_ENTRY_POINT_GROUP",
    "EXPORTER_REGISTRY",
    "BaseExporter",
    "CsvExporter",
    "ExportResult",
    "HuggingFaceExporter",
    "JsonlExporter",
    "KaggleExporter",
    "MarkdownExporter",
    "ParquetExporter",
    "clear_exporter_registry",
    "ensure_output_dir",
    "get_exporter",
    "load_entry_point_exporters",
    "register_exporter",
    "register_exporter_factory",
    "register_exporter_instance",
    "registered_exporter_kinds",
]
