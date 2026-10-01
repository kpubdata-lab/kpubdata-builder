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
from ..stages.gold.card import (
    NO_PROCESSING_STEP,
    CardSource,
    DatasetCard,
    card_sections,
    render_dataset_card,
)
from ..stages.gold.pii import PII_MASK_TOKEN, PiiMaskResult

CatalogLookup = Callable[[str], DatasetCatalogInfo | None]

CARD_JSON = "card.json"
_KAGGLE_METADATA = "dataset-metadata.json"
_HF_INFOS = "dataset_infos.json"


def _front_matter(text: str) -> str:
    """The YAML front matter block at the top of a card, with its fences, or ""."""
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    return text[: end + 5] + "\n" if end != -1 else ""


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
        # The upload id is internal; a public card does not carry it.
        url = "uploaded file"
    else:
        url = (info.source_url if info else "") or ""
    declared, provider, mismatch = _licence_facts(spec, info)
    return CardSource(
        source=label,
        institution=institution,
        url=url,
        license=_original_licence(declared, provider, mismatch),
        collected_at=_collected_at(provenance),
        license_declared=declared or None,
        license_provider=provider or None,
        license_mismatch=mismatch,
    )


def _licence_facts(spec: BuildSpec, info: DatasetCatalogInfo | None) -> tuple[str, str, bool]:
    """``(declared, provider, mismatch)``: the BuildSpec's licence as written (with its
    link), the provider's declared terms, and whether both are known and differ (#955)."""
    declared = (spec.license_name or spec.license or "").strip()
    if spec.license_link and declared:
        declared = f"{declared} ({spec.license_link})"
    provider = (info.license_type or "").strip() if info else ""
    mismatch = bool(provider and declared and provider.lower() not in declared.lower())
    return declared, provider, mismatch


def _original_licence(declared: str, provider: str, mismatch: bool) -> str:
    """The licence under the name it was declared with — never restated as another."""
    if mismatch:
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
    """Every transformation declared for ``source``, in the order they run, or
    :data:`NO_PROCESSING_STEP` alone when there is none (``card.json`` then says
    ``processing_declared: false``, #955)."""
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
    return steps or [NO_PROCESSING_STEP]


def personal_information(spec: BuildSpec, masking: PiiMaskResult | None = None) -> str:
    """How personal information was handled, from what the build recorded — stated even
    when nothing was done.

    Two separate facts (#689, #902): what Gold did with the columns declared personal
    information (``masking``, the manifest's ``pii_masking``), and how the BuildSpec
    ``pii`` policy scanned Silver's values.
    """
    parts: list[str] = []
    if masking is not None and masking.masked:
        columns = ", ".join(
            f"{name} ({'emptied' if name in masking.nulled else f'replaced by {PII_MASK_TOKEN}'})"
            for name in sorted(masking.masked)
        )
        parts.append(f"Columns declared personal information were masked: {columns}.")
    if masking is not None and masking.unmasked:
        parts.append(
            "Columns declared personal information published unmasked, as the BuildSpec's "
            f"gold.publish_unmasked asks: {', '.join(sorted(masking.unmasked))}."
        )
    if masking is not None and masking.declared_absent:
        parts.append(
            "Declared personal information by kpubdata but not in this source: "
            f"{', '.join(masking.declared_absent)}."
        )
    if not parts:
        parts.append("No column was declared personal information.")
    policy = spec.pii
    if policy is None:
        parts.append("Values were not scanned for personal information (no pii policy).")
    else:
        outcome = {
            "block": "a column that looked like personal information failed the build",
            "warn": "columns that looked like personal information were reported, not removed",
            "allow": "what the scan found was not acted on",
        }[policy.mode]
        parts.append(f"Values were scanned (pii mode: {policy.mode}); {outcome}.")
        if policy.allow_columns:
            parts.append(
                f"Accepted as publishable despite the scan: {', '.join(policy.allow_columns)}."
            )
    return " ".join(parts)


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
    # A Hugging Face layout uploads its own README.md to the repository root, after the
    # Gold directory's (#694): its card gets the same sections, under the exporter's
    # front matter, so whichever lands last is complete.
    for infos in sorted(gold_dir.rglob(_HF_INFOS)):
        layout = infos.parent
        hf_readme = layout / "README.md"
        front = _front_matter(hf_readme.read_text(encoding="utf-8")) if hf_readme.is_file() else ""
        hf_readme.write_text(front + render_dataset_card(card), encoding="utf-8")
        shutil.copyfile(sections, layout / CARD_JSON)
        written += [hf_readme, layout / CARD_JSON]
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
