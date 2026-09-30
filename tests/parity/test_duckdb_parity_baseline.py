"""The current engine still matches the committed parity baseline (#865).

This test only reads golden files; ``scripts/generate_duckdb_parity_baseline.py`` is the
one thing that writes them. When the engine changes on purpose, regenerate and commit
the diff with the reason — the diff is the record of what changed for users.
"""

from __future__ import annotations

import json

import pytest

from .canonical import to_json
from .scenarios import GOLDEN, SCENARIOS


def test_every_scenario_has_a_baseline_and_no_baseline_is_orphaned() -> None:
    committed = {path.stem for path in GOLDEN.glob("*.json")}

    assert committed == set(SCENARIOS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_matches_its_baseline(name: str) -> None:
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    actual = json.loads(to_json(SCENARIOS[name]()))

    assert actual == expected, (
        f"{name} no longer matches tests/golden/duckdb_parity/{name}.json; if the change "
        "is intended, run scripts/generate_duckdb_parity_baseline.py and commit the diff"
    )


def test_the_harness_needs_no_duckdb() -> None:
    """The baseline is taken before DuckDB exists; it must not quietly depend on it."""
    import sys

    SCENARIOS["r15_sqlglot_dialect"]()

    assert "duckdb" not in sys.modules
