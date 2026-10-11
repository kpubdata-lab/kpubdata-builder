"""``docs/REQUEST_CREDENTIALS.md`` says what the code and the deployment files say (#1104).

The document tells a user where their key can be read and when it is dropped. A value
in it that has drifted from the code is a wrong promise, so the ones it states are held
here.
"""

from __future__ import annotations

import re
from pathlib import Path

from kpubdata_builder.service import request_credentials
from kpubdata_builder.service.publish_credentials import PUBLISH_CREDENTIAL_HEADER

_ROOT = Path(__file__).resolve().parents[2]
_DOC = (_ROOT / "docs" / "REQUEST_CREDENTIALS.md").read_text(encoding="utf-8")
_COMPOSE = (_ROOT / "docker-compose.prod.app.yml").read_text(encoding="utf-8")
_CADDYFILE = (_ROOT / "ops" / "caddy" / "Caddyfile").read_text(encoding="utf-8")


def test_the_header_names_are_the_ones_the_service_reads() -> None:
    assert f"`{request_credentials.PROVIDER_KEY_HEADER}: " in _DOC
    assert f"`{PUBLISH_CREDENTIAL_HEADER}: " in _DOC


def test_the_waiting_lifetime_is_the_settings_name_and_default() -> None:
    default = int(request_credentials._DEFAULT_TTL_SECONDS)

    assert f"`{request_credentials.CREDENTIAL_TTL_ENV}` (기본 {default}초)" in _DOC


def test_the_proxy_reaches_builder_over_plain_http_on_the_compose_network() -> None:
    """The document says this hop is not encrypted; the compose file is why."""
    assert "BACKEND_UPSTREAM: builder:8000" in _COMPOSE
    assert "reverse_proxy {$BACKEND_UPSTREAM}" in _CADDYFILE
    assert "compose `BACKEND_UPSTREAM: builder:8000`" in _DOC
    assert "**평문 HTTP.**" in _DOC


def test_the_proxy_keeps_an_access_log_without_the_two_key_headers() -> None:
    """Caddy logs requests since #1100, so the document says so and says what is cut.

    The two key headers travel as request headers, and the log's filter removes every
    request header rather than named ones. If the ``log`` directive goes, or the filter
    stops removing the headers, the document's claim must be revisited.
    """
    directives = [
        line.strip()
        for line in _CADDYFILE.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert "log" in directives
    assert "log default {" in directives
    assert "request>headers delete" in directives
    assert "접근 로그는 켜져 있고, 요청 헤더 전체와 쿼리 문자열을 지우고 쓴다" in _DOC
    assert "접근 로그는 켜져 있지 않다" not in _DOC


def test_builders_port_is_published_on_the_loopback_only_by_default() -> None:
    published = [
        line.strip() for line in _COMPOSE.splitlines() if ":8000" in line and "BUILDER_BIND" in line
    ]
    published = [line for line in published if not line.startswith("#")]

    assert published == ['- "${BUILDER_BIND:-127.0.0.1:8000}:8000"']
    assert "루프백에만 묶인다" in _DOC


def test_the_document_does_not_promise_that_signing_out_stops_a_job() -> None:
    """The issue's last criterion: a plain sign-out is not said to discard server jobs."""
    assert "**로그아웃은 서버의 작업을 멈추지 않는다.**" in _DOC
    assert "취소되지 않고" in _DOC


def test_the_tests_it_names_exist() -> None:
    for name in re.findall(r"`(?:tests/unit/)?(test_[a-z_]+\.py)`", _DOC):
        assert (_ROOT / "tests" / "unit" / name).is_file(), name
