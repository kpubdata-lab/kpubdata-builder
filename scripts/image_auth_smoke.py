#!/usr/bin/env python3
"""Start the Builder image under each authentication configuration (#1122).

docker-entrypoint.sh decides whether there is any authentication at all and whether a
service key looks usable; ``serve`` then checks the OIDC configuration. This runs the
real thing end to end, the way a deployment would, and asks the started service the
one question that tells the configurations apart: what does a request without
credentials get?

Cases:

- OIDC only → starts; ``/healthz`` 200; ``/builds`` without a token 401 "sign-in required".
- service key only → starts; ``/builds`` 401 without the key, 200 with it.
- both → starts; ``/builds`` 200 with the key.
- neither → refuses to start.
- an example service key → refuses to start, and the key is not in the output.
- OIDC without an audience → ``serve`` refuses to start.
- dev-mode with OIDC → ``serve`` refuses to start (dev-mode does not skip the check).

Usage:
    python scripts/image_auth_smoke.py --image kpubdata-builder:ci
    python scripts/image_auth_smoke.py --local   # docker-entrypoint.sh + installed serve
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRONG_KEY = "smoke-" + "k" * 40
ISSUER = "https://id.invalid/realms/kpubdata"
OIDC = {
    "OIDC_ISSUER": ISSUER,
    "OIDC_AUDIENCE": "kpubdata-builder",
    "KPUBDATA_BUILDER_ADMIN_SUBJECTS": f"{ISSUER}|smoke-admin",
}


@dataclass
class Case:
    name: str
    env: dict[str, str]
    starts: bool
    checks: list[tuple[dict[str, str], int, str]] = field(default_factory=list)
    """(request headers, expected status, text the body must contain) for GET /builds."""
    secret: str | None = None
    """A value that must not appear in the output."""


CASES = [
    Case("oidc-only", dict(OIDC), True, [({}, 401, "sign-in required")]),
    Case(
        "key-only",
        {"KPUBDATA_BUILDER_API_KEY": STRONG_KEY},
        True,
        [({}, 401, ""), ({"X-API-Key": STRONG_KEY}, 200, "")],
        secret=STRONG_KEY,
    ),
    Case(
        "both",
        {**OIDC, "KPUBDATA_BUILDER_API_KEY": STRONG_KEY},
        True,
        [({"X-API-Key": STRONG_KEY}, 200, "")],
        secret=STRONG_KEY,
    ),
    Case("neither", {}, False),
    Case(
        "example-key",
        {"KPUBDATA_BUILDER_API_KEY": "replace-with-strong-random-api-key"},
        False,
        secret="replace-with-strong-random-api-key",
    ),
    Case("oidc-without-audience", {"OIDC_ISSUER": ISSUER}, False),
    Case("dev-mode-with-oidc", {**OIDC, "KPUBDATA_BUILDER_DEV_MODE": "1"}, False),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


@dataclass
class Running:
    poll: Callable[[], int | None]
    output: Callable[[], str]
    stop: Callable[[], None]


def _start_docker(image: str, env: dict[str, str], port: int) -> Running:
    args = ["docker", "run", "-d", "-p", f"127.0.0.1:{port}:8000"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    container = subprocess.run(
        [*args, image], check=True, capture_output=True, text=True
    ).stdout.strip()

    def poll() -> int | None:
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", container],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        return None if state[0] == "true" else int(state[1])

    def output() -> str:
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
        return logs.stdout + logs.stderr

    def stop() -> None:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)

    return Running(poll, output, stop)


def _start_local(env: dict[str, str], port: int) -> Running:
    data = tempfile.mkdtemp(prefix="image-auth-smoke-")
    log = Path(data) / "out.log"
    controlled = {"KPUBDATA_BUILDER_API_KEY", "KPUBDATA_BUILDER_DEV_MODE", *OIDC}
    full_env = {k: v for k, v in os.environ.items() if k not in controlled}
    full_env.update(
        env,
        KPUBDATA_BUILDER_HOST="127.0.0.1",
        KPUBDATA_BUILDER_PORT=str(port),
        KPUBDATA_BUILDER_OUTPUT_DIR=data,
    )
    handle = log.open("wb")
    process = subprocess.Popen(
        ["sh", str(ROOT / "docker-entrypoint.sh")], env=full_env, stdout=handle, stderr=handle
    )

    def stop() -> None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        handle.close()

    return Running(process.poll, lambda: log.read_text("utf-8", "replace"), stop)


def run_case(case: Case, start: Callable[[dict[str, str], int], Running]) -> list[str]:
    port = _free_port()
    running = start(case.env, port)
    base = f"http://127.0.0.1:{port}"
    problems: list[str] = []
    try:
        healthy = False
        for _ in range(60):
            if running.poll() is not None:
                break
            try:
                healthy = _get(f"{base}/healthz", {})[0] == 200
            except OSError:
                healthy = False
            if healthy:
                break
            time.sleep(0.5)
        if case.starts and not healthy:
            problems.append(f"did not start: {running.output()[-600:]}")
        if not case.starts and (healthy or running.poll() in (None, 0)):
            problems.append("started, but should have refused")
        for headers, status, text in case.checks if healthy else []:
            got, body = _get(f"{base}/builds", headers)
            if got != status or text not in body:
                problems.append(f"GET /builds {sorted(headers)} -> {got} {body[:120]!r}")
    finally:
        running.stop()
    if case.secret and case.secret in running.output():
        problems.append("the service key appears in the output")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image", help="docker image to run")
    mode.add_argument("--local", action="store_true", help="run docker-entrypoint.sh here")
    args = parser.parse_args()

    def start(env: dict[str, str], port: int) -> Running:
        if args.local:
            return _start_local(env, port)
        return _start_docker(args.image, env, port)

    failed = 0
    for case in CASES:
        problems = run_case(case, start)
        print(f"{'ok  ' if not problems else 'FAIL'} {case.name}")
        for problem in problems:
            print(f"     {problem}")
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
