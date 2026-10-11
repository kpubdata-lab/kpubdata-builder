"""What each hop of the production proxy chain trusts (#1098).

Cloudflare -> Caddy -> Builder. Builder counts authentication failures per client
address, and behind a proxy that address comes from ``X-Forwarded-For``. These hold the
configuration: Caddy trusts Cloudflare's published ranges and nothing wider, Builder
trusts Caddy's one fixed address, and no other container can be given that address.
They do not start a container; ``scripts/proxy_chain_smoke.py`` runs the chain.

And what the chain writes down (#1100): Caddy logs each request with every header and
query value removed, and both containers' logs are bounded in size. Whether Caddy
accepts the configuration and what its lines then hold is the smoke script's to show;
here are the configuration as text and the check the smoke script reads the log with.
"""

from __future__ import annotations

import datetime
import importlib.util
import ipaddress
import json
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
smoke = _load_script("proxy_chain_smoke")


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


def _directives() -> list[str]:
    """The Caddyfile's lines that are not comments, without their indentation."""
    text = _CADDYFILE.read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if not line.strip().startswith("#")]


def _block(directives: list[str], opening: str) -> list[str]:
    """The lines between ``opening`` and the brace that closes it."""
    start = directives.index(opening)
    depth, inside = 1, []
    for line in directives[start + 1 :]:
        depth += line.endswith("{") - (line == "}")
        if depth == 0:
            return inside
        inside.append(line)
    raise AssertionError(f"{opening!r} is never closed")


class TestLogRotation:
    """Docker keeps a container's stderr in a file that grows without limit by default."""

    _SIZE = re.compile(r"[1-9]\d*[km]")

    def test_every_service_has_a_bounded_json_file_log(self) -> None:
        services = _compose()["services"]
        assert isinstance(services, dict)

        assert set(services) >= {"builder", "caddy"}
        for name, service in services.items():
            logging = service.get("logging")
            assert logging, f"{name} has no logging section: its log grows without limit"
            assert logging["driver"] == "json-file"
            options = logging["options"]
            assert set(options) == {"max-size", "max-file"}
            size = _default(options["max-size"], "LOG_MAX_SIZE")
            files = _default(options["max-file"], "LOG_MAX_FILE")
            assert self._SIZE.fullmatch(size), f"{name}: {size!r} is not a size Docker reads"
            # One file would mean the whole log is thrown away each time it fills.
            assert int(files) >= 2

    def test_the_default_bound_is_what_the_guide_says(self) -> None:
        services = _compose()["services"]
        assert isinstance(services, dict)
        options = services["builder"]["logging"]["options"]
        size = _default(options["max-size"], "LOG_MAX_SIZE")
        files = _default(options["max-file"], "LOG_MAX_FILE")
        assert size.endswith("m")
        total = int(size[:-1]) * int(files)
        guide = (_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
        start = guide.index("### 로그의 보관과 열람")
        section = guide[start : guide.index("\n## ", start)]

        assert f"`{size}`" in section and f"`{files}`" in section
        assert f"{total}MB" in section

    def test_the_two_services_share_the_bound(self) -> None:
        services = _compose()["services"]
        assert isinstance(services, dict)

        assert services["builder"]["logging"] == services["caddy"]["logging"]


class TestCaddyLog:
    def test_every_line_caddy_writes_has_headers_and_query_removed(self) -> None:
        directives = _directives()
        options = _block(directives, "{")
        default_logger = _block(options, "log default {")
        fields = _block(default_logger, "fields {")

        assert "wrap json" in _block(default_logger, "format filter {")
        # Whole sections, not names picked out of them: a header nobody listed is
        # removed with the rest.
        assert "request>headers delete" in fields
        assert "resp_headers delete" in fields
        assert f'request>uri regexp "[?].*" "{smoke.QUERY_REMOVED}"' in fields

    def test_the_site_logs_through_that_logger(self) -> None:
        directives = _directives()
        site = _block(directives, "{$APP_DOMAIN} {")

        # A bare ``log``: with an output or a format of its own the site's lines would
        # leave through a logger the filter above is not on.
        assert "log" in site
        assert not any(line.startswith("log ") for line in site)

    def test_nothing_turns_credential_logging_on(self) -> None:
        assert not any("log_credentials" in line for line in _directives())


class TestAccessLogCheck:
    """The check the smoke script reads Caddy's log with, on lines written by hand."""

    _FORBIDDEN = {"X-API-Key": "marker-in-a-header", "query value": "marker-in-a-query"}
    _PATH = "/access-log-probe"

    @staticmethod
    def _line(
        uri: str,
        logger: str = "http.log.access",
        request: dict[str, object] | None = None,
        **extra: object,
    ) -> str:
        described = {"method": "GET", "host": "caddy", "uri": uri, **(request or {})}
        return json.dumps({"level": "info", "logger": logger, "request": described, **extra})

    def _clean(self) -> list[str]:
        return [
            '{"level":"info","logger":"tls","msg":"cleaning storage unit"}',
            "not json at all",
            self._line("/version", status=200),
            self._line(f"{self._PATH}{smoke.QUERY_REMOVED}", status=401),
        ]

    def test_a_filtered_log_has_no_problem(self) -> None:
        log = "\n".join(self._clean())

        assert smoke.access_log_problems(log, self._FORBIDDEN, self._PATH) == []

    def test_a_marker_anywhere_is_a_problem_named_by_its_place_not_its_value(self) -> None:
        log = "\n".join([*self._clean(), "plain text with marker-in-a-header in it"])

        problems = smoke.access_log_problems(log, self._FORBIDDEN, self._PATH)

        assert problems == ["the value sent as X-API-Key is in Caddy's log"]

    def test_request_headers_are_a_problem_even_without_a_marker(self) -> None:
        log = "\n".join([*self._clean(), self._line("/version", request={"headers": {}})])

        assert smoke.access_log_problems(log, self._FORBIDDEN, self._PATH) == [
            "line 5 holds the request headers"
        ]

    def test_response_headers_are_a_problem(self) -> None:
        log = "\n".join([*self._clean(), self._line("/version", resp_headers={})])

        assert smoke.access_log_problems(log, self._FORBIDDEN, self._PATH) == [
            "line 5 holds the response headers"
        ]

    def test_a_query_string_is_a_problem(self) -> None:
        log = "\n".join([*self._clean(), self._line("/version?page=2")])

        assert smoke.access_log_problems(log, self._FORBIDDEN, self._PATH) == [
            "line 5 holds a query string"
        ]

    def test_a_probe_that_left_no_line_is_a_problem(self) -> None:
        """An empty log holds no marker either: finding nothing must not pass."""
        log = "\n".join(self._clean()[:-1])

        assert smoke.access_log_problems(log, self._FORBIDDEN, self._PATH) == [
            f"the request to {self._PATH} left no access-log line"
        ]

    def test_a_probe_line_whose_query_vanished_without_a_trace_is_a_problem(self) -> None:
        """The probe is sent with a query: a line without the mark is not the filter's."""
        log = "\n".join([*self._clean()[:-1], self._line(self._PATH)])

        assert smoke.access_log_problems(log, self._FORBIDDEN, self._PATH) == [
            "line 4: the probe's query left no trace of being cut"
        ]

    def test_an_error_line_is_required_only_when_asked_for(self) -> None:
        clean = self._clean()
        with_error = [
            *clean,
            self._line(f"{self._PATH}{smoke.QUERY_REMOVED}", logger="http.log.error"),
        ]

        assert smoke.access_log_problems(
            "\n".join(clean), self._FORBIDDEN, self._PATH, error_line=True
        ) == [f"the request to {self._PATH} left no error-log line"]
        assert (
            smoke.access_log_problems(
                "\n".join(with_error), self._FORBIDDEN, self._PATH, error_line=True
            )
            == []
        )

    def test_the_probe_sends_a_marker_in_every_place_the_guide_names(self) -> None:
        headers = smoke._probe_headers("203.0.113.60")
        url = smoke._probe_url(smoke.ACCESS_PROBE_PATH)

        for name in (
            "Authorization",
            "X-API-Key",
            "X-Provider-Key",
            "X-Publish-Credential",
            "Cookie",
        ):
            assert smoke.MARKERS[name] in headers[name]
        assert smoke.MARKERS["query value"] in url
        assert smoke.MARKERS["serviceKey query value"] in url
        assert len(set(smoke.MARKERS.values())) == len(smoke.MARKERS)


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
