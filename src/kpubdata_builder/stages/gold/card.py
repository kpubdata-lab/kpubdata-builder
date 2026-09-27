"""dataset card (README) contract and Markdown template (#37)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from ...spec import JsonValue


@dataclass(frozen=True)
class CardField:
    """single column in card schema section."""

    name: str
    type: str
    nullable: bool


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


def build_dataset_card(
    *,
    title: str,
    description: str = "",
    sources: Iterable[str] = (),
    fields: Iterable[tuple[str, str, bool]] = (),
    sample_rows: Iterable[Mapping[str, JsonValue]] = (),
    license: str = "",
    version: str = "",
) -> DatasetCard:
    """assembles DatasetCard from raw input."""
    return DatasetCard(
        title=title,
        description=description,
        sources=tuple(sources),
        fields=tuple(CardField(name=n, type=t, nullable=nl) for n, t, nl in fields),
        sample_rows=tuple(dict(row) for row in sample_rows),
        license=license,
        version=version,
    )


def _cell(value: object) -> str:
    """creates safe string for Markdown table cell(escapes pipes/newlines)."""
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value)
    elif isinstance(value, (date, datetime)):
        # temporal Python objects fail if directly JSON encoded, so like Silver serializer
        # convert to ISO 8601 strings (#195).
        text = value.isoformat()
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
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

    lines += ["## License", "", card.license or "N/A", ""]
    lines += ["## Version", "", card.version or "unversioned"]
    return "\n".join(lines) + "\n"


__all__ = [
    "CardField",
    "DatasetCard",
    "build_dataset_card",
    "render_dataset_card",
]
