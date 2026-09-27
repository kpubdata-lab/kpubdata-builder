"""Validate rendering and loading of reusable build templates (#14)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.spec import load_template, render_template

_TEMPLATE = """\
_template:
  name: air quality
  parameters:
    station_name:
      default: 종로구
    fmt:
      default: jsonl
dataset_id: "air_quality_{{ station_name }}"
title: "{{ station_name }} 대기오염"
description: "air quality for {{ station_name }}"
sources:
  - provider: datago
    dataset: air_quality
    params:
      stationName: "{{ station_name }}"
exports:
  - kind: "{{ fmt }}"
    output_path: "data.{{ fmt }}"
"""


def _write_template(tmp_path: Path) -> Path:
    path = tmp_path / "air_quality.yaml"
    path.write_text(_TEMPLATE, encoding="utf-8")
    return path


def test_render_uses_defaults_when_no_params(tmp_path: Path) -> None:
    rendered = render_template(_write_template(tmp_path), {})

    data = yaml.safe_load(rendered)
    assert data["dataset_id"] == "air_quality_종로구"
    assert data["exports"][0]["kind"] == "jsonl"
    # _template metadata block must be removed.
    assert "_template" not in data


def test_render_overrides_defaults_with_params(tmp_path: Path) -> None:
    rendered = render_template(_write_template(tmp_path), {"station_name": "강남구", "fmt": "csv"})

    data = yaml.safe_load(rendered)
    assert data["dataset_id"] == "air_quality_강남구"
    assert data["title"] == "강남구 대기오염"
    assert data["sources"][0]["params"]["stationName"] == "강남구"
    assert data["exports"][0]["output_path"] == "data.csv"


def test_load_template_returns_valid_build_spec(tmp_path: Path) -> None:
    spec = load_template(_write_template(tmp_path), {"station_name": "강남구"})

    assert spec.dataset_id == "air_quality_강남구"
    assert spec.sources[0].provider == "datago"
    assert spec.exports[0].kind == "jsonl"


def test_render_raises_on_missing_parameter(tmp_path: Path) -> None:
    # Error if placeholder has neither default value nor provided value.
    path = tmp_path / "t.yaml"
    path.write_text(
        '_template:\n  name: t\ndataset_id: "{{ missing }}"\ntitle: t\n', encoding="utf-8"
    )

    with pytest.raises(SpecLoadError, match="Missing template parameter"):
        render_template(path, {})


def test_render_rejects_non_mapping_template(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(SpecLoadError, match="must be a mapping"):
        render_template(path, {})


def test_load_template_avoids_yaml_reparse(tmp_path: Path) -> None:
    # #225: load_template calls render_template to serialize YAML to string,
    # but passes substituted in-memory structure directly to parse_spec
    # without reparsing via yaml.safe_load, eliminating unnecessary
    # serialization/deserialization roundtrips. Verify that parameter values
    # appearing integer-like are preserved as strings.
    path = tmp_path / "tmpl.yaml"
    path.write_text(
        '_template:\n  parameters:\n    version:\n      default: "v1"\n'
        "dataset_id: air_quality\ntitle: Air Quality\n"
        "description: d\n"
        "sources:\n  - provider: datago\n    dataset: air_quality\n"
        '    params:\n      version: "{{ version }}"\n'
        "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n",
        encoding="utf-8",
    )

    spec = load_template(path, {"version": "v2"})

    # Parameter substitution works correctly and BuildSpec is parsed.
    assert spec.sources[0].params["version"] == "v2"
    assert isinstance(spec.sources[0].params["version"], str)
    # Calling twice returns identical result (deterministic).
    spec2 = load_template(path, {"version": "v2"})
    assert spec.sources[0].params == spec2.sources[0].params
