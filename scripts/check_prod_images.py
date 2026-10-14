#!/usr/bin/env python3
"""Refuse production compose images that are not pinned to a digest (#1337).

A tag such as ``:latest`` or ``:2.11.6-alpine`` can be moved; a redeploy then pulls
whatever it points at now, not the release that was approved. Only a reference that
ends in ``@sha256:<64 hex>`` names one image for good. Builder's image must also carry
a release tag (``:vX.Y.Z@sha256:...``, the release notes' ``Image:`` line), so a
``main`` build pinned by digest is still refused: only a release went through the
gates ``release.yml`` runs before it tags.

Two checks:

``--static`` reads the committed ``docker-compose.prod.app.yml`` without Docker. Every
``image:`` must be pinned by digest as written, through its ``${NAME:-default}``
default, or be ``${NAME:?message}`` — a variable compose refuses to start without.
A plain ``${NAME}`` or an empty default is refused: compose would start with an empty
or unpinned name. A unit test runs this on every change.

Without ``--static`` it asks ``docker compose config`` for the images the stack would
start with — the values in ``.env`` beside the compose file and the environment —
over every profile, prints each, and fails on one that is not pinned. An operator
runs this before ``up``; a missing ``BUILDER_IMAGE`` fails here with compose's own
message.

Usage:
    python3 scripts/check_prod_images.py             # the images `up` would use
    python3 scripts/check_prod_images.py --static    # the committed defaults
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.app.yml"

_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
_RELEASE = re.compile(r":v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?@sha256:[0-9a-f]{64}$")
#: Services whose image must be a Builder release, not any digest.
RELEASED_SERVICES = frozenset({"builder"})
_REQUIRED = re.compile(r"^\$\{\w+:\?[^}]*\}$")
_DEFAULT = re.compile(r"\$\{\w+:-([^${}]*)\}")


def unpinned(image: str, service: str = "") -> str | None:
    """Why ``image`` is not pinned, or None when it is."""
    if not image:
        return "an empty image name"
    if not _PINNED.search(image):
        return f"{image} is not pinned by digest (@sha256:...)"
    if service in RELEASED_SERVICES and not _RELEASE.search(image):
        return f"{image} is not a release (:vX.Y.Z@sha256:...)"
    return None


def static_problems(text: str) -> list[str]:
    """What is not pinned among the ``image:`` values of a compose file, as written."""
    services = (yaml.safe_load(text) or {}).get("services") or {}
    problems: list[str] = []
    for name, service in services.items():
        written = (service or {}).get("image")
        if written is None:
            continue
        written = str(written)
        if _REQUIRED.match(written):
            continue
        image = _DEFAULT.sub(lambda default: default.group(1), written)
        if "$" in image:
            problems.append(f"{name}: {written} has no pinned default and is not required")
            continue
        reason = unpinned(image, name)
        if reason:
            problems.append(f"{name}: {reason}")
    return problems


def resolved_images(compose_file: Path = COMPOSE_FILE) -> dict[str, str]:
    """The image of each service that names one, as ``docker compose config`` resolves it."""
    command = ["docker", "compose", "-f", str(compose_file), "--profile", "*", "config"]
    result = subprocess.run(
        [*command, "--format", "json"], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose config failed")
    services = json.loads(result.stdout).get("services") or {}
    return {name: str(service["image"]) for name, service in services.items() if "image" in service}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--static", action="store_true", help="check the committed file only")
    parser.add_argument("--compose", type=Path, default=COMPOSE_FILE, help="the compose file")
    args = parser.parse_args(argv)

    if args.static:
        problems = static_problems(args.compose.read_text(encoding="utf-8"))
    else:
        try:
            images = resolved_images(args.compose)
        except RuntimeError as error:
            print(f"::error::{error}")
            return 1
        problems = []
        for name, image in images.items():
            reason = unpinned(image, name)
            print(f"{'ok  ' if reason is None else 'FAIL'} {name}: {image}")
            if reason:
                problems.append(f"{name}: {reason}")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
