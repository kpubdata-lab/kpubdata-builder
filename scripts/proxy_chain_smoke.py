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

The same chain is asked for its response headers (#1107). Caddy serves the site twice
here: over plain HTTP as ``http://caddy``, and over TLS as ``caddy.localhost`` with a
certificate from Caddy's own local authority, which the client does not verify.

5. A response through Caddy carries each security header once, with the value the
   Caddyfile sets, whether Builder answered 200 or 401.
6. ``Strict-Transport-Security`` is on the TLS response, without ``includeSubDomains``
   or ``preload``, and is not on the plain-HTTP one (RFC 6797 section 7.2).
7. With ``--browser``, a real browser refuses to show the response in a frame of
   another page, and does show a response that did not come through Caddy.

Usage:
    python3 scripts/proxy_chain_smoke.py --image kpubdata-builder:ci
    python3 scripts/proxy_chain_smoke.py --image kpubdata-builder:ci --browser google-chrome
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.app.yml"
PROJECT = "proxy-chain-smoke"
NETWORK = "kpubdata-builder-app-net"
SERVICE_KEY = "smoke-" + "k" * 40
LIMIT = 3
VIA_CADDY = "http://caddy/version"
DIRECT = "http://builder:8000/version"
#: The name Caddy serves over TLS in this run. ``.localhost`` names get a certificate
#: from Caddy's local authority; no public one is requested.
TLS_HOST = "caddy.localhost"
VIA_CADDY_TLS = f"https://{TLS_HOST}/version"

#: What every response through Caddy must carry, once (ops/caddy/Caddyfile).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
HSTS = "Strict-Transport-Security"
#: A year, and nothing else: no ``includeSubDomains``, no ``preload`` (#1107).
HSTS_VALUE = "max-age=31536000"

# Runs inside a container of the Builder image: one GET, print the status and every
# header line as JSON. The certificate is Caddy's own local authority's, so it is not
# verified: what is read here is the headers, not who signed the certificate.
_CLIENT = (
    "import json,ssl,sys,urllib.request,urllib.error\n"
    "request=urllib.request.Request(sys.argv[1],headers=json.loads(sys.argv[2]))\n"
    "context=ssl._create_unverified_context()\n"
    "try:\n"
    "    answer=urllib.request.urlopen(request,timeout=10,context=context)\n"
    "except urllib.error.HTTPError as error:\n"
    "    answer=error\n"
    "print(json.dumps({'status':answer.status,'headers':answer.headers.items()}))\n"
)

# A page of another origin (a file) that frames two addresses.
_FRAMING_PAGE = """\
<!doctype html>
<title>frame check</title>
<iframe src="{guarded}"></iframe>
<iframe src="{unguarded}"></iframe>
"""

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
    networks:
      app-net:
        # The name the TLS site is asked for by.
        aliases:
          - ${SMOKE_TLS_HOST}
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


def _get(
    override: Path, sender: str, url: str, headers: dict[str, str]
) -> tuple[int, list[tuple[str, str]]]:
    """The status and every header line of one GET sent from ``sender``."""
    result = _compose(
        override, "exec", "-T", sender, "python", "-c", _CLIENT, url, json.dumps(headers)
    )
    answer = json.loads(result.stdout)
    return int(answer["status"]), [(name, value) for name, value in answer["headers"]]


def _status(override: Path, sender: str, url: str, headers: dict[str, str]) -> int:
    return _get(override, sender, url, headers)[0]


def header_problems(label: str, headers: list[tuple[str, str]], *, over_tls: bool) -> list[str]:
    """What is wrong with one response's security headers; empty when nothing is.

    Each header must be there once: a browser reads the first
    ``Strict-Transport-Security`` and ignores the rest, and two different
    ``Content-Security-Policy`` lines are both enforced.
    """
    problems: list[str] = []
    expected = dict(SECURITY_HEADERS)
    if over_tls:
        expected[HSTS] = HSTS_VALUE
    for name, want in expected.items():
        got = [value for key, value in headers if key.lower() == name.lower()]
        if got != [want]:
            problems.append(f"{label}: {name} is {got}, expected [{want!r}]")
    if not over_tls:
        sent = [value for key, value in headers if key.lower() == HSTS.lower()]
        if sent:
            problems.append(f"{label}: {HSTS} {sent} was sent over plain HTTP")
    return problems


def refused_frames(log: str, urls: list[str]) -> list[str]:
    """The ``urls`` a browser's log says it refused to show in a frame.

    Chrome writes one console line per refused frame, naming the frame's origin and
    the ``frame-ancestors`` directive that refused it. The wording around them has
    changed between versions, so only those two are looked for.
    """
    lines = [line for line in log.splitlines() if "frame-ancestors" in line]
    refused = []
    for url in urls:
        origin = re.match(r"https?://[^/]+", url)
        if origin and any(origin.group(0) in line for line in lines):
            refused.append(url)
    return refused


def _address(container: str) -> str:
    """The container's address on the smoke's network, as the host reaches it."""
    template = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    result = subprocess.run(
        ["docker", "inspect", "-f", template, container],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def frame_check(browser: str, directory: Path) -> list[str]:
    """Open a page that frames the chain's answer in a real browser on this host.

    The page is a local file, so it is another origin to both frames. One frame asks
    for Builder's ``/healthz`` through Caddy and must be refused; the other asks
    Builder's own port for the same path, which sends no such header, and must not be
    — without it, a browser that refused every frame would pass. That second address
    is first fetched from this host, so "not refused" is said of a frame that could
    load.
    """
    caddy, builder = _address("kpubdata-builder-caddy"), _address("kpubdata-builder")
    guarded = "http://caddy/healthz"
    unguarded = f"http://{builder}:8000/healthz"
    try:
        reached = urllib.request.urlopen(unguarded, timeout=10).status
    except OSError as error:
        return [f"this host cannot reach {unguarded}: {error}"]
    print(f"{'ok  ' if reached == 200 else 'FAIL'} this host reaches {unguarded}: {reached}")
    page = directory / "framing.html"
    page.write_text(_FRAMING_PAGE.format(guarded=guarded, unguarded=unguarded), encoding="utf-8")
    result = subprocess.run(
        [
            browser,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={directory / 'browser-profile'}",
            # The site is named `caddy`; the host does not resolve the network's names.
            f"--host-resolver-rules=MAP caddy {caddy}",
            "--enable-logging=stderr",
            "--v=0",
            "--virtual-time-budget=10000",
            "--dump-dom",
            page.as_uri(),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    refused = refused_frames(result.stderr, [guarded, unguarded])
    print(f"{'ok  ' if guarded in refused else 'FAIL'} the browser refuses to frame {guarded}")
    print(f"{'ok  ' if unguarded not in refused else 'FAIL'} the browser frames {unguarded}")
    problems = []
    if guarded not in refused:
        problems.append(f"the browser did not refuse to frame {guarded}")
    if unguarded in refused:
        problems.append(f"the browser refused to frame {unguarded}, which sends no header")
    if problems:
        print(f"browser exit status {result.returncode}; its log:\n{result.stderr[-4000:]}")
    return problems


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

    # 5 and 6. The security headers, on an answer Builder gave with 200 and with 401,
    #    over TLS and over plain HTTP. The 401s name a client of their own so that they
    #    are counted against nobody above.
    with_key = {"X-API-Key": SERVICE_KEY, "X-Forwarded-For": "203.0.113.10"}
    for label, url, over_tls in (
        ("over TLS", VIA_CADDY_TLS, True),
        ("over plain HTTP", VIA_CADDY, False),
    ):
        for want, sent in ((200, with_key), (401, _wrong("203.0.113.50"))):
            status, headers = _get(override, "edge", url, sent)
            expect(f"a {want} {label} is answered", status, want)
            found = header_problems(f"the {want} {label}", headers, over_tls=over_tls)
            print(f"{'FAIL' if found else 'ok  '} the {want} {label} carries the security headers")
            problems.extend(found)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--image", required=True, help="Builder image to run")
    parser.add_argument(
        "--browser",
        help="a Chrome or Chromium on this host; with it, the frame refusal is checked in it",
    )
    args = parser.parse_args()

    edge = str(_subnet_default().network_address + 3)
    os.environ.update(
        BUILDER_IMAGE=args.image,
        KPUBDATA_BUILDER_API_KEY=SERVICE_KEY,
        KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT=str(LIMIT),
        # The site under two names: plain HTTP named after the service, and a
        # `.localhost` name that Caddy serves over TLS with its own local authority's
        # certificate. No public certificate is requested.
        APP_DOMAIN=f"http://caddy, {TLS_HOST}",
        SMOKE_TLS_HOST=TLS_HOST,
        # Builder's host port is not used here; let the host pick one.
        BUILDER_BIND="127.0.0.1:0",
    )
    # The browser's profile may still be written to when the directory is removed.
    with tempfile.TemporaryDirectory(
        prefix="proxy-chain-smoke-", ignore_cleanup_errors=True
    ) as directory:
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
                if args.browser:
                    problems += frame_check(args.browser, Path(directory))
                else:
                    print("note the frame refusal was not checked in a browser (no --browser)")
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
