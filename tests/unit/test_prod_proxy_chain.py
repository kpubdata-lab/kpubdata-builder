"""What each hop of the production proxy chain trusts (#1098).

Cloudflare -> Caddy -> Builder. Builder counts authentication failures per client
address, and behind a proxy that address comes from ``X-Forwarded-For``. These hold the
configuration: Caddy trusts Cloudflare's published ranges and nothing wider, Builder
trusts Caddy's one fixed address, and no other container can be given that address.
They do not start a container; ``scripts/proxy_chain_smoke.py`` runs the chain.
"""

from __future__ import annotations

import datetime
import importlib.util
import ipaddress
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from kpubdata_builder.service.auth_throttle import parse_trusted_proxies

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "docker-compose.prod.app.yml"
_CADDYFILE = _ROOT / "ops" / "caddy" / "Caddyfile"
_TRUSTED = _ROOT / "ops" / "caddy" / "trusted_proxies.caddy"
_DEFAULT = re.compile(r"\$\{(\w+):-([^${}]*)\}")


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ranges = _load_script("check_cloudflare_ranges")


def _default(value: str, name: str) -> str:
    """The default compose substitutes for ``name`` when .env does not set it."""
    found = {key: default for key, default in _DEFAULT.findall(value)}
    return found[name]


def _compose() -> dict[str, object]:
    loaded = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


class TestCompose:
    def test_builder_trusts_caddys_one_address_by_default(self) -> None:
        services = _compose()["services"]
        assert isinstance(services, dict)
        value = services["builder"]["environment"]["KPUBDATA_BUILDER_TRUSTED_PROXIES"]
        caddy_address = services["caddy"]["networks"]["app-net"]["ipv4_address"]

        # .env wins; otherwise the address Caddy has when .env does not move it.
        trusted = _default(value, "KPUBDATA_BUILDER_TRUSTED_PROXIES")
        assert trusted == _default(caddy_address, "CADDY_IPV4")
        networks = parse_trusted_proxies(trusted)
        assert [network.num_addresses for network in networks] == [1]

    def test_no_other_container_can_be_given_caddys_address(self) -> None:
        compose = _compose()
        services, networks = compose["services"], compose["networks"]
        assert isinstance(services, dict) and isinstance(networks, dict)
        (config,) = networks["app-net"]["ipam"]["config"]
        subnet = ipaddress.IPv4Network(_default(config["subnet"], "APP_NET_SUBNET"))
        dynamic = ipaddress.IPv4Network(_default(config["ip_range"], "APP_NET_IP_RANGE"))
        caddy = ipaddress.IPv4Address(
            _default(services["caddy"]["networks"]["app-net"]["ipv4_address"], "CADDY_IPV4")
        )

        assert caddy in subnet
        assert dynamic.subnet_of(subnet)
        # Containers without a fixed address are handed one from ip_range.
        assert caddy not in dynamic
        assert services["builder"]["networks"] == ["app-net"]

    def test_caddy_is_given_the_trusted_proxies_file(self) -> None:
        services = _compose()["services"]
        assert isinstance(services, dict)

        assert (
            "./ops/caddy/trusted_proxies.caddy:/etc/caddy/trusted_proxies.caddy:ro"
            in services["caddy"]["volumes"]
        )


class TestCaddy:
    def test_the_client_address_is_decided_once_and_rewritten(self) -> None:
        text = _CADDYFILE.read_text(encoding="utf-8")
        directives = [
            line.strip() for line in text.splitlines() if not line.strip().startswith("#")
        ]

        assert "import trusted_proxies.caddy" in directives
        # Without strict, Caddy takes the leftmost value: the one the client wrote.
        assert "trusted_proxies_strict" in directives
        assert "header_up X-Forwarded-For {http.vars.client_ip}" in directives

    def test_only_public_ranges_no_wider_than_cloudflares_are_trusted(self) -> None:
        listed = ranges.listed_ranges(_TRUSTED.read_text(encoding="utf-8"))

        assert listed
        assert [str(network) for network in listed if not network.is_global] == []
        # Cloudflare's widest published blocks are a /13 (IPv4) and a /29 (IPv6).
        too_wide = [
            str(network)
            for network in listed
            if network.prefixlen < (13 if network.version == 4 else 29)
        ]
        assert too_wide == []


class TestCloudflareRangeCheck:
    _FILE = "# 확인한 날: 2026-01-01\ntrusted_proxies static 198.51.100.0/24 2001:db8::/32\n"

    def test_a_matching_list_has_no_difference(self) -> None:
        published = ranges.parse_ranges("2001:db8::/32 198.51.100.0/24")

        assert ranges.difference(ranges.listed_ranges(self._FILE), published) == ([], [])

    def test_a_published_range_that_is_not_listed_and_a_listed_one_that_is_gone(self) -> None:
        published = ranges.parse_ranges("198.51.100.0/24 203.0.113.0/24")

        missing, extra = ranges.difference(ranges.listed_ranges(self._FILE), published)

        assert missing == ["203.0.113.0/24"]
        assert extra == ["2001:db8::/32"]

    def test_rewriting_replaces_the_list_and_the_date_and_keeps_the_comments(self) -> None:
        published = ranges.parse_ranges("203.0.113.0/24")

        text = ranges.rewritten(self._FILE, published, datetime.date(2026, 10, 10))

        assert text == "# 확인한 날: 2026-10-10\ntrusted_proxies static 203.0.113.0/24\n"

    def test_a_file_without_exactly_one_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="found 0"):
            ranges.listed_ranges("# nothing here\n")
        with pytest.raises(ValueError, match="found 2"):
            ranges.listed_ranges(self._FILE + self._FILE)

    def test_a_value_that_is_not_a_network_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ranges.listed_ranges("trusted_proxies static private_ranges\n")
