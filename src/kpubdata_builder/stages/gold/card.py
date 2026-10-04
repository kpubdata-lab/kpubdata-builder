"""dataset card (README) contract and Markdown template (#37), filled from provenance (#694).

Besides the schema and a sample, a card says where the data comes from and what was done
to it — the facts a reuser needs and #677 found missing on most published datasets:

- **Provenance**, per source: the providing institution (the BuildSpec's declared
  ``attribution``), the source URL, the licence **under its original name** (never
  rewritten — KOGL type 1 stays KOGL type 1), and when it was collected;
- **Processing**: every declared transformation, or a statement that there was none;
- **Personal information**: the PII policy the build ran under.

The same sections are written as ``card.json`` next to the README, so publishing can
check them without parsing Markdown: a section left empty blocks publishing
(:func:`missing_sections`). ``card.json`` is the contract's ``DatasetCard`` schema
(#955): besides the sentences the README shows, it states as fields what a client
would otherwise read out of a sentence — whether the declared licence and the
provider's differ (``provenance[].license_mismatch``) and whether any transformation
was declared (``processing_declared``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ...exporters._json_safe import json_safe
from ...spec import JsonValue


@dataclass(frozen=True)
class CardField:
    """single column in card schema section."""

    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class CardSource:
    """Where one source's data comes from (#694)."""

    source: str
    institution: str
    url: str
    #: The licence sentence the README shows: the declared licence, with the
    #: provider's own terms beside it when they differ.
    license: str
    collected_at: str
    #: The licence the BuildSpec declares, as written (with its link), or None.
    license_declared: str | None = None
    #: The terms the provider declares in the kpubdata catalog, or None.
    license_provider: str | None = None
    #: True when both are known and the provider's terms are not the declared licence.
    license_mismatch: bool = False


#: Sections a published card must fill; each is checked per source where it is per source.
REQUIRED_SOURCE_FIELDS: tuple[str, ...] = ("institution", "url", "license", "collected_at")
REQUIRED_SECTIONS: tuple[str, ...] = ("provenance", "processing", "personal_information")

#: The processing step stated when nothing was declared — a sentence for the README;
#: ``card.json`` says the same as ``processing_declared: false`` (#955).
NO_PROCESSING_STEP = "No transformation declared: values are as the source gave them."


@dataclass(frozen=True)
class DatasetCard:
    """dataset card (README) contract."""

    title: str
    description: str = ""
    sources: tuple[str, ...] = ()
    fields: tuple[CardField, ...] = ()
    sample_rows: tuple[dict[str, JsonValue], ...] = ()
    license: str = ""
    version: str = ""
    provenance: tuple[CardSource, ...] = ()
    processing: tuple[str, ...] = ()
    personal_information: str = ""
    #: Whether ``processing`` lists a declared transformation, not only
    #: :data:`NO_PROCESSING_STEP` (#955).
    processing_declared: bool = False


def build_dataset_card(
    *,
    title: str,
    description: str = "",
    sources: Iterable[str] = (),
    fields: Iterable[tuple[str, str, bool]] = (),
    sample_rows: Iterable[Mapping[str, JsonValue]] = (),
    license: str = "",
    version: str = "",
    provenance: Iterable[CardSource] = (),
    processing: Iterable[str] = (),
    personal_information: str = "",
    processing_declared: bool | None = None,
) -> DatasetCard:
    """assembles DatasetCard from raw input.

    ``processing_declared`` defaults to whether ``processing`` holds any step other
    than :data:`NO_PROCESSING_STEP`.
    """
    steps = tuple(processing)
    if processing_declared is None:
        processing_declared = any(step != NO_PROCESSING_STEP for step in steps)
    return DatasetCard(
        title=title,
        description=description,
        sources=tuple(sources),
        fields=tuple(CardField(name=n, type=t, nullable=nl) for n, t, nl in fields),
        sample_rows=tuple(dict(row) for row in sample_rows),
        license=license,
        version=version,
        provenance=tuple(provenance),
        processing=steps,
        personal_information=personal_information,
        processing_declared=processing_declared,
    )


def card_sections(card: DatasetCard) -> dict[str, JsonValue]:
    """The card's provenance, processing and personal-information sections as data —
    the contract's ``DatasetCard`` (#955)."""
    return {
        "card_version": 1,
        "title": card.title,
        "provenance": [
            {
                "source": s.source,
                "institution": s.institution,
                "url": s.url,
                "license": s.license,
                "collected_at": s.collected_at,
                "license_declared": s.license_declared,
                "license_provider": s.license_provider,
                "license_mismatch": s.license_mismatch,
            }
            for s in card.provenance
        ],
        "processing": list(card.processing),
        "processing_declared": card.processing_declared,
        "personal_information": card.personal_information,
    }


def missing_sections(sections: Mapping[str, object]) -> list[str]:
    """The required sections a ``card.json`` leaves empty, as ``section`` or
    ``provenance[source].field``; empty when the card is complete."""
    missing: list[str] = []
    for name in REQUIRED_SECTIONS:
        value = sections.get(name)
        if not value or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    provenance = sections.get("provenance")
    for entry in provenance if isinstance(provenance, list) else []:
        if not isinstance(entry, Mapping):
            missing.append("provenance")
            continue
        for field in REQUIRED_SOURCE_FIELDS:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                missing.append(f"provenance[{entry.get('source')}].{field}")
    return missing


def _cell(value: object) -> str:
    """creates safe string for Markdown table cell(escapes pipes/newlines)."""
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        # A date, a time, a datetime and a Decimal fail if directly JSON encoded, alone
        # or inside a list or struct, so they take the text every exporter gives them
        # (#195, #979).
        safe = json_safe(value)
        text = (
            safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, sort_keys=True)
        )
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _render_schema(fields: Sequence[CardField]) -> list[str]:
    """renders schema summary section."""
    lines = ["## Schema", ""]
    if not fields:
        lines.append("_No schema available._")
        return lines
    lines.append("| Column | Type | Nullable |")
    lines.append("| --- | --- | --- |")
    for column in fields:
        nullable = "yes" if column.nullable else "no"
        lines.append(f"| {_cell(column.name)} | {_cell(column.type)} | {nullable} |")
    return lines


def _render_sample(card: DatasetCard) -> list[str]:
    """renders sample preview section."""
    lines = ["## Sample", ""]
    columns = [column.name for column in card.fields]
    if not columns or not card.sample_rows:
        lines.append("_No sample rows available._")
        return lines
    lines.append("| " + " | ".join(_cell(name) for name in columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in card.sample_rows:
        lines.append("| " + " | ".join(_cell(row.get(name)) for name in columns) + " |")
    return lines


def _render_provenance(provenance: Sequence[CardSource]) -> list[str]:
    lines = ["## Provenance", ""]
    if not provenance:
        lines.append("_Not recorded._")
        return lines
    for source in provenance:
        lines += [
            f"### {source.source}",
            "",
            # The BuildSpec's attribution, or the attribution text kpubdata declares
            # (kpubdata#617) — a statement to show, not always an institution's name.
            f"- Attribution: {source.institution or '_not declared_'}",
            f"- Source: {source.url or '_not known_'}",
            f"- Licence: {source.license or '_not declared_'}",
            f"- Collected: {source.collected_at or '_not known_'}",
            "",
        ]
    return lines[:-1]


def render_dataset_card(card: DatasetCard) -> str:
    """renders DatasetCard to Markdown README string."""
    lines: list[str] = [f"# {card.title}", ""]
    if card.description:
        lines += [card.description, ""]

    lines += ["## Sources", ""]
    if card.sources:
        lines += [f"- {source}" for source in card.sources]
    else:
        lines.append("_No sources recorded._")
    lines.append("")

    lines += _render_schema(card.fields)
    lines.append("")
    lines += _render_sample(card)
    lines.append("")

    lines += _render_provenance(card.provenance)
    lines.append("")
    lines += ["## Processing", ""]
    lines += [f"- {step}" for step in card.processing] or ["_Not recorded._"]
    lines.append("")
    lines += ["## Personal information", "", card.personal_information or "_Not recorded._", ""]
    lines += ["## License", "", card.license or "N/A", ""]
    lines += ["## Version", "", card.version or "unversioned"]
    return "\n".join(lines) + "\n"


__all__ = [
    "NO_PROCESSING_STEP",
    "REQUIRED_SECTIONS",
    "REQUIRED_SOURCE_FIELDS",
    "CardField",
    "CardSource",
    "DatasetCard",
    "build_dataset_card",
    "card_sections",
    "missing_sections",
    "render_dataset_card",
]
