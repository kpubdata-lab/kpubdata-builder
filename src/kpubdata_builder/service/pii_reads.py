"""Declared PII stays masked on every read of Silver and Bronze (#900).

Gold masks the columns kpubdata (``license.pii_columns``) or the BuildSpec
(``sources[].gold.pii_columns``) declare personal (#689, ``stages/gold/pii.py``). Silver
and Bronze keep the original values (#611), and the service reads them on four paths.
Each shows the same columns masked the same way Gold does — text becomes the mask
token, any other dtype null — or refuses:

- ``POST /query`` with ``stage: silver`` runs the query on a masked copy of the Silver
  table, so no SQL expression over a declared column (``upper``, ``substr``, a
  ``WHERE`` equality) can see an original value.
- ``POST /preview`` masks each source's ``sample``, ``source_sample`` (raw field names,
  mapped through the source's ``schema.coalesce`` and ``schema.rename``) and the
  ``diffs`` of a declared column.
- ``GET /builds/{run_id}/stages/silver/{source}`` masks the stage ``sample``. The Bronze
  stage detail carries no rows.
- ``GET /artifacts/{run_id}/{path}`` refuses (403 ``declared_pii_withheld``) every file
  under ``bronze/{source}/`` or ``silver/{source}/`` of a source with a withheld
  column. Those are raw records and Parquet; rewriting them in flight would mean
  serving a file that is not the artifact, so they do not leave. Gold holds the
  published, masked table.

Which columns are withheld is resolved by :func:`~kpubdata_builder.stages.gold.pii.
columns_withheld_from_silver`, the declaration Gold masks less ``gold.publish_unmasked``:
an opted-out column is plain text on these reads exactly as it is in Gold. The columns
the run's manifest recorded as masked are withheld too, so a declaration the catalog
later drops does not unmask a run built under it.

When a public_api source's kpubdata declaration cannot be read at all, the read fails
closed: ``/query``, ``/preview`` and downloads answer 503
``pii_declaration_unavailable`` and a stage detail withholds its sample
(``sample_withheld``). The manifest record is not a substitute — it names only what
Gold masked, not a column ``gold.select`` dropped or a run whose Gold never finished.
A file or URL source, or a BuildSpec-only declaration, needs no lookup.

These checks sit at the same choke points as the redistribution gate (#688,
``redistribution.py``) and run after it: a source whose terms forbid redistribution lets
nothing out, so there is nothing left to mask. They stay a separate policy because the
answer differs — the terms refuse a read, a declaration masks it.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import unquote

import polars as pl

from ..pipeline.preview import SourcePreview
from ..spec import BuildSpec, JsonValue, SourceRef
from ..stages._stage_reader import sanitize_source_segment, silver_source_dir
from ..stages.bronze.build import SourceClient
from ..stages.gold.pii import (
    PII_MASK_TOKEN,
    builder_column_names,
    columns_withheld_from_silver,
    core_pii_columns,
    is_text_dtype_name,
    mask_records,
    mask_value,
    source_fields_of,
)
from ..tabular import PreviewSlice
from ..tabular.builder_parquet import read_builder_parquet_schema, scan_builder_parquet
from . import datasets as datasets_service
from . import stages as stages_service
from .responses import ServiceResponse

logger = logging.getLogger(__name__)

#: ``"provider.dataset"`` → the ``pii_columns`` its kpubdata spec declares.
PiiLookup = Callable[[str], Sequence[str]]

#: The error code a refused Silver or Bronze download answers with.
DECLARED_PII_WITHHELD = "declared_pii_withheld"

#: The error code a read answers with when a source's declaration cannot be read.
PII_DECLARATION_UNAVAILABLE = "pii_declaration_unavailable"

_RAW_STAGES = ("bronze", "silver")


class PiiDeclarationUnavailable(Exception):
    """A public_api source's kpubdata PII declaration could not be read (#900).

    Every Silver or Bronze read of that source is then refused, not served with a
    partial mask: which of its columns are personal is not known.
    """

    def __init__(self, dataset_id: str) -> None:
        super().__init__(dataset_id)
        self.dataset_id = dataset_id


def unavailable_response(exc: PiiDeclarationUnavailable, *, what: str) -> ServiceResponse:
    """503: the declaration could not be read, so ``what`` does not leave (#900)."""
    return ServiceResponse(
        503,
        {
            "error": (
                f"the PII declaration of {exc.dataset_id} could not be read, so {what} "
                "cannot leave Builder unmasked; try again, or read Gold"
            ),
            "code": PII_DECLARATION_UNAVAILABLE,
            "dataset": exc.dataset_id,
        },
    )


def client_pii_lookup(
    create_client: Callable[[], SourceClient], close_client: Callable[[SourceClient], None]
) -> PiiLookup:
    """A lookup that reads the declaration the way the build does: through a client.

    The build reads ``client.dataset(...).ref.license.pii_columns`` with the client it
    fetched with (#689); a read uses a client from the same factory, so both see the
    same declaration. Cached per dataset: it comes with the installed kpubdata.
    """
    cache: dict[str, tuple[str, ...]] = {}

    def lookup(dataset_id: str) -> tuple[str, ...]:
        if dataset_id not in cache:
            client = create_client()
            try:
                cache[dataset_id] = core_pii_columns(client.dataset(dataset_id))
            finally:
                close_client(client)
        return cache[dataset_id]

    return lookup


def _core_columns(source: SourceRef, lookup: PiiLookup) -> tuple[str, ...]:
    if source.kind != "public_api":
        return ()
    dataset_id = f"{source.provider}.{source.dataset}"
    try:
        return tuple(lookup(dataset_id))
    except Exception as exc:
        # Not knowing which columns are personal is not permission to show them (#688):
        # the manifest only records what Gold masked, not a column Gold dropped or a
        # run whose Gold never finished, so it cannot stand in for the declaration.
        logger.warning("PII declaration unreadable for %s; refusing the read", dataset_id)
        raise PiiDeclarationUnavailable(dataset_id) from exc


def source_withheld_columns(
    source: SourceRef, lookup: PiiLookup, *, silver_columns: Sequence[str] | None
) -> frozenset[str]:
    """Declared columns of ``source`` that a Silver or Bronze read does not show as is."""
    gold = source.gold
    return columns_withheld_from_silver(
        core=_core_columns(source, lookup),
        build_spec=gold.pii_columns if gold is not None else (),
        publish_unmasked=gold.publish_unmasked if gold is not None else (),
        silver_columns=silver_columns,
        contract=source.schema,
    )


def _recorded_masked(manifest: Mapping[str, object] | None, source_key: str) -> set[str]:
    """Columns the run's manifest says Gold masked for ``source_key`` (#689)."""
    record = manifest.get("pii_masking") if manifest is not None else None
    entry = record.get(source_key) if isinstance(record, dict) else None
    masked = entry.get("masked") if isinstance(entry, dict) else None
    if not isinstance(masked, list):
        return set()
    return {m["column"] for m in masked if isinstance(m, dict) and isinstance(m.get("column"), str)}


def run_withheld_columns(
    output_root: Path,
    run_id: str,
    source_key: str,
    lookup: PiiLookup,
    *,
    silver_columns: Sequence[str] | None,
    spec: BuildSpec | None = None,
) -> frozenset[str]:
    """Withheld columns of one source of a run: its declaration and its manifest record."""
    if spec is None:
        spec = datasets_service.read_snapshot_spec(output_root, run_id)
    source = stages_service.match_source_ref(spec, source_key) if spec is not None else None
    withheld = (
        set(source_withheld_columns(source, lookup, silver_columns=silver_columns))
        if source is not None
        else set()
    )
    withheld |= _recorded_masked(datasets_service.read_manifest(output_root, run_id), source_key)
    return frozenset(withheld)


# ------------------------------------------------------------------ stage sample


def mask_stage_sample(
    output_root: Path, run_id: str, source_key: str, body: dict[str, JsonValue], lookup: PiiLookup
) -> dict[str, JsonValue]:
    """A Silver stage detail with its ``sample`` masked; ``masked_columns`` names them."""
    schema = body.get("schema")
    columns = schema if isinstance(schema, list) else []
    names = [
        name for c in columns if isinstance(c, dict) and isinstance(name := c.get("name"), str)
    ]
    try:
        withheld = run_withheld_columns(
            output_root, run_id, source_key, lookup, silver_columns=names
        )
    except PiiDeclarationUnavailable:
        # The stage's metadata stays; its sample rows do not leave (as #688 withholds).
        return {**body, "sample": [], "sample_withheld": PII_DECLARATION_UNAVAILABLE}
    sample = body.get("sample")
    rows = [r for r in sample if isinstance(r, dict)] if isinstance(sample, list) else []
    present = sorted(c for c in withheld if c in names or any(c in r for r in rows))
    if not present:
        return body
    text = {
        str(c["name"])
        for c in columns
        if isinstance(c, dict) and is_text_dtype_name(_str_or_none(c.get("dtype")))
    }
    return {
        **body,
        "sample": list(mask_records(rows, present, text_columns=text)),
        "masked_columns": list(present),
    }


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


# ------------------------------------------------------------------ artifact file


def artifact_refusal(
    output_root: Path, run_id: str, file_path: str, lookup: PiiLookup
) -> ServiceResponse | None:
    """403 for a Bronze or Silver file of a source with a withheld column; None otherwise.

    The file is identified by its path under the run, as the artifact list names it
    (``silver/{source}/table.parquet``, ``bronze/{source}/{artifact}/raw_records.jsonl``).
    A stage directory that matches no known source is judged against every source of
    the run: not knowing which source it holds is not permission.
    """
    segments = unquote(file_path).replace("\\", "/").split("/")
    if len(segments) < 2 or segments[0] not in _RAW_STAGES:
        return None
    manifest = datasets_service.read_manifest(output_root, run_id)
    known = stages_service.known_source_keys(manifest) if manifest is not None else []
    matching = [k for k in known if sanitize_source_segment(k) == segments[1]]
    spec = datasets_service.read_snapshot_spec(output_root, run_id)
    withheld: set[str] = set()
    for key in matching or known:
        try:
            withheld |= run_withheld_columns(
                output_root,
                run_id,
                key,
                lookup,
                silver_columns=_silver_columns(output_root, run_id, key),
                spec=spec,
            )
        except PiiDeclarationUnavailable as exc:
            return unavailable_response(exc, what=f"{segments[0]} files")
    if not withheld:
        return None
    return ServiceResponse(
        403,
        {
            "error": (
                f"{segments[0]} files hold the declared PII columns "
                f"{', '.join(sorted(withheld))} in plain text, so they cannot be "
                "downloaded; Gold holds the table with them masked"
            ),
            "code": DECLARED_PII_WITHHELD,
            "columns": list[JsonValue](sorted(withheld)),
        },
    )


def _silver_columns(output_root: Path, run_id: str, source_key: str) -> list[str] | None:
    """The Silver table's columns, or None when there is no readable Silver table."""
    try:
        table = silver_source_dir(output_root, run_id, source_key) / "table.parquet"
        if not table.is_file() or table.is_symlink():
            return None
        return list(read_builder_parquet_schema(table))
    except (OSError, ValueError, pl.exceptions.PolarsError):
        return None


# ------------------------------------------------------------------ /query on Silver


@dataclass
class MaskedTable:
    """A masked copy of a Silver table, removed when the query is done."""

    path: Path
    columns: tuple[str, ...]
    _directory: tempfile.TemporaryDirectory[str]

    def close(self) -> None:
        self._directory.cleanup()


def masked_silver_table(table_path: Path, withheld: frozenset[str]) -> MaskedTable | None:
    """A copy of ``table_path`` with the withheld columns masked; None when none are there.

    The table is read as the query engine reads it (:func:`scan_builder_parquet`), so
    the Builder dtypes and real column names the DuckDB writer recorded in the file's
    metadata (#891) are given back before masking; Polars then writes those dtypes
    as they are (a Null column, a Duration, an Int128, a zoned datetime). A query of
    the copy reports the same ``column_meta`` as a query of the original.
    """
    frame = scan_builder_parquet(table_path)
    schema = frame.collect_schema()
    present = tuple(sorted(c for c in withheld if c in schema))
    if not present:
        return None
    directory = tempfile.TemporaryDirectory(prefix="kpubdata-pii-")
    path = Path(directory.name) / "table.parquet"
    try:
        frame.with_columns(_mask_expressions(schema, present)).sink_parquet(path)
    except BaseException:
        directory.cleanup()
        raise
    return MaskedTable(path=path, columns=present, _directory=directory)


def _mask_expressions(schema: pl.Schema, columns: Sequence[str]) -> list[pl.Expr]:
    """Gold's masking (``stages/gold/pii.mask_columns``) as Polars expressions.

    Text keeps the null pattern and gets the token; any other dtype becomes null of
    its own dtype (#902).
    """
    return [
        (
            pl.when(pl.col(c).is_null()).then(None).otherwise(pl.lit(PII_MASK_TOKEN))
            if schema[c] == pl.String
            else pl.lit(None, dtype=schema[c])
        ).alias(c)
        for c in columns
    ]


# ------------------------------------------------------------------ /preview


def mask_source_preview(
    preview: SourcePreview, source: SourceRef, core: Sequence[str]
) -> tuple[SourcePreview, tuple[str, ...]]:
    """``preview`` with the withheld columns masked in every sample and diff.

    ``core`` is what the preview's own client read from the dataset's spec, as the
    build reads it. Returns the masked Silver column names.
    """
    silver_columns = [c.name for c in preview.schema.columns]
    gold = source.gold
    withheld = columns_withheld_from_silver(
        core=core,
        build_spec=gold.pii_columns if gold is not None else (),
        publish_unmasked=gold.publish_unmasked if gold is not None else (),
        silver_columns=silver_columns,
        contract=source.schema,
    )
    present = tuple(sorted(c for c in withheld if c in silver_columns))
    if not present:
        return preview, ()
    text = {c.name for c in preview.schema.columns if is_text_dtype_name(c.dtype)}
    raw_fields = {key for row in preview.source_sample for key in row}
    raw_masked = source_fields_of(raw_fields, present, source.schema)
    # A raw field is text when the Silver column it becomes is.
    raw_text = {f for f in raw_masked if builder_column_names((f,), source.schema) & text}
    masked = replace(
        preview,
        preview=PreviewSlice(
            rows=tuple(mask_records(preview.preview.rows, present, text_columns=text)),
            total_rows=preview.preview.total_rows,
        ),
        source_sample=tuple(mask_records(preview.source_sample, raw_masked, text_columns=raw_text)),
        diffs=tuple(
            replace(
                d,
                before=mask_value(d.before, text=d.column in text),
                after=mask_value(d.after, text=d.column in text),
            )
            if d.column in present
            else d
            for d in preview.diffs
        ),
    )
    return masked, present


__all__ = [
    "DECLARED_PII_WITHHELD",
    "PII_DECLARATION_UNAVAILABLE",
    "PiiDeclarationUnavailable",
    "MaskedTable",
    "PiiLookup",
    "artifact_refusal",
    "client_pii_lookup",
    "mask_source_preview",
    "mask_stage_sample",
    "masked_silver_table",
    "run_withheld_columns",
    "source_withheld_columns",
    "unavailable_response",
]
