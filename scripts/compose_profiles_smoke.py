#!/usr/bin/env python3
"""Start each profile of the production compose and make one call through it (#692).

``docker-compose.prod.app.yml`` is the one bundle a deployment starts from: Builder on
its own, and the opt-in profiles ``caddy`` (the TLS entry) and ``studio`` (the web
screens). A profile nobody starts can point at an image that does not exist, or at a
service that never becomes healthy, and nobody hears of it until an operator follows
the guide (#675: the CUBRID stack asked for a ``:cubrid`` image no workflow published).

Two checks, run separately:

``--image IMAGE`` starts the bundle once per profile, with that Builder image, waits
for every service's health check, and then asks, from inside the network:

- ``default``: Builder's ``/healthz`` answers 200, ``/version`` answers 200 with the
  service key and 401 without it, and ``/readyz`` answers without the key (#1101): ready,
  or not ready only because the runner's disk is below the default threshold.
- ``caddy``: Caddy's own ``/healthz`` answers 200, and ``/version`` and ``/readyz``
  through Caddy reach Builder. The forged-header, security-header and log checks of the same chain
  are ``proxy_chain_smoke.py``'s and are not repeated here.
- ``studio``: the page and ``/config.js`` are served, ``/config.js`` names the Builder
  address the stack was given, and the call the browser makes to that address — with
  the page's origin — answers 200 and is allowed by CORS. Ask KPubData is off by
  default: ``assistantEnabled`` is ``"false"`` and the page's Content-Security-Policy
  ``connect-src`` holds no bare ``https:`` (kpubdata-studio#933).

``--published`` asks the registry for every image the deployment compose files name
(``image_rescan.deployed_images``, the same list the scheduled rescan reads) and fails
on one that is not there. A private image needs a login first; CI logs in with a token
that may only read packages.

The Studio image is built from the commit the compose file pins, since Studio does not
publish one; it names no ``image:``, so it is not in the ``--published`` list.

Usage:
    python3 scripts/compose_profiles_smoke.py --image kpubdata-builder:ci
    python3 scripts/compose_profiles_smoke.py --image kpubdata-builder:ci --profile studio
    python3 scripts/compose_profiles_smoke.py --published
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.app.yml"
PROJECT = "compose-profiles-smoke"
SERVICE_KEY = "smoke-" + "k" * 40
#: ``default`` is the stack started without ``--profile``.
PROFILES = ("default", "caddy", "studio")
#: The Builder address Studio is given, as a container on the network reaches it.
BUILDER_URL = "http://builder:8000"
#: The origin a browser loads Studio from on the VM, through STUDIO_BIND's default.
STUDIO_ORIGIN = "http://localhost:8080"

# Caddy's host ports are dropped: the requests come from inside the network, and a host
# that already serves 80 or 443 could not start the profile.
_OVERRIDE = """\
services:
  caddy:
    ports: !reset []
"""

# Runs inside the Builder container: one GET, print the status, the headers and the
# start of the body as JSON.
_CLIENT = (
    "import json,sys,urllib.request,urllib.error\n"
    "request=urllib.request.Request(sys.argv[1],headers=json.loads(sys.argv[2]))\n"
    "try:\n"
    "    answer=urllib.request.urlopen(request,timeout=10)\n"
    "except urllib.error.HTTPError as error:\n"
    "    answer=error\n"
    "body=answer.read(4096).decode('utf-8','replace')\n"
    "print(json.dumps({'status':answer.status,'headers':dict(answer.headers.items()),"
    "'body':body}))\n"
)


class Answer:
    def __init__(self, status: int, headers: dict[str, str], body: str) -> None:
        self.status = status
        self.headers = {name.lower(): value for name, value in headers.items()}
        self.body = body


def _compose(
    override: Path, profile: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), "-f", str(override)]
    if profile != "default":
        command += ["--profile", profile]
    return subprocess.run([*command, *args], check=check, capture_output=True, text=True)


def compose_profiles() -> set[str]:
    """The profiles the compose file declares, as compose reads them."""
    listed = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--profiles"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in listed.stdout.splitlines() if line.strip()}


def _get(override: Path, profile: str, url: str, headers: dict[str, str] | None = None) -> Answer:
    result = _compose(
        override,
        profile,
        "exec",
        "-T",
        "builder",
        "python",
        "-c",
        _CLIENT,
        url,
        json.dumps(headers or {}),
    )
    answer = json.loads(result.stdout)
    return Answer(int(answer["status"]), answer["headers"], answer["body"])


def _expect(problems: list[str], profile: str, what: str, ok: bool) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} [{profile}] {what}")
    if not ok:
        problems.append(f"[{profile}] {what}")


def assistant_off(config: str, headers: dict[str, str]) -> bool:
    """True when ``/config.js`` and the page's CSP say Ask KPubData is off."""
    policy = {key.lower(): value for key, value in headers.items()}.get(
        "content-security-policy", ""
    )
    connect = [
        directive.split()[1:]
        for directive in policy.split(";")
        if directive.strip().startswith("connect-src")
    ]
    return '"assistantEnabled": "false"' in config and bool(connect) and "https:" not in connect[0]


def readiness_ok(answer: Answer) -> bool:
    """``/readyz`` answered, unauthenticated, as a ready service does (#1101).

    A 503 whose only failed check is ``disk: low`` passes: the threshold is a share of
    the disk the stack runs on, which is the CI runner's, not the deployment's.
    """
    try:
        body = json.loads(answer.body)
    except ValueError:
        return False
    if not isinstance(body, dict) or not isinstance(body.get("checks"), dict):
        return False
    failed = {name: state for name, state in body["checks"].items() if state != "ok"}
    if answer.status == 200:
        return body.get("status") == "ready" and not failed
    return answer.status == 503 and body.get("status") == "not_ready" and failed == {"disk": "low"}


def check_profile(override: Path, profile: str) -> list[str]:
    problems: list[str] = []
    key = {"X-API-Key": SERVICE_KEY}

    health = _get(override, profile, f"{BUILDER_URL}/healthz")
    _expect(problems, profile, "Builder /healthz answers 200", health.status == 200)
    version = _get(override, profile, f"{BUILDER_URL}/version", key)
    _expect(problems, profile, "Builder /version answers 200 with the key", version.status == 200)
    refused = _get(override, profile, f"{BUILDER_URL}/version")
    _expect(problems, profile, "Builder /version answers 401 without it", refused.status == 401)
    ready = _get(override, profile, f"{BUILDER_URL}/readyz")
    _expect(problems, profile, "Builder /readyz answers without the key", readiness_ok(ready))

    if profile == "caddy":
        own = _get(override, profile, "http://caddy/healthz")
        _expect(problems, profile, "Caddy /healthz answers 200", own.status == 200)
        through = _get(override, profile, "http://caddy/version", key)
        _expect(
            problems,
            profile,
            "/version through Caddy reaches Builder",
            through.status == 200 and through.body == version.body,
        )
        ready_through = _get(override, profile, "http://caddy/readyz")
        _expect(
            problems, profile, "/readyz through Caddy reaches Builder", readiness_ok(ready_through)
        )

    if profile == "studio":
        page = _get(override, profile, "http://studio:8080/")
        _expect(
            problems,
            profile,
            "Studio serves its page",
            page.status == 200 and 'id="root"' in page.body,
        )
        config = _get(override, profile, "http://studio:8080/config.js")
        _expect(
            problems,
            profile,
            "Studio's /config.js names the Builder address it was given",
            config.status == 200 and f'"builderApiUrl": "{BUILDER_URL}"' in config.body,
        )
        _expect(
            problems,
            profile,
            "Ask KPubData is off by default (assistantEnabled false, no https: in connect-src)",
            assistant_off(config.body, page.headers),
        )
        call = _get(override, profile, f"{BUILDER_URL}/version", {**key, "Origin": STUDIO_ORIGIN})
        _expect(
            problems,
            profile,
            "the call the page makes to Builder answers 200 and CORS allows its origin",
            call.status == 200 and call.headers.get("access-control-allow-origin") == STUDIO_ORIGIN,
        )
    return problems


def run_profile(override: Path, profile: str) -> list[str]:
    print(f"--- profile {profile}")
    # The Builder image is the one given, never rebuilt here: only Studio is built, from
    # the commit the compose file pins.
    started = subprocess.CompletedProcess[str]([], 0, "", "")
    if profile == "studio":
        started = _compose(override, profile, "build", "studio", check=False)
    if started.returncode == 0:
        started = _compose(override, profile, "up", "-d", "--wait", "--no-build", check=False)
    try:
        if started.returncode != 0:
            print(started.stdout + started.stderr)
            logs = _compose(override, profile, "logs", "--tail", "40", check=False)
            print(logs.stdout + logs.stderr)
            return [f"[{profile}] the stack did not start and become healthy"]
        return check_profile(override, profile)
    finally:
        _compose(override, profile, "down", "--volumes", "--remove-orphans", check=False)


def published_images() -> list[str]:
    """What ``image_rescan.py`` beside this file reads: the images a deployment pulls."""
    path = Path(__file__).with_name("image_rescan.py")
    spec = importlib.util.spec_from_file_location("image_rescan", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [str(image) for image in module.deployed_images()]


def missing_images(images: list[str], inspect: list[str] | None = None) -> list[str]:
    """The images the registry does not have (or will not show to this login)."""
    command = inspect or ["docker", "manifest", "inspect"]
    missing: list[str] = []
    for image in images:
        found = subprocess.run([*command, image], capture_output=True, text=True)
        print(f"{'ok  ' if found.returncode == 0 else 'FAIL'} {image} is published")
        if found.returncode != 0:
            print(f"     {found.stderr.strip()[-300:]}")
            missing.append(image)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image", help="Builder image to start each profile with")
    mode.add_argument(
        "--published",
        action="store_true",
        help="check that every image the compose files name can be pulled",
    )
    parser.add_argument(
        "--profile",
        action="append",
        choices=PROFILES,
        help="a profile to start (repeatable; all of them when left out)",
    )
    args = parser.parse_args()

    problems: list[str] = []
    if args.published:
        missing = missing_images(published_images())
        problems = [f"{image} is not published" for image in missing]
    else:
        declared = compose_profiles()
        unknown = declared - set(PROFILES)
        if unknown:
            problems.append(f"profiles this smoke does not start: {sorted(unknown)}")
        os.environ.update(
            BUILDER_IMAGE=args.image,
            KPUBDATA_BUILDER_API_KEY=SERVICE_KEY,
            KPUBDATA_BUILDER_ALLOWED_ORIGINS=STUDIO_ORIGIN,
            STUDIO_BUILDER_API_URL=BUILDER_URL,
            # Caddy serves plain HTTP under the name the requests use; TLS is
            # proxy_chain_smoke.py's.
            APP_DOMAIN="http://caddy",
            # No host port is used here; let the host pick them.
            BUILDER_BIND="127.0.0.1:0",
            STUDIO_BIND="127.0.0.1:0",
        )
        with tempfile.TemporaryDirectory() as scratch:
            override = Path(scratch) / "override.yml"
            override.write_text(_OVERRIDE, encoding="utf-8")
            for profile in args.profile or PROFILES:
                problems += run_profile(override, profile)

    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
