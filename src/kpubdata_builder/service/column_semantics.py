"""What a run's kpubdata sources declare about their columns, for column metadata (#702).

kpubdata's dataset specs declare what each field means (`semantic_kind`, kpubdata ADR
0006) next to its title and format. Builder does not decide which columns are codes;
it reads that declaration here and passes it to the column metadata of `/query`,
`/preview` and the warehouse reads as `core_spec` hints (ADR 0019). A text column
declared `code` is then reported as logical type `identifier` (`wire.mark_identifiers`).

Only `public_api` sources have a kpubdata spec. A file or url source, a dataset with no
spec, and a spec that cannot be read contribute nothing — the metadata is then what it
was before, never a guess. The declaration is read from the installed kpubdata, the one
this Builder was built and tested against.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import lru_cache

from ..spec import JsonValue
from ..spec.models import BuildSpec, SourceRef
from ..stages.bronze.resolve import source_identity
from ..tabular.semantics import ColumnSemantics, from_field_descriptor, with_semantics
from ..tabular.wire import mark_identifiers


@lru_cache(maxsize=256)
def _core_fields(dataset_id: str) -> tuple[object, ...]:
    """The `FieldDescriptor`s kpubdata's spec declares for ``dataset_id``, or none.

    Specs ship inside the kpubdata package and do not change while the process runs,
    so each dataset is read once.
    """
    try:
        from kpubdata import Client

        client = Client()
        try:
            schema = client.dataset(dataset_id).schema()
        finally:
            client.close()
    except Exception:
        # Metadata only: a spec that cannot be read never fails the response it describes.
        return ()
    return tuple(schema.fields) if schema is not None else ()


def output_key(source: SourceRef) -> str:
    """The key a source's outputs are stored and queried under: its alias, or provider.dataset."""
    return source.alias or ".".join(source_identity(source))


def source_semantics(source: SourceRef) -> dict[str, ColumnSemantics]:
    """Per column, what the source's kpubdata spec declares, under Builder's column names.

    A field renamed in the BuildSpec (`schema.rename`) is described under its new
    name. A field folded into another by `schema.coalesce` is left out: the canonical
    column is not any one of its candidates.
    """
    if source.kind != "public_api" or not source.provider or not source.dataset:
        return {}
    contract = source.schema
    rename = contract.rename if contract is not None else {}
    absorbed = (
        {name for names in contract.coalesce.values() for name in names}
        if contract is not None
        else set()
    )
    out: dict[str, ColumnSemantics] = {}
    for field in _core_fields(f"{source.provider}.{source.dataset}"):
        name = getattr(field, "name", None)
        if not isinstance(name, str) or not name or name in absorbed:
            continue
        semantics = from_field_descriptor(field)
        if not semantics.is_empty():
            out[rename.get(name, name)] = semantics
    return out


def spec_semantics(
    spec: BuildSpec | None, source_key: str | None = None
) -> dict[str, ColumnSemantics]:
    """Column semantics of a run's table.

    With ``source_key`` naming one of the spec's sources, that source's declaration;
    otherwise (a composed table, or a key that names no single source) every source's.
    A column two sources describe differently gets nothing: the table cannot say which
    one it came from.
    """
    if spec is None:
        return {}
    sources: Iterable[SourceRef] = spec.sources
    if source_key is not None:
        matched = [s for s in spec.sources if output_key(s) == source_key]
        if matched:
            sources = matched
    merged: dict[str, ColumnSemantics] = {}
    conflicted: set[str] = set()
    for source in sources:
        for name, semantics in source_semantics(source).items():
            if name in conflicted:
                continue
            if name in merged and merged[name] != semantics:
                del merged[name]
                conflicted.add(name)
                continue
            merged[name] = semantics
    return merged


def table_key(spec: BuildSpec | None, logical_name: str) -> str | None:
    """The source key of a warehouse table named ``<dataset_id>.<key>``, or None."""
    if spec is None or not logical_name.startswith(f"{spec.dataset_id}."):
        return None
    return logical_name[len(spec.dataset_id) + 1 :]


def describe_columns(
    meta: Iterable[Mapping[str, JsonValue]],
    semantics: Mapping[str, ColumnSemantics] | None,
) -> list[dict[str, JsonValue]]:
    """Column metadata with each column's semantics, and code columns as identifiers."""
    return with_semantics(mark_identifiers(meta, semantics), semantics)


def describe_json_columns(
    meta: JsonValue, semantics: Mapping[str, ColumnSemantics] | None
) -> JsonValue:
    """`describe_columns` for a value read back from a worker: a list of objects, or as is."""
    if not semantics or not isinstance(meta, list):
        return meta
    entries = [entry for entry in meta if isinstance(entry, dict)]
    if len(entries) != len(meta):
        return meta
    return list(describe_columns(entries, semantics))


__all__ = [
    "describe_columns",
    "describe_json_columns",
    "output_key",
    "source_semantics",
    "spec_semantics",
    "table_key",
]
