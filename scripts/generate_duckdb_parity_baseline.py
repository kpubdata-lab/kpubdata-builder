#!/usr/bin/env python3
"""Write the Polars parity baseline the DuckDB migration is checked against (#865).

Each scenario in `tests/parity/scenarios.py` runs the current engine on fixed inputs and
becomes one golden file, `tests/golden/duckdb_parity/<scenario>.json`. The comparison
lives in `tests/parity/test_duckdb_parity_baseline.py` and never writes: a baseline
changes only when someone runs this script and commits the diff, with the reason.

Every scenario runs twice and must give the same result both times; a scenario that
does not is refused, since a golden file that cannot be reproduced proves nothing.

Usage:
    python scripts/generate_duckdb_parity_baseline.py             # rewrite every file
    python scripts/generate_duckdb_parity_baseline.py r08 r11     # names starting so
    python scripts/generate_duckdb_parity_baseline.py --check     # exit 1 when stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(ROOT))
    from tests.parity.canonical import to_json
    from tests.parity.scenarios import GOLDEN, SCENARIOS

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prefixes", nargs="*", help="only scenarios whose name starts so")
    parser.add_argument("--check", action="store_true", help="compare instead of writing")
    args = parser.parse_args(argv)

    names = [
        name
        for name in SCENARIOS
        if not args.prefixes or any(name.startswith(p) for p in args.prefixes)
    ]
    if not names:
        print(f"no scenario matches {args.prefixes}", file=sys.stderr)
        return 2
    GOLDEN.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name in names:
        first = to_json(SCENARIOS[name]())
        second = to_json(SCENARIOS[name]())
        if first != second:
            print(f"{name}: two runs differ; not written", file=sys.stderr)
            return 1
        path = GOLDEN / f"{name}.json"
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if args.check:
            if current != first:
                stale.append(name)
        elif current != first:
            path.write_text(first, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("stale baselines: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
