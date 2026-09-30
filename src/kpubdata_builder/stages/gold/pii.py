"""Mask declared PII columns in Gold, the published table, by default (#689).

Which columns hold personal data is declared, never guessed: kpubdata's dataset spec
lists them in ``license.pii_columns`` (kpubdata#525), and a BuildSpec may add its own
in ``sources[].gold.pii_columns``. Every declared column that reaches Gold has each
non-null value replaced by :data:`PII_MASK_TOKEN`, so the row shape and the null
pattern are kept while no original value is published. Masking rather than dropping
keeps the published schema the same whatever a spec declares, and a reader sees the
column was withheld instead of wondering whether it existed.

Silver keeps every value (#611); quality is measured there. Gold is what exports,
the dataset card, publishing, Gold ``/query`` and the warehouse read, so masking it
once covers all of them.

Publishing a declared column unmasked takes an explicit ``sources[].gold.
publish_unmasked`` entry, and every such column is recorded as a warning in the
manifest.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import polars as pl

from ...spec import JsonValue
from ...spec.models import SchemaContract

#: What a masked cell holds. Not a value any PII pattern matches.
PII_MASK_TOKEN = "[masked]"

#: Where a column's PII declaration came from.
DECLARED_BY_KPUBDATA = "kpubdata_spec"
DECLARED_BY_BUILD_SPEC = "build_spec"


class PiiDeclarationError(ValueError):
    """A BuildSpec PII declaration names a column Silver does not have."""


@dataclass(frozen=True)
class PiiMaskResult:
    """Which declared columns Gold masked, and which it published unmasked, and why.

    Each column maps to the sources of its declaration (``kpubdata_spec``,
    ``build_spec``). Never holds a value.
    """

    masked: Mapping[str, tuple[str, ...]]
    unmasked: Mapping[str, tuple[str, ...]]

    def is_empty(self) -> bool:
        return not self.masked and not self.unmasked

    def body(self) -> dict[str, JsonValue]:
        def entries(columns: Mapping[str, tuple[str, ...]]) -> list[JsonValue]:
            return [
                {"column": name, "declared_by": list(origins)}
                for name, origins in sorted(columns.items())
            ]

        return {
            "token": PII_MASK_TOKEN,
            "masked": entries(self.masked),
            "unmasked": entries(self.unmasked),
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

    A kpubdata declaration naming a field this source does not carry is skipped: the
    spec describes the dataset, not one response. A BuildSpec declaration naming a
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


def mask_columns(frame: pl.DataFrame, columns: Iterable[str]) -> pl.DataFrame:
    """``frame`` with every non-null value of ``columns`` replaced by the mask token."""
    present = [c for c in columns if c in frame.columns]
    if not present:
        return frame
    return frame.with_columns(
        [
            pl.when(pl.col(c).is_null()).then(None).otherwise(pl.lit(PII_MASK_TOKEN)).alias(c)
            for c in present
        ]
    )


def apply_pii_masking(
    frame: pl.DataFrame,
    declared: Mapping[str, tuple[str, ...]],
    *,
    publish_unmasked: Sequence[str] = (),
) -> tuple[pl.DataFrame, PiiMaskResult]:
    """Mask every declared column of ``frame`` except those explicitly published unmasked.

    Only columns still in ``frame`` (after ``gold.select``) are reported: a column the
    selection dropped is not published at all.
    """
    in_gold = {name: origins for name, origins in declared.items() if name in frame.columns}
    unmasked = {name: o for name, o in in_gold.items() if name in publish_unmasked}
    masked = {name: o for name, o in in_gold.items() if name not in unmasked}
    return mask_columns(frame, masked), PiiMaskResult(masked=masked, unmasked=unmasked)


__all__ = [
    "DECLARED_BY_BUILD_SPEC",
    "DECLARED_BY_KPUBDATA",
    "PII_MASK_TOKEN",
    "PiiDeclarationError",
    "PiiMaskResult",
    "apply_pii_masking",
    "builder_column_names",
    "core_pii_columns",
    "declared_pii_columns",
    "mask_columns",
]
