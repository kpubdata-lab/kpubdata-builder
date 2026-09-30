"""The current engine still matches the committed parity baseline (#865).

This test only reads golden files; ``scripts/generate_duckdb_parity_baseline.py`` is the
one thing that writes them. When the engine changes on purpose, regenerate and commit
the diff with the reason — the diff is the record of what changed for users.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from .canonical import to_json
from .scenarios import GOLDEN, ROOT, SCENARIOS


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
    """The baseline pins the Polars engine; taking it must not quietly load DuckDB.

    Checked in a fresh interpreter: this test session imports DuckDB elsewhere
    (tests/unit/test_duckdb_runtime.py), so its own ``sys.modules`` proves nothing.
    """
    probe = (
        "import sys\n"
        "from tests.parity.scenarios import SCENARIOS\n"
        "SCENARIOS['r15_sqlglot_dialect']()\n"
        "SCENARIOS['r12_parquet_logical_equality']()\n"
        "print('duckdb' in sys.modules)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "False"
