"""Static data catalog page generator (#42).

Creates static HTML catalog pages for brand distribution based on build manifests.
Lists each dataset's title, description, record count, source count, and artifact list as cards.
All dynamic text is HTML-escaped for safe insertion.

Key components:
    - CatalogEntry: Single catalog entry
    - catalog_entry_from_manifest: BuildManifest → CatalogEntry
    - render_catalog_html: Item list → completed HTML document
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from .manifest import BuildManifest


@dataclass(frozen=True)
class CatalogEntry:
    """Single dataset entry on catalog page.

    Attributes:
        dataset_id: Dataset identifier.
        title: Display title.
        description: Description.
        record_count: Total record count.
        source_count: Number of sources.
        outputs: List of artifact paths.
    """

    dataset_id: str
    title: str
    description: str = ""
    record_count: int = 0
    source_count: int = 0
    outputs: tuple[str, ...] = field(default_factory=tuple)


def catalog_entry_from_manifest(
    manifest: BuildManifest,
    *,
    dataset_id: str,
    title: str,
    description: str = "",
) -> CatalogEntry:
    """Derive catalog entry from BuildManifest.

    Record count is sum of row_counts, source count is inputs length.

    Args:
        manifest: Build manifest to get statistics from.
        dataset_id: Dataset identifier.
        title: Display title.
        description: Description.

    Returns:
        CatalogEntry: Renderable catalog entry.
    """
    return CatalogEntry(
        dataset_id=dataset_id,
        title=title,
        description=description,
        record_count=sum(manifest.row_counts.values()),
        source_count=len(manifest.inputs),
        outputs=tuple(manifest.outputs),
    )


def _render_entry(entry: CatalogEntry) -> list[str]:
    """Render single catalog entry as HTML card."""
    lines = [
        '    <article class="dataset-card">',
        f"      <h2>{escape(entry.title)}</h2>",
        f'      <p class="dataset-id">{escape(entry.dataset_id)}</p>',
    ]
    if entry.description:
        lines.append(f"      <p>{escape(entry.description)}</p>")
    lines += [
        '      <ul class="stats">',
        f"        <li>Records: {entry.record_count}</li>",
        f"        <li>Sources: {entry.source_count}</li>",
        f"        <li>Outputs: {len(entry.outputs)}</li>",
        "      </ul>",
    ]
    if entry.outputs:
        lines.append('      <ul class="outputs">')
        lines += [f"        <li>{escape(path)}</li>" for path in entry.outputs]
        lines.append("      </ul>")
    lines.append("    </article>")
    return lines


def render_catalog_html(entries: list[CatalogEntry], *, site_title: str = "Data Catalog") -> str:
    """Render list of catalog entries as complete static HTML document.

    Args:
        entries: Catalog entries to display (preserve given order).
        site_title: Page title.

    Returns:
        str: HTML document including final newline.
    """
    head = [
        "<!DOCTYPE html>",
        '<html lang="ko">',
        "<head>",
        '  <meta charset="utf-8">',
        f"  <title>{escape(site_title)}</title>",
        "</head>",
        "<body>",
        f"  <h1>{escape(site_title)}</h1>",
        f'  <p class="count">{len(entries)} dataset(s)</p>',
        '  <main class="catalog">',
    ]
    body: list[str] = []
    if entries:
        for entry in entries:
            body += _render_entry(entry)
    else:
        body.append("    <p>No datasets available.</p>")
    tail = ["  </main>", "</body>", "</html>"]
    return "\n".join(head + body + tail) + "\n"


__all__ = ["CatalogEntry", "catalog_entry_from_manifest", "render_catalog_html"]
