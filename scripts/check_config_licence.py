#!/usr/bin/env python3
"""Refuse to publish a legacy config that claims a licence its source did not grant (#758, #792).

A publishable config states the source's own terms: ``license: other`` with a
``license_name`` and a ``license_link``. A config whose source terms have not been
checked states no licence at all — ``scripts/pipeline/package.py`` then refuses to
package it, which is the safe outcome. What must not happen is the old default,
``cc-by-4.0``, on data whose source never granted it.

One exception is recorded here with its reason, so it cannot grow silently.

Usage:
    python scripts/check_config_licence.py                   # every config
    python scripts/check_config_licence.py path/to/config.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGS = REPO_ROOT / "scripts" / "configs"

#: Configs allowed to keep a licence that has not been checked, and why.
UNCONFIRMED: dict[str, str] = {}

ALLOWED_NAMES = frozenset(
    {"korea-public-data-unrestricted", "kogl-type-1", "kogl-type-3", "bok-ecos-attribution"}
)


def problems_for(path: Path) -> list[str]:
    """Why ``path`` may not be published; empty when it may, or when it claims nothing."""
    if path.name in UNCONFIRMED:
        return []
    card = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("card") or {}
    licence = card.get("license")
    if licence is None:
        return []  # claims nothing; packaging refuses it until the terms are recorded
    if licence != "other":
        return [f"license is {licence!r}; state the source's terms as license: other"]
    problems = []
    if card.get("license_name") not in ALLOWED_NAMES:
        problems.append(f"license_name {card.get('license_name')!r} is not a recorded term")
    if not str(card.get("license_link") or "").startswith("https://"):
        problems.append("license_link must link the source's terms over https")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    paths = [Path(a) for a in args] or sorted(CONFIGS.rglob("*.yaml"))
    failed = False
    for path in paths:
        for problem in problems_for(path):
            failed = True
            print(f"error: {path}: {problem}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
