"""Mask declared PII columns in Gold, the published table, by default (#689).

Which columns hold personal data is declared, never guessed: kpubdata's dataset spec
lists them in ``license.pii_columns`` (kpubdata#525), and a BuildSpec may add its own
in ``sources[].gold.pii_columns``. Every declared column that reaches Gold is masked
so that no original value is published, and its dtype is kept (#902): a text column
has each non-null value replaced by :data:`PII_MASK_TOKEN`, keeping the null pattern;
any other column (a number, a date, a list) becomes all null, because no token fits
its dtype. The manifest names which of the two each column got (``masked_as``).
Masking rather than dropping, in the column's own dtype, keeps the Gold schema equal
to Silver's whatever a spec declares, and a reader sees the column was withheld
instead of wondering whether it existed.

Silver keeps every value (#611); quality is measured there. Gold is what exports,
the dataset card, publishing, Gold ``/query`` and the warehouse read, so masking it
once covers all of them.

Publishing a declared column unmasked takes an explicit ``sources[].gold.
publish_unmasked`` entry, and every such column is recorded as a warning in the
manifest.

How this relates to the BuildSpec ``pii`` scan gate (#441, #902). The gate looks at
Silver for values and names that look like PII, before Gold is built. A declared
column Gold will mask is already handled, so the gate does not count it. A column in
``publish_unmasked`` is published as is, so the gate still counts it: under ``mode:
block`` it must also be listed in ``pii.allow_columns``. The two switches say
different things and neither implies the other. ``allow_columns`` accepts a column's
plain values as publishable where nothing declared it (the gate, warehouse profiles
and exports read it that way); it never unmasks a declared column. ``publish_unmasked``
opts a declared column out of masking; it never silences the gate.

A kpubdata declaration naming a field this source does not carry is not an error,
but it is recorded in the manifest (``declared_absent``) so a spelling that no longer
matches the source cannot let the real column through unnoticed (#902).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ...spec import JsonValue
from ...spec.models import SchemaContract
from ...tabular.duckdb_load import TableHandle, storage_type
from ...tabular.duckdb_runtime import ROW_SEQ_COLUMN, TabularRelation
from ...tabular.sql import quote_identifier, quote_literal

#: What a masked cell holds. Not a value any PII pattern matches.
PII_MASK_TOKEN = "[masked]"

#: Where a column's PII declaration came from.
DECLARED_BY_KPUBDATA = "kpubdata_spec"
DECLARED_BY_BUILD_SPEC = "build_spec"


class PiiDeclarationError(ValueError):
    """A BuildSpec PII declaration names a column Silver does not have."""


#: How a masked column was masked (#902): text cells hold the token, other dtypes null.
MASKED_AS_TOKEN = "token"
MASKED_AS_NULL = "null"


@dataclass(frozen=True)
class PiiMaskResult:
    """Which declared columns Gold masked, and which it published unmasked, and why.

    Each column maps to the sources of its declaration (``kpubdata_spec``,
    ``build_spec``). ``nulled`` names the masked columns that became null because
    their dtype is not text (#902). ``declared_absent`` names the kpubdata-declared
    fields this source does not carry (#902). Never holds a value.
    """

    masked: Mapping[str, tuple[str, ...]]
    unmasked: Mapping[str, tuple[str, ...]]
    nulled: frozenset[str] = frozenset()
    declared_absent: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.masked and not self.unmasked and not self.declared_absent

    def body(self) -> dict[str, JsonValue]:
        def entry(name: str, origins: tuple[str, ...]) -> dict[str, JsonValue]:
            return {"column": name, "declared_by": list(origins)}

        return {
            "token": PII_MASK_TOKEN,
            "masked": [
                {
                    **entry(name, origins),
                    "masked_as": MASKED_AS_NULL if name in self.nulled else MASKED_AS_TOKEN,
                }
                for name, origins in sorted(self.masked.items())
            ],
            "unmasked": [entry(name, o) for name, o in sorted(self.unmasked.items())],
            "declared_absent": list(self.declared_absent),
        }


def core_pii_columns(dataset: object) -> tuple[str, ...]:
    """The ``pii_columns`` a kpubdata dataset's spec declares, or none.

    Read through the public ``Dataset.ref.license`` (kpubdata 0.8). A dataset without a
    licence block, or an object that is not a kpubdata dataset, declares nothing.
    """
    ref = getattr(dataset, "ref", None)
    license_spec = getattr(ref, "license", None)
    columns = getattr(license_spec, "pii_columns", None) or ()
    return tuple(c for c in columns if isinstance(c, str) and c)


def builder_column_names(names: Iterable[str], contract: SchemaContract | None) -> set[str]:
    """Source field names as Silver names them: coalesced, then renamed.

    A field folded into a canonical column by ``schema.coalesce`` makes that canonical
    column hold its values, so the canonical column is what carries the declaration.
    """
    if contract is None:
        return set(names)
    canonical_of = {
        candidate: canonical
        for canonical, candidates in contract.coalesce.items()
        for candidate in candidates
    }
    out: set[str] = set()
    for name in names:
        canonical = canonical_of.get(name, name)
        out.add(contract.rename.get(canonical, canonical))
    return out


def declared_pii_columns(
    *,
    core: Iterable[str],
    build_spec: Sequence[str],
    silver_columns: Sequence[str],
    contract: SchemaContract | None,
) -> dict[str, tuple[str, ...]]:
    """Silver columns declared PII, each with where its declaration came from.

    A kpubdata declaration naming a field this source does not carry is skipped here:
    the spec describes the dataset, not one response. :func:`absent_core_pii_columns`
    names those fields so the manifest records them (#902). A BuildSpec declaration naming a
    column Silver lacks fails, as ``gold.select`` does — a typo must not let the real
    column through unmasked.
    """
    missing = [c for c in build_spec if c not in silver_columns]
    if missing:
        raise PiiDeclarationError(f"gold pii_columns names columns Silver lacks: {missing}")
    origins: dict[str, list[str]] = {}
    for name in sorted(builder_column_names(core, contract)):
        if name in silver_columns:
            origins.setdefault(name, []).append(DECLARED_BY_KPUBDATA)
    for name in build_spec:
        origins.setdefault(name, []).append(DECLARED_BY_BUILD_SPEC)
    return {name: tuple(values) for name, values in origins.items()}


def absent_core_pii_columns(
    core: Iterable[str], *, silver_columns: Sequence[str], contract: SchemaContract | None
) -> tuple[str, ...]:
    """kpubdata-declared fields that name no Silver column, as kpubdata spells them (#902).

    :func:`declared_pii_columns` skips them; the manifest records them instead.
    """
    present = set(silver_columns)
    return tuple(
        sorted({name for name in core if not builder_column_names((name,), contract) & present})
    )


def columns_masked_in_gold(
    declared: Mapping[str, tuple[str, ...]], publish_unmasked: Sequence[str]
) -> frozenset[str]:
    """Declared columns Gold will not publish as is: masked, or dropped by ``select``.

    The Silver PII scan gate treats these as handled (#902). A ``publish_unmasked``
    column is published as is, so it is not among them.
    """
    return frozenset(declared) - frozenset(publish_unmasked)


_names = itertools.count()


def nulled_columns(table: TableHandle, columns: Iterable[str]) -> frozenset[str]:
    """The ``columns`` of ``table`` that masking turns null because they are not text."""
    loaded = table.table
    return frozenset(
        c for c in columns if c in loaded.names and loaded.nodes[loaded.names.index(c)] != ("str",)
    )


def mask_columns(table: TableHandle, columns: Iterable[str]) -> TableHandle:
    """``table`` with ``columns`` masked, each keeping its dtype (#902).

    A text column has every non-null value replaced by the mask token; any other
    column becomes all null, since the token is not a value of its dtype. The result is
    a new table in the same connection (#870); ``table`` itself is unchanged.
    """
    loaded = table.table
    masked = {c for c in columns if c in loaded.names}
    if not masked:
        return table
    token = quote_literal(PII_MASK_TOKEN)
    parts = [quote_identifier(ROW_SEQ_COLUMN)]
    for index, (name, physical, node) in enumerate(
        zip(loaded.names, loaded.physical, loaded.nodes, strict=True)
    ):
        column = quote_identifier(physical)
        if name not in masked:
            expression = column
        elif node == ("str",):
            expression = f"CASE WHEN {column} IS NULL THEN NULL ELSE {token} END"
        else:
            expression = f"CAST(NULL AS {storage_type(node)})"
        parts.append(f"{expression} AS {quote_identifier(f'c{index}')}")
    return table.derive(
        f"SELECT {', '.join(parts)} FROM {loaded.relation.sql}",
        into=TabularRelation(f"{loaded.relation.name}_masked_{next(_names)}"),
        columns=list(zip(loaded.names, loaded.nodes, strict=True)),
    )


def apply_pii_masking(
    table: TableHandle,
    declared: Mapping[str, tuple[str, ...]],
    *,
    publish_unmasked: Sequence[str] = (),
    declared_absent: Sequence[str] = (),
) -> tuple[TableHandle, PiiMaskResult]:
    """Mask every declared column of ``table`` except those explicitly published unmasked.

    Only columns still in ``table`` (after ``gold.select``) are reported: a column the
    selection dropped is not published at all. ``declared_absent`` is carried into the
    result as is.
    """
    in_gold = {name: origins for name, origins in declared.items() if name in table.columns}
    unmasked = {name: o for name, o in in_gold.items() if name in publish_unmasked}
    masked = {name: o for name, o in in_gold.items() if name not in unmasked}
    result = PiiMaskResult(
        masked=masked,
        unmasked=unmasked,
        nulled=nulled_columns(table, masked),
        declared_absent=tuple(declared_absent),
    )
    return mask_columns(table, masked), result


__all__ = [
    "DECLARED_BY_BUILD_SPEC",
    "DECLARED_BY_KPUBDATA",
    "MASKED_AS_NULL",
    "MASKED_AS_TOKEN",
    "PII_MASK_TOKEN",
    "PiiDeclarationError",
    "PiiMaskResult",
    "absent_core_pii_columns",
    "apply_pii_masking",
    "builder_column_names",
    "columns_masked_in_gold",
    "core_pii_columns",
    "declared_pii_columns",
    "mask_columns",
    "nulled_columns",
]
