"""The provenance, processing and personal-information facts of a dataset card (#694).

Every fact comes from something recorded — the BuildSpec, the source's provenance entry
(the manifest's), the kpubdata catalog — never from a guess. A fact nothing records is
left empty, and an empty required section blocks publishing
(``stages.gold.card.missing_sections``) rather than going out blank.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from ..catalog_info import DatasetCatalogInfo, catalog_info
from ..manifest.provenance import SourceProvenance
from ..spec import BuildSpec, SourceRef
from ..stages.bronze.resolve import sanitize_endpoint_identity
from ..stages.gold.card import CardSource, DatasetCard, card_sections, render_dataset_card

CatalogLookup = Callable[[str], DatasetCatalogInfo | None]

CARD_JSON = "card.json"
_KAGGLE_METADATA = "dataset-metadata.json"


def card_source(
    source: SourceRef,
    *,
    spec: BuildSpec,
    provenance: SourceProvenance | None,
    label: str,
    lookup: CatalogLookup | None = None,
) -> CardSource:
    """Where one source's data comes from, as far as it is recorded.

    ``lookup`` defaults to the installed kpubdata's catalog, resolved at call time.
    """
    read = lookup if lookup is not None else catalog_info
    info = read(f"{source.provider}.{source.dataset}") if source.kind == "public_api" else None
    institution = (spec.attribution or "").strip() or (info.attribution if info else "") or ""
    if source.kind == "url":
        url = sanitize_endpoint_identity(source.endpoint)
    elif source.kind == "file":
        url = f"uploaded file ({source.upload_id})"
    else:
        url = (info.source_url if info else "") or ""
    return CardSource(
        source=label,
        institution=institution,
        url=url,
        license=_original_licence(spec, info),
        collected_at=_collected_at(provenance),
    )


def _original_licence(spec: BuildSpec, info: DatasetCatalogInfo | None) -> str:
    """The licence under the name it was declared with — never restated as another."""
    declared = (spec.license_name or spec.license or "").strip()
    if spec.license_link and declared:
        declared = f"{declared} ({spec.license_link})"
    provider = (info.license_type or "").strip() if info else ""
    if provider and declared and provider.lower() not in declared.lower():
        return f"{declared}; the provider declares: {provider}"
    return declared or provider


def _collected_at(provenance: SourceProvenance | None) -> str:
    if provenance is None or not provenance.fetched_at:
        return ""
    try:
        moment = datetime.fromisoformat(provenance.fetched_at)
    except ValueError:
        return provenance.fetched_at
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def processing_steps(source: SourceRef, spec: BuildSpec) -> list[str]:
    """Every transformation declared for ``source``, in the order they run."""
    steps: list[str] = []
    schema = source.schema
    if schema is not None:
        if schema.read_as:
            steps.append(f"Read as text: {', '.join(sorted(schema.read_as))}")
        if schema.null_tokens:
            steps.append(f"Treated as missing: {', '.join(repr(t) for t in schema.null_tokens)}")
        for column, tokens in sorted((schema.column_null_tokens or {}).items()):
            steps.append(
                f"Treated as missing in {column}: {', '.join(repr(t) for t in tokens.tokens)}"
            )
        for target, candidates in sorted((schema.coalesce or {}).items()):
            steps.append(f"Merged {', '.join(candidates)} into {target}")
        for old, new in (schema.rename or {}).items():
            steps.append(f"Renamed {old} to {new}")
        for column, width in (schema.zfill or {}).items():
            steps.append(f"Zero-padded {column} to {width} characters")
        for column, dtype in (schema.casts or {}).items():
            steps.append(f"Converted {column} to {dtype}")
        for rule in schema.derived:
            steps.append(f"Derived {rule.name} ({rule.kind}) from {', '.join(rule.columns)}")
    gold = source.gold
    if gold is not None:
        if gold.select:
            steps.append(f"Published columns: {', '.join(gold.select)}")
        if gold.filters:
            steps.append(f"Rows kept by {len(gold.filters)} declared filter(s)")
    if spec.splits is not None:
        steps.append(f"Split into {', '.join(spec.splits.ratios)} ({spec.splits.mode})")
    return steps or ["No transformation declared: values are as the source gave them."]


def personal_information(spec: BuildSpec) -> str:
    """How personal information was handled — stated even when it was not."""
    policy = spec.pii
    if policy is None:
        return "No personal-information policy was declared; values were not scanned."
    allowed = (
        f" Accepted columns: {', '.join(policy.allow_columns)}." if policy.allow_columns else ""
    )
    if policy.mode == "block":
        return "Scanned; a column that looked like personal information failed the build." + allowed
    if policy.mode == "warn":
        return (
            "Scanned; columns that looked like personal information were reported, "
            "not removed." + allowed
        )
    return "Declared as containing no personal information to scan for (allow)." + allowed


def write_card(gold_dir: Path, card: DatasetCard) -> list[Path]:
    """Write ``README.md`` and ``card.json``, and copy both into any Kaggle package in
    ``gold_dir``, which is what a Kaggle publish uploads (#694: every published artifact
    has a card). Returns the paths written."""
    readme = gold_dir / "README.md"
    sections = gold_dir / CARD_JSON
    readme.write_text(render_dataset_card(card), encoding="utf-8")
    sections.write_text(
        json.dumps(card_sections(card), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written = [readme, sections]
    for metadata in sorted(gold_dir.rglob(_KAGGLE_METADATA)):
        package = metadata.parent
        if package == gold_dir:
            continue
        for path in (readme, sections):
            copy = package / path.name
            shutil.copyfile(path, copy)
            written.append(copy)
    return written


def sources_for(spec: BuildSpec, keys: Sequence[str]) -> list[tuple[str, SourceRef]]:
    """``(output key, source)`` for each of ``keys`` the spec declares."""
    by_key = {(s.alias or f"{s.provider}.{s.dataset}"): s for s in spec.sources}
    return [(key, by_key[key]) for key in keys if key in by_key]


__all__ = [
    "CARD_JSON",
    "CatalogLookup",
    "card_source",
    "personal_information",
    "processing_steps",
    "sources_for",
    "write_card",
]
