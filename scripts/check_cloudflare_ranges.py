#!/usr/bin/env python3
"""Compare Caddy's trusted proxies with the ranges Cloudflare publishes (#1098).

``ops/caddy/trusted_proxies.caddy`` lists the peers whose ``X-Forwarded-For`` Caddy
reads. It must be Cloudflare's edge ranges and nothing else: a range missing from it
makes every user behind that edge share one throttle bucket, and a range that is no
longer Cloudflare's lets whoever holds it now name their own client address.

Exit status 0 when the file matches what Cloudflare publishes, 1 when it differs (the
difference is printed), 2 when the published lists could not be read.

Usage:
    python3 scripts/check_cloudflare_ranges.py            # compare
    python3 scripts/check_cloudflare_ranges.py --write    # rewrite the file's list
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRUSTED_PROXIES = ROOT / "ops" / "caddy" / "trusted_proxies.caddy"
SOURCES = ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6")
_DIRECTIVE = "trusted_proxies static"
_CHECKED_ON = re.compile(r"^# 확인한 날: .*$", re.MULTILINE)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_ranges(text: str) -> list[Network]:
    """Networks in a whitespace-separated list; a value that is not one raises."""
    return [ipaddress.ip_network(value) for value in text.split()]


def listed_ranges(caddy_text: str) -> list[Network]:
    """The networks on the file's one ``trusted_proxies static`` line."""
    lines = [
        line.strip() for line in caddy_text.splitlines() if line.strip().startswith(_DIRECTIVE)
    ]
    if len(lines) != 1:
        raise ValueError(f"expected one '{_DIRECTIVE}' line, found {len(lines)}")
    return parse_ranges(lines[0][len(_DIRECTIVE) :])


def difference(listed: list[Network], published: list[Network]) -> tuple[list[str], list[str]]:
    """(published but not listed, listed but not published), each sorted."""
    missing = sorted(str(network) for network in set(published) - set(listed))
    extra = sorted(str(network) for network in set(listed) - set(published))
    return missing, extra


def rewritten(caddy_text: str, published: list[Network], today: datetime.date) -> str:
    """The file with its list replaced by ``published``, in the order published."""
    line = f"{_DIRECTIVE} " + " ".join(str(network) for network in published)
    out = [
        line if existing.strip().startswith(_DIRECTIVE) else existing
        for existing in caddy_text.splitlines()
    ]
    text = "\n".join(out) + "\n"
    return _CHECKED_ON.sub(f"# 확인한 날: {today.isoformat()}", text)


def fetch_published() -> list[Network]:
    published: list[Network] = []
    for url in SOURCES:
        request = urllib.request.Request(url, headers={"User-Agent": "kpubdata-builder"})
        with urllib.request.urlopen(request, timeout=20) as response:
            published += parse_ranges(response.read().decode("ascii"))
    return published


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the file's list")
    args = parser.parse_args()

    try:
        published = fetch_published()
    except (OSError, ValueError) as error:
        print(f"could not read Cloudflare's published ranges: {error}", file=sys.stderr)
        return 2
    if not published:
        print("Cloudflare's published lists are empty", file=sys.stderr)
        return 2

    text = TRUSTED_PROXIES.read_text(encoding="utf-8")
    missing, extra = difference(listed_ranges(text), published)
    if not missing and not extra:
        print(f"{TRUSTED_PROXIES.relative_to(ROOT)}: matches {len(published)} published ranges")
        return 0
    for network in missing:
        print(f"published by Cloudflare, not listed: {network}")
    for network in extra:
        print(f"listed, no longer published by Cloudflare: {network}")
    if args.write:
        TRUSTED_PROXIES.write_text(
            rewritten(text, published, datetime.date.today()), encoding="utf-8"
        )
        print(f"rewrote {TRUSTED_PROXIES.relative_to(ROOT)}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
