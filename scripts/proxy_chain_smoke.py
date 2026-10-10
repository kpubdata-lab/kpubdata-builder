#!/usr/bin/env python3
"""Run the production proxy chain and try to forge a client address through it (#1098).

Builder throttles repeated authentication failures per client address. Behind a proxy
that address comes from ``X-Forwarded-For``, which anyone can write, so the chain is
only as good as what each hop trusts. This starts the real thing — the production
compose file with its Caddy profile, the repository's Caddyfile, and the Builder image —
and sends requests from two containers on the same network:

- ``edge`` stands in for Cloudflare: its address is the only one Caddy trusts here. The
  repository's ``trusted_proxies.caddy`` (Cloudflare's ranges) is replaced by a file
  naming that one address, because a CI runner cannot send from Cloudflare's network.
  Nothing else differs from a deployment.
- ``outsider`` is a client that reaches the origin without going through the edge.

What must hold, with the failure limit set to 3:

1. A client the edge names is refused after its own 3 failures, and another client the
   edge names is not.
2. Headers the outsider writes (``X-Forwarded-For``, ``CF-Connecting-IP``,
   ``Forwarded``) are not believed: its failures are counted against its own address,
   a different forged address does not get it a new allowance, and the client it named
   is not charged for them.
3. Reaching Builder's port directly, past Caddy, with a forged header changes nothing.
4. What a client wrote to the left of the address the edge appended is not believed.

Usage:
    python3 scripts/proxy_chain_smoke.py --image kpubdata-builder:ci
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.app.yml"
PROJECT = "proxy-chain-smoke"
NETWORK = "kpubdata-builder-app-net"
SERVICE_KEY = "smoke-" + "k" * 40
LIMIT = 3
VIA_CADDY = "http://caddy/version"
DIRECT = "http://builder:8000/version"

# Runs inside a container of the Builder image: one GET, print the status.
_CLIENT = (
    "import json,sys,urllib.request,urllib.error\n"
    "request=urllib.request.Request(sys.argv[1],headers=json.loads(sys.argv[2]))\n"
    "try:\n"
    "    print(urllib.request.urlopen(request,timeout=10).status)\n"
    "except urllib.error.HTTPError as error:\n"
    "    print(error.code)\n"
)

# Compose fills the three SMOKE_* values in from the environment this script sets.
# The two senders answer their health check at once: `up --wait` refuses a container
# that has none, and the image's own check asks a Builder that is not running in them.
_OVERRIDE = """\
services:
  caddy:
    # No host ports: the requests come from containers on the network.
    ports: !reset []
    volumes:
      - ${SMOKE_TRUSTED_FILE}:/etc/caddy/trusted_proxies.caddy:ro
  edge:
    image: ${BUILDER_IMAGE}
    entrypoint: ["sleep", "infinity"]
    healthcheck:
      test: ["CMD", "true"]
      interval: 2s
    networks:
      app-net:
        ipv4_address: ${SMOKE_EDGE_IPV4}
  outsider:
    image: ${BUILDER_IMAGE}
    entrypoint: ["sleep", "infinity"]
    healthcheck:
      test: ["CMD", "true"]
      interval: 2s
    networks:
      - app-net
"""


def _compose(override: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(override),
        "--profile",
        "caddy",
        *args,
    ]
    return subprocess.run(command, check=check, capture_output=True, text=True)


def _status(override: Path, sender: str, url: str, headers: dict[str, str]) -> int:
    result = _compose(
        override, "exec", "-T", sender, "python", "-c", _CLIENT, url, json.dumps(headers)
    )
    return int(result.stdout.strip())


def _subnet_default() -> ipaddress.IPv4Network:
    """The network's default subnet, read from the compose file this runs."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    marker = "${APP_NET_SUBNET:-"
    start = text.index(marker) + len(marker)
    return ipaddress.IPv4Network(text[start : text.index("}", start)])


def _wrong(forwarded_for: str | None = None, **extra: str) -> dict[str, str]:
    headers = {"X-API-Key": "wrong", **extra}
    if forwarded_for is not None:
        headers["X-Forwarded-For"] = forwarded_for
    return headers


def run(override: Path) -> list[str]:
    problems: list[str] = []

    def expect(label: str, got: int, want: int) -> None:
        print(f"{'ok  ' if got == want else 'FAIL'} {label}: {got} (expected {want})")
        if got != want:
            problems.append(f"{label}: got {got}, expected {want}")

    def fill(label: str, sender: str, url: str, headers: list[dict[str, str]]) -> None:
        """LIMIT failures are answered 401, the next request 429."""
        for index, each in enumerate(headers[:LIMIT], start=1):
            expect(f"{label} failure {index}", _status(override, sender, url, each), 401)
        expect(f"{label} over the limit", _status(override, sender, url, headers[LIMIT]), 429)

    expect(
        "the chain serves a request with the key",
        _status(
            override,
            "edge",
            VIA_CADDY,
            {"X-API-Key": SERVICE_KEY, "X-Forwarded-For": "203.0.113.10"},
        ),
        200,
    )

    # 1. Clients the edge names have their own allowances.
    fill("client 203.0.113.1 via the edge", "edge", VIA_CADDY, [_wrong("203.0.113.1")] * 4)
    expect(
        "client 203.0.113.2 via the edge is not refused with it",
        _status(override, "edge", VIA_CADDY, _wrong("203.0.113.2")),
        401,
    )

    # 2. The outsider names 203.0.113.2 as itself. Were that believed, the failure above
    #    would already be in that bucket and the third request here would be refused.
    forged = _wrong(
        "203.0.113.2",
        **{"CF-Connecting-IP": "203.0.113.2", "Forwarded": "for=203.0.113.2"},
    )
    fill("outsider forging 203.0.113.2", "outsider", VIA_CADDY, [forged] * 4)
    expect(
        "outsider forging another address gets no new allowance",
        _status(override, "outsider", VIA_CADDY, _wrong("198.51.100.7")),
        429,
    )
    expect(
        "client 203.0.113.2 was not charged for the outsider's failures",
        _status(override, "edge", VIA_CADDY, _wrong("203.0.113.2")),
        401,
    )

    # 3. Straight to Builder's port: the header is not read, the bucket is the same one.
    expect(
        "outsider reaching Builder directly with a forged header",
        _status(override, "outsider", DIRECT, _wrong("198.51.100.8")),
        429,
    )

    # 4. The edge appends the address it saw; what the client wrote before it is ignored.
    fill(
        "client 203.0.113.4 changing a forged prefix",
        "edge",
        VIA_CADDY,
        [_wrong(f"198.51.100.{n}, 203.0.113.4") for n in range(1, 5)],
    )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--image", required=True, help="Builder image to run")
    args = parser.parse_args()

    edge = str(_subnet_default().network_address + 3)
    os.environ.update(
        BUILDER_IMAGE=args.image,
        KPUBDATA_BUILDER_API_KEY=SERVICE_KEY,
        KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT=str(LIMIT),
        # A plain-http site named after the service: no certificate is requested.
        APP_DOMAIN="http://caddy",
        # Builder's host port is not used here; let the host pick one.
        BUILDER_BIND="127.0.0.1:0",
    )
    with tempfile.TemporaryDirectory(prefix="proxy-chain-smoke-") as directory:
        trusted = Path(directory) / "trusted_proxies.caddy"
        trusted.write_text(f"trusted_proxies static {edge}\n", encoding="utf-8")
        trusted.chmod(0o644)
        override = Path(directory) / "override.yml"
        override.write_text(_OVERRIDE, encoding="utf-8")
        os.environ.update(SMOKE_TRUSTED_FILE=str(trusted), SMOKE_EDGE_IPV4=edge)
        try:
            started = _compose(override, "up", "-d", "--wait", "--no-build", check=False)
            if started.returncode != 0:
                print(started.stdout + started.stderr)
                problems = ["the chain did not start"]
            else:
                problems = run(override)
            if problems:
                logs = _compose(override, "logs", "--no-color", "--tail", "60", check=False)
                print(logs.stdout + logs.stderr)
        finally:
            _compose(override, "down", "--volumes", "--remove-orphans", check=False)

    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
