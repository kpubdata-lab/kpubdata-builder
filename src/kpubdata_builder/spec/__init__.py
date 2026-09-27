"""BuildSpec model/loader package (Medallion refactor)."""

from __future__ import annotations

from .loader import load_spec, parse_spec
from .models import (
    BuildSpec,
    ColumnNullTokens,
    CompositionSpec,
    DerivedColumn,
    ExportTarget,
    JoinSpec,
    JsonPrimitive,
    JsonValue,
    SchemaContract,
    SourceRef,
    SplitSpec,
)
from .param_grid import expand_param_grid
from .serializer import (
    BUILDSPEC_SNAPSHOT_FILENAME,
    canonical_spec_mapping,
    compute_spec_digest,
    serialize_spec,
    serialize_spec_bytes,
    write_buildspec_snapshot,
)
from .template import load_template, render_template

__all__ = [
    "BuildSpec",
    "BUILDSPEC_SNAPSHOT_FILENAME",
    "CompositionSpec",
    "DerivedColumn",
    "expand_param_grid",
    "ExportTarget",
    "JoinSpec",
    "JsonPrimitive",
    "JsonValue",
    "ColumnNullTokens",
    "SchemaContract",
    "SourceRef",
    "SplitSpec",
    "canonical_spec_mapping",
    "compute_spec_digest",
    "load_spec",
    "load_template",
    "parse_spec",
    "render_template",
    "serialize_spec",
    "serialize_spec_bytes",
    "write_buildspec_snapshot",
]
