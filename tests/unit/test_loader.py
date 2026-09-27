"""Verify success and failure scenarios of BuildSpec YAML load path."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder import SpecLoadError
from kpubdata_builder.spec import load_spec


def test_load_spec_reads_valid_yaml(tmp_path: Path) -> None:
    # Verify valid YAML parses into BuildSpec object.
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n",
        encoding="utf-8",
    )

    spec = load_spec(spec_path)

    assert spec.dataset_id == "dataset.sample"
    assert spec.sources[0].provider == "datago"
    assert spec.exports[0].output_path == "out/data.jsonl"


def test_load_spec_raises_for_invalid_yaml(tmp_path: Path) -> None:
    # Verify invalid YAML syntax is wrapped as SpecLoadError.
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("dataset_id: [unterminated\n", encoding="utf-8")

    with pytest.raises(SpecLoadError):
        load_spec(spec_path)


def test_load_spec_raises_for_missing_file(tmp_path: Path) -> None:
    # Verify non-existent file path is handled as SpecLoadError.
    with pytest.raises(SpecLoadError):
        load_spec(tmp_path / "missing.yaml")


def test_load_spec_rejects_circular_yaml_without_crash(tmp_path: Path) -> None:
    # YAML anchor/alias circular structures should be SpecLoadError, not
    # RecursionError crash (#169).
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
    params: &p
      self: *p
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecLoadError, match="circular reference"):
        load_spec(spec_path)


def test_load_spec_rejects_non_finite_float_params(tmp_path: Path) -> None:
    # YAML .nan becomes non-standard NaN token in json.dumps, so reject as SpecLoadError (#201).
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
    params:
      threshold: .nan
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecLoadError, match="finite"):
        load_spec(spec_path)


def test_load_spec_parses_rename_and_derived(tmp_path: Path) -> None:
    # #611 — for Silver to become canonical dataset, rename and derived columns must
    # be expressible as declaration.
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: dataset.trades
title: Trades
description: Seoul apartment trades
sources:
  - provider: datago
    dataset: apt_trade
    schema:
      rename:
        sggCd: district_code
        dealAmount: deal_amount
      casts:
        deal_amount: int_comma
      derived:
        - name: deal_date
          kind: date_parts
          columns: [dealYear, dealMonth, dealDay]
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + chr(10),
        encoding="utf-8",
    )

    spec = load_spec(spec_path)

    schema = spec.sources[0].schema
    assert schema is not None
    assert schema.rename == {"sggCd": "district_code", "dealAmount": "deal_amount"}
    assert schema.derived[0].name == "deal_date"
    assert schema.derived[0].kind == "date_parts"
    assert schema.derived[0].columns == ("dealYear", "dealMonth", "dealDay")
