"""Verify data catalog page generator (#42)."""

from __future__ import annotations

from datetime import datetime, timezone

from kpubdata_builder.catalog import (
    CatalogEntry,
    catalog_entry_from_manifest,
    render_catalog_html,
)
from kpubdata_builder.manifest import BuildManifest

_TS = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)


def test_entry_from_manifest_derives_counts() -> None:
    manifest = BuildManifest(
        build_id="run1",
        started_at=_TS,
        finished_at=_TS,
        inputs=("datago.apt_trade", "datago.air_quality"),
        outputs=("out/a.jsonl", "out/b.jsonl"),
        row_counts={"datago.apt_trade": 100, "datago.air_quality": 50},
    )

    entry = catalog_entry_from_manifest(
        manifest, dataset_id="seoul", title="Seoul", description="desc"
    )

    assert entry.record_count == 150
    assert entry.source_count == 2
    assert entry.outputs == ("out/a.jsonl", "out/b.jsonl")


def test_render_lists_entries_with_stats() -> None:
    entries = [
        CatalogEntry(
            dataset_id="seoul",
            title="Seoul Apartments",
            description="trades",
            record_count=150,
            source_count=2,
            outputs=("out/a.jsonl",),
        )
    ]

    html = render_catalog_html(entries, site_title="KPubData Catalog")

    assert html.startswith("<!DOCTYPE html>")
    assert "<title>KPubData Catalog</title>" in html
    assert "1 dataset(s)" in html
    assert "Seoul Apartments" in html
    assert "Records: 150" in html
    assert "Sources: 2" in html
    assert "out/a.jsonl" in html
    assert html.endswith("\n")


def test_render_escapes_html_in_dynamic_text() -> None:
    entries = [CatalogEntry(dataset_id="x", title="<script>alert(1)</script>")]

    html = render_catalog_html(entries)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_empty_catalog() -> None:
    html = render_catalog_html([])

    assert "0 dataset(s)" in html
    assert "No datasets available." in html


def test_render_preserves_entry_order() -> None:
    entries = [
        CatalogEntry(dataset_id="b", title="B"),
        CatalogEntry(dataset_id="a", title="A"),
    ]

    html = render_catalog_html(entries)

    assert html.index("B") < html.index("A")
