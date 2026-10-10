#!/usr/bin/env python3
"""Check that the production compose keeps /data across a restart (#1097).

Builder's state is files under ``/data``: the manifests, the build index and the other
SQLite stores. The deployment guide requires a volume that is local to the host and
outlives the container. This starts the real compose file with the Builder image,
writes a marker under ``/data`` as the service's own user, takes the stack down (the
container is removed, the volume is not), brings it up again and reads the marker back.

It checks the compose file's volume wiring, not the disk under it: whether a host's
Docker volumes sit on a local block device is the host's configuration.

Usage:
    python3 scripts/data_volume_smoke.py --image kpubdata-builder:ci
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.app.yml"
PROJECT = "data-volume-smoke"
MARKER = "/data/.data-volume-smoke"
SERVICE_KEY = "smoke-" + "k" * 40


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(command, check=check, capture_output=True, text=True)


def _up() -> bool:
    started = _compose("up", "-d", "--wait", "--no-build", "builder", check=False)
    if started.returncode != 0:
        print(started.stdout + started.stderr)
    return started.returncode == 0


def run() -> list[str]:
    token = uuid.uuid4().hex
    if not _up():
        return ["the stack did not start"]
    write = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
    written = _compose("exec", "-T", "builder", "python", "-c", write, MARKER, token, check=False)
    if written.returncode != 0:
        return [f"could not write under /data: {written.stderr.strip()[-300:]}"]

    # `down` without --volumes: the container goes, the named volume stays.
    _compose("down")
    if not _up():
        return ["the stack did not start again"]
    read = "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())"
    back = _compose("exec", "-T", "builder", "python", "-c", read, MARKER, check=False)
    got = back.stdout.strip()
    print(f"{'ok  ' if got == token else 'FAIL'} /data kept what was written before the restart")
    if got != token:
        return [f"/data did not keep the marker: {back.stderr.strip()[-300:] or 'another value'}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--image", required=True, help="Builder image to run")
    args = parser.parse_args()

    os.environ.update(
        BUILDER_IMAGE=args.image,
        KPUBDATA_BUILDER_API_KEY=SERVICE_KEY,
        # Builder's host port is not used here; let the host pick one.
        BUILDER_BIND="127.0.0.1:0",
    )
    try:
        problems = run()
    finally:
        _compose("down", "--volumes", "--remove-orphans", check=False)
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
