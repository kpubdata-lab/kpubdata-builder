"""A BuildSpec's YAML is bounded before it is materialised (#1322).

``yaml.safe_load`` shares an aliased node instead of copying it, so a few hundred
bytes of nested aliases parse cheaply into a graph that names a billion nodes. The
spec parser then copies that graph into fresh containers and pays for every one of
them. Deep nesting has the opposite problem: the parser recurses once per level
and dies with ``RecursionError``. Every BuildSpec input — a file, a template, the
CLI and the HTTP body — goes through one bounded loader that counts expanded nodes
and depth while composing, and refuses before anything is copied.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kpubdata_builder import SpecLoadError
from kpubdata_builder.cli import main
from kpubdata_builder.service.build_runs_api import _declared_dataset_id
from kpubdata_builder.service.spec_api import parse_spec_text
from kpubdata_builder.spec import load_spec, load_template, parse_spec
from kpubdata_builder.spec.loader import MAX_SPEC_DEPTH, MAX_SPEC_NODES

_HEAD = """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()


def _alias_bomb(levels: int = 9) -> str:
    """Ten-way nested aliases under ``params``: 10**levels nodes once expanded."""
    lines = [_HEAD, "anchors:", '  l0: &l0 ["x","x","x","x","x","x","x","x","x","x"]']
    for level in range(1, levels + 1):
        prev = f"*l{level - 1}"
        lines.append(f"  l{level}: &l{level} [{','.join([prev] * 10)}]")
    lines += [
        "sources:",
        "  - provider: datago",
        "    dataset: air_quality",
        f"    params: {{bomb: *l{levels}}}",
    ]
    return "\n".join(lines) + "\n"


def _deep_nesting(depth: int) -> str:
    return (
        _HEAD
        + "\nsources:\n  - provider: datago\n    dataset: air_quality\n    params:\n"
        + f"      deep: {'[' * depth}{']' * depth}\n"
    )


_NORMAL_WITH_ALIAS = (
    _HEAD
    + """
sources:
  - provider: datago
    dataset: air_quality
    params: &common
      sidoName: seoul
      numOfRows: 100
  - provider: datago
    dataset: air_quality
    alias: second
    params: *common
"""
)


def test_alias_bomb_in_a_file_is_refused_in_bounded_time(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(_alias_bomb(), encoding="utf-8")
    assert len(spec_path.read_bytes()) < 2000

    started = time.monotonic()
    with pytest.raises(SpecLoadError, match="more than"):
        load_spec(spec_path)
    assert time.monotonic() - started < 2.0


def test_alias_bomb_in_text_is_refused_in_bounded_time() -> None:
    started = time.monotonic()
    with pytest.raises(SpecLoadError, match="more than"):
        parse_spec_text(_alias_bomb())
    assert time.monotonic() - started < 2.0


def test_alias_bomb_in_a_template_is_refused(tmp_path: Path) -> None:
    template = tmp_path / "template.yaml"
    template.write_text(_alias_bomb(), encoding="utf-8")
    with pytest.raises(SpecLoadError, match="more than"):
        load_template(template, {})


def test_node_budget_counts_expanded_aliases() -> None:
    # Three levels of ten-way aliases expand to 1,000 leaves: below the budget.
    spec = parse_spec_text(_alias_bomb(levels=3))
    assert spec.dataset_id == "dataset.sample"
    # One level more than the budget allows is refused, though the text is tiny.
    levels = 1
    while 10**levels <= MAX_SPEC_NODES:
        levels += 1
    with pytest.raises(SpecLoadError, match=f"more than {MAX_SPEC_NODES}"):
        parse_spec_text(_alias_bomb(levels=levels))


@pytest.mark.parametrize("depth", [MAX_SPEC_DEPTH + 1, 5000])
def test_deep_nesting_is_refused_without_recursion_error(tmp_path: Path, depth: int) -> None:
    text = _deep_nesting(depth)
    with pytest.raises(SpecLoadError, match="nested deeper than"):
        parse_spec_text(text)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(text, encoding="utf-8")
    with pytest.raises(SpecLoadError, match="nested deeper than"):
        load_spec(spec_path)


def test_deep_nesting_in_a_mapping_is_refused_without_recursion_error() -> None:
    # parse_spec also takes a mapping that never went through YAML.
    deep: object = "leaf"
    for _ in range(5000):
        deep = [deep]
    data: dict[str, object] = {
        "dataset_id": "dataset.sample",
        "title": "t",
        "description": "d",
        "metadata": {"deep": deep},
        "sources": [{"provider": "datago", "dataset": "air_quality"}],
    }
    with pytest.raises(SpecLoadError, match="nested deeper than"):
        parse_spec(data)


def test_cycle_is_still_refused_as_a_circular_reference() -> None:
    text = (
        _HEAD
        + "\nsources:\n  - provider: datago\n    dataset: air_quality\n"
        + "    params: &p\n      self: *p\n"
    )
    with pytest.raises(SpecLoadError, match="circular reference"):
        parse_spec_text(text)


def test_a_normal_alias_still_loads(tmp_path: Path) -> None:
    spec = parse_spec_text(_NORMAL_WITH_ALIAS)
    assert (
        spec.sources[0].params
        == spec.sources[1].params
        == {
            "sidoName": "seoul",
            "numOfRows": 100,
        }
    )
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(_NORMAL_WITH_ALIAS, encoding="utf-8")
    assert load_spec(spec_path) == spec


def test_job_label_read_does_not_crash_on_a_hostile_spec() -> None:
    # The HTTP build path reads dataset_id to label a queued job before the worker
    # validates the spec. A hostile spec gets no label rather than a 500.
    assert _declared_dataset_id(_deep_nesting(5000)) is None
    assert _declared_dataset_id(_alias_bomb()) is None


def test_cli_validate_refuses_an_alias_bomb(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(_alias_bomb(), encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "more than" in captured.err
