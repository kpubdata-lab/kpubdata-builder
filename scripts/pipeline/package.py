"""Package transformed data into Parquet files and dataset cards.

DEPRECATED — legacy publish path (#208). The canonical execution path is the
medallion orchestrator ``kpubdata_builder.pipeline.run_build``. See
``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger("publish_to_hf.package")


def write_parquet(df: pl.DataFrame, output_path: Path) -> Path:
    """Write DataFrame to Parquet file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(output_path)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info("Wrote parquet: %s (%.2f MB, %d rows)", output_path, size_mb, df.height)
    return output_path


def license_front_matter(card: dict[str, Any]) -> str:
    """The card's licence lines for the front matter, or a refusal (#758).

    There is no default. The old one was ``cc-by-4.0``, which claimed a licence nobody
    chose and that the source did not grant: most sources here state no restriction on
    use, and one is KOGL Type 3, which forbids exactly the change CC BY allows. A config
    that does not say what its source grants cannot be packaged.

    ``license: other`` is how Hugging Face records a licence outside its list, and it
    means nothing without ``license_name`` and ``license_link`` — so those are required
    with it.

    Raises:
        ValueError: ``license`` is missing, or ``other`` without its name and link.
    """
    license_id = str(card.get("license") or "").strip()
    if not license_id:
        raise ValueError(
            "card.license is required: state the terms the source grants "
            "(e.g. license: other + license_name + license_link). There is no default."
        )
    lines = [f"license: {license_id}"]
    if license_id == "other":
        name = str(card.get("license_name") or "").strip()
        link = str(card.get("license_link") or "").strip()
        if not name or not link:
            raise ValueError(
                "card.license is 'other', so card.license_name and card.license_link are "
                "required — 'other' alone tells a reader nothing about the terms"
            )
        lines += [f"license_name: {name}", f"license_link: {link}"]
    return "\n".join(lines)


def generate_dataset_card(
    df: pl.DataFrame,
    config: dict[str, Any],
    output_path: Path,
    variant_names: list[str] | None = None,
) -> Path:
    """Generate a HuggingFace dataset card (README.md) from config and data."""
    card = config["card"]
    output_cfg = config["output"]

    features_table = _build_features_table(card.get("features", []))
    sample_table = _build_sample_table(df, max_rows=5)
    stats_section = _build_stats_section(df)

    tags_yaml = "\n".join(f"- {t}" for t in card.get("tags", []))
    languages_yaml = "\n".join(f"- {lang}" for lang in card.get("language", ["ko"]))
    task_categories = card.get("task_categories", [])
    task_categories_yaml = "\n".join(f"- {t}" for t in task_categories)
    task_categories_block = f"task_categories:\n{task_categories_yaml}\n" if task_categories else ""
    hf_repo = output_cfg["hf_repo"]
    source_url = config["source"].get("source_url", "https://www.data.go.kr")
    attribution = card.get("attribution", "")
    attribution_block = f"\n{attribution}\n" if attribution else ""

    configs_block = ""
    if variant_names and len(variant_names) > 1:
        configs_lines = ["configs:"]
        for vname in variant_names:
            configs_lines.append(f"- config_name: {vname}")
            configs_lines.append(f"  data_files: data/{vname}/train.parquet")
        default_config = "en" if "en" in variant_names else variant_names[0]
        configs_lines.append(f"default_config_name: {default_config}")
        configs_block = "\n".join(configs_lines) + "\n"

    content = f"""---
{license_front_matter(card)}
language:
{languages_yaml}
tags:
{tags_yaml}
size_categories:
- {_size_category(df.height)}
{task_categories_block}{configs_block}---

# {card["title"]}

{card["description"]}

## Dataset Summary

- **Records**: {df.height:,}
- **Features**: {df.width}
- **Source**: [data.go.kr]({source_url})
- **HuggingFace Repo**: [{hf_repo}](https://huggingface.co/datasets/{hf_repo})

{stats_section}

## Features

{features_table}

## Sample Data

{sample_table}

## Usage

```python
from datasets import load_dataset

ds = load_dataset("{hf_repo}")
df = ds["train"].to_pandas()
print(df.head())
```

## Source

This dataset was generated using [kpubdata](https://github.com/kpubdata-lab/kpubdata)
from public data APIs on data.go.kr.
{attribution_block}"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    logger.info("Wrote dataset card: %s", output_path)
    return output_path


def _build_features_table(features: list[dict[str, str]]) -> str:
    if not features:
        return ""
    lines = ["| Feature | Description |", "| :--- | :--- |"]
    for f in features:
        escaped_desc = f["description"].replace("|", "\\|")
        lines.append(f"| `{f['name']}` | {escaped_desc} |")
    return "\n".join(lines)


def _build_sample_table(df: pl.DataFrame, max_rows: int = 5) -> str:
    sample = df.head(max_rows)
    header = "| " + " | ".join(sample.columns) + " |"
    separator = "| " + " | ".join("---" for _ in sample.columns) + " |"
    rows: list[str] = []
    for row in sample.iter_rows(named=True):
        cells = " | ".join(str(v).replace("|", "\\|") for v in row.values())
        rows.append(f"| {cells} |")
    return "\n".join([header, separator, *rows])


def _build_stats_section(df: pl.DataFrame) -> str:
    numeric_cols = [
        col for col, dtype in zip(df.columns, df.dtypes, strict=True) if dtype.is_numeric()
    ]
    if not numeric_cols:
        return ""

    lines = [
        "## Statistics",
        "",
        "| Feature | Mean | Std | Min | Max |",
        "| :--- | ---: | ---: | ---: | ---: |",
    ]
    for col in numeric_cols:
        series = df[col].drop_nulls()
        if series.is_empty():
            continue
        mean_val = series.mean()
        std_val = series.std()
        mean = float(str(mean_val)) if mean_val is not None else 0.0
        std = float(str(std_val)) if std_val is not None else 0.0
        min_val = str(series.min())
        max_val = str(series.max())
        lines.append(f"| `{col}` | {mean:,.2f} | {std:,.2f} | {min_val} | {max_val} |")
    return "\n".join(lines)


def _size_category(n: int) -> str:
    if n < 1_000:
        return "n<1K"
    if n < 10_000:
        return "1K<n<10K"
    if n < 100_000:
        return "10K<n<100K"
    if n < 1_000_000:
        return "100K<n<1M"
    if n < 10_000_000:
        return "1M<n<10M"
    return "n>10M"
