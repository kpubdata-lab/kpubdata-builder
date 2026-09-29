"""Tests for scripts/pipeline/package.py — parquet output and dataset card.

Pure logic tests; no network calls.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import polars as pl
import pytest

_PACKAGE_PATH = Path(__file__).parents[2] / "scripts" / "pipeline" / "package.py"


def _load_package() -> Any:
    spec = importlib.util.spec_from_file_location("scripts.pipeline.package", _PACKAGE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("scripts.pipeline.package", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


package_mod = _load_package()
write_parquet = package_mod.write_parquet
generate_dataset_card = package_mod.generate_dataset_card
_size_category = package_mod._size_category


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_config(hf_repo: str = "kpubdata/test-dataset") -> dict[str, Any]:
    return {
        "source": {"source_url": "https://www.data.go.kr"},
        "output": {"hf_repo": hf_repo},
        "card": {
            "title": "Test Dataset",
            "description": "A test dataset.",
            "license": "cc-by-4.0",
            "language": ["ko"],
            "tags": ["korea", "test"],
            "features": [{"name": "id", "description": "Record identifier"}],
        },
    }


# ---------------------------------------------------------------------------
# write_parquet
# ---------------------------------------------------------------------------


def test_write_parquet_creates_file(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1", "2"], "value": [10, 20]})
    out = tmp_path / "data" / "train.parquet"

    result = write_parquet(df, out)

    assert result == out
    assert out.exists()
    assert pl.read_parquet(out).equals(df)


def test_write_parquet_creates_parent_dirs(tmp_path: Path) -> None:
    df = pl.DataFrame({"x": [1]})
    nested = tmp_path / "a" / "b" / "c" / "out.parquet"

    write_parquet(df, nested)

    assert nested.exists()


def test_write_parquet_roundtrip_preserves_schema(tmp_path: Path) -> None:
    df = pl.DataFrame({"name": ["Seoul"], "count": [42]})
    out = tmp_path / "out.parquet"
    write_parquet(df, out)

    loaded = pl.read_parquet(out)
    assert loaded.columns == df.columns
    assert loaded["count"][0] == 42


# ---------------------------------------------------------------------------
# generate_dataset_card
# ---------------------------------------------------------------------------


def test_generate_dataset_card_creates_readme(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1", "2"]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    result = generate_dataset_card(df, config, out)

    assert result == out
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "# Test Dataset" in content
    assert "kpubdata/test-dataset" in content


def test_generate_dataset_card_contains_frontmatter(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1"]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    generate_dataset_card(df, config, out)

    content = out.read_text(encoding="utf-8")
    assert content.startswith("---")
    assert "license: cc-by-4.0" in content
    assert "- korea" in content


def test_generate_dataset_card_with_variants(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1"]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    generate_dataset_card(df, config, out, variant_names=["ko", "en"])

    content = out.read_text(encoding="utf-8")
    assert "config_name: ko" in content
    assert "config_name: en" in content
    assert "default_config_name: en" in content


def test_generate_dataset_card_sample_table(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["r1", "r2", "r3", "r4", "r5", "r6"]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    generate_dataset_card(df, config, out)

    content = out.read_text(encoding="utf-8")
    # Sample table capped at 5 rows — r6 should not appear
    assert "r5" in content
    assert "r6" not in content


def test_generate_dataset_card_stats_for_numeric(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1", "2"], "count": [10, 20]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    generate_dataset_card(df, config, out)

    content = out.read_text(encoding="utf-8")
    assert "## Statistics" in content
    assert "`count`" in content


# ---------------------------------------------------------------------------
# _size_category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "n<1K"),
        (999, "n<1K"),
        (1_000, "1K<n<10K"),
        (9_999, "1K<n<10K"),
        (10_000, "10K<n<100K"),
        (99_999, "10K<n<100K"),
        (100_000, "100K<n<1M"),
        (999_999, "100K<n<1M"),
        (1_000_000, "1M<n<10M"),
        (9_999_999, "1M<n<10M"),
        (10_000_000, "n>10M"),
    ],
)
def test_size_category(n: int, expected: str) -> None:
    assert _size_category(n) == expected


# ---------------------------------------------------------------------------
# Licence: the source's own terms, never a default (#758)
# ---------------------------------------------------------------------------

_CONFIGS = Path(__file__).parents[2] / "scripts" / "configs"


def test_an_other_licence_carries_its_name_and_link(tmp_path: Path) -> None:
    config = _minimal_config()
    config["card"].update(
        license="other",
        license_name="korea-public-data-unrestricted",
        license_link="https://www.data.go.kr/data/15059486/openapi.do",
    )
    out = tmp_path / "README.md"

    generate_dataset_card(pl.DataFrame({"id": ["1"]}), config, out)

    front_matter = out.read_text(encoding="utf-8").split("---")[1]
    assert "license: other" in front_matter
    assert "license_name: korea-public-data-unrestricted" in front_matter
    assert "license_link: https://www.data.go.kr/data/15059486/openapi.do" in front_matter


def test_a_card_without_a_licence_is_refused(tmp_path: Path) -> None:
    """Negative: nobody chose a licence, so none is claimed — cc-by-4.0 was the default."""
    config = _minimal_config()
    del config["card"]["license"]
    out = tmp_path / "README.md"

    with pytest.raises(ValueError, match="card.license is required"):
        generate_dataset_card(pl.DataFrame({"id": ["1"]}), config, out)
    assert not out.exists()


@pytest.mark.parametrize("missing", ["license_name", "license_link"])
def test_other_without_its_name_or_link_is_refused(tmp_path: Path, missing: str) -> None:
    config = _minimal_config()
    config["card"].update(license="other", license_name="kogl-type-1", license_link="https://x")
    del config["card"][missing]

    with pytest.raises(ValueError, match="license_name and card.license_link"):
        generate_dataset_card(pl.DataFrame({"id": ["1"]}), config, tmp_path / "README.md")


def _licence_check() -> Any:
    spec = importlib.util.spec_from_file_location(
        "scripts.check_config_licence", _CONFIGS.parents[1] / "scripts" / "check_config_licence.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


licence_check = _licence_check()


@pytest.mark.parametrize(
    "path",
    # Every config at every depth — top level, localdata, templates (#792).
    sorted(_CONFIGS.rglob("*.yaml")),
    ids=lambda p: str(p.relative_to(_CONFIGS)),
)
def test_no_config_claims_a_licence_its_source_did_not_grant(path: Path) -> None:
    """#758, #792: a config states the source's terms, or claims none and cannot be packaged.

    The guard used to read only the top level, so localdata/* and templates/* kept
    cc-by-4.0 unseen. The rule lives in scripts/check_config_licence.py, which the
    publish workflow runs on the config it is asked to publish.
    """
    import yaml

    assert licence_check.problems_for(path) == []
    if path.name in licence_check.UNCONFIRMED:
        return
    card = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("card") or {}
    if "license" in card:
        package_mod.license_front_matter(card)
    else:
        with pytest.raises(ValueError, match="card.license is required"):
            package_mod.license_front_matter(card)


def test_the_licence_check_refuses_cc_by(tmp_path: Path) -> None:
    """Negative: the check itself fails on the old default."""
    config = tmp_path / "x.yaml"
    config.write_text("card:\n  license: cc-by-4.0\n", encoding="utf-8")

    assert licence_check.main([str(config)]) == 1


def test_the_only_unconfirmed_licence_is_recorded_with_a_reason() -> None:
    assert set(licence_check.UNCONFIRMED) == {"korea_base_rate.yaml"}
    assert all(reason.strip() for reason in licence_check.UNCONFIRMED.values())
