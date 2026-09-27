"""SSRF-safe GET fetch for URL source (#498 P0).

Unlike Public API/File, URL source lets user specify arbitrary endpoint.
sends requests directly to endpoint, so unchecked access uses server as proxy
to scan/access internal networks (loopback/private/link-local/cloud metadata IP etc)
SSRF attack is possible. This module enforces (#498 checklist):

    - Allow GET only, don't send arbitrary headers like Authorization (Auth=None, P0).
    - Allow https scheme only ("HTTPS by default" — file/ftp etc already rejected by scheme
      itself).
    - Reject if URL has userinfo (``user:pass@host``).
    - Directly resolve hostname via DNS; only proceed if all results are public
      (global-routable) addresses — reject any private/loopback/
      reject all if link-local/reserved (fail-closed).
    - Actual TCP connection to validated IP directly (Host header keeps original hostname
      — reconnecting via hostname could allow DNS rebinding between verify/connect time
      to bypass validation.
    - Don't auto-follow redirects; repeat same validation per hop.
    - Enforce connect/read timeout and response size limit.

This module is pure network layer — responsibility for parsing response bytes to records
is in ``tabular_ingest``.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from .errors import IngestionError

# Allowed schemes. http/file/ftp etc. are not here, so structurally rejected.
_ALLOWED_SCHEMES = frozenset({"https"})

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_REDIRECTS = 5
_READ_CHUNK_BYTES = 65536

# Response size limit (#498). Overridable via env var, but default is conservative.
_MAX_FETCH_BYTES_ENV = "KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES"
_DEFAULT_MAX_FETCH_BYTES = 20 * 1024 * 1024  # 20 MiB

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def default_max_fetch_bytes() -> int:
    """Read response size limit from environment variable (default if not set)."""
    raw = os.environ.get(_MAX_FETCH_BYTES_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_FETCH_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_FETCH_BYTES
    return value if value > 0 else _DEFAULT_MAX_FETCH_BYTES


@dataclass(frozen=True)
class FetchResult:
    """Successful fetch result.

    Attributes:
        content: Response body raw bytes.
        content_type: Response ``Content-Type`` header value (empty string if absent).
        final_url: Actual requested URL after following all redirects. Provenance stores
            only sanitized identity, not this value — caller's responsibility.
    """

    content: bytes
    content_type: str
    final_url: str


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects only to IPs validated by DNS.

    ``http.client.HTTPSConnection`` by default re-resolves ``self.host`` to connect — if
    DNS response changes between validate(``_resolve_and_validate``) and actual connect
    (DNS rebinding), could connect to private IP.
    This class opens socket only with pre-validated ``pinned_ip``, uses original ``host``
    for TLS SNI/cert validation (maintain vhost/cert validation consistency).

    Apply ``connect_timeout`` and ``read_timeout`` separately: TCP+TLS
    connect uses ``connect_timeout``, socket read after connection uses
    ``read_timeout`` (#538 review) — if ``connect()`` before actual connect
    ``self.timeout`` already overwritten to read_timeout, connect_timeout
    won't be used; switch timeout only after connect completes.
    """

    def __init__(
        self,
        host: str,
        pinned_ip: str,
        port: int,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=connect_timeout)
        self._pinned_ip = pinned_ip
        self._read_timeout = read_timeout

    def connect(self) -> None:  # noqa: D102 - http.client signature override
        sock = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)
        self.sock.settimeout(self._read_timeout)


def _validate_url_shape(url: str) -> tuple[str, str, int, str]:
    """Validate scheme/userinfo/host structure and return (scheme, host, port, path+query)."""
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise IngestionError(
            f"refusing to fetch url with scheme {parts.scheme or 'none'!r} "
            "(only https is allowed, SSRF policy #498)"
        )
    if parts.username or parts.password:
        raise IngestionError("refusing to fetch url containing userinfo (SSRF policy #498)")
    host = parts.hostname
    if not host:
        raise IngestionError("url must include a host")
    port = parts.port or 443
    path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return parts.scheme, host, port, path


def _resolve_and_validate(host: str, port: int) -> str:
    """Resolve host via DNS and return first IP only if all results are public IPs.

    Return None if any are private/loopback/link-local/reserved/multicast/unspecified
    reject all (fail-closed) — can't guarantee which stack picks private address
    if only some responses are private.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise IngestionError(f"failed to resolve host: {host}") from exc
    if not infos:
        raise IngestionError(f"no addresses resolved for host: {host}")

    resolved_ips: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        # sockaddr is tuple covering IPv4(host, port)/IPv6(host, port, flowinfo, scope_id) forms,
        # so index 0 static type widens to str|int — actual value is always address string,
        # so str() explicitly cast.
        ip_text = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise IngestionError(f"resolved a non-IP address for host: {host}") from exc
        if not ip.is_global:
            raise IngestionError(
                f"refusing to fetch from non-public address for host {host!r} (SSRF policy #498)"
            )
        resolved_ips.append(ip_text)
    return resolved_ips[0]


def _read_bounded(response: http.client.HTTPResponse, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise IngestionError(
                f"response exceeds max size ({max_bytes} bytes, SSRF/DoS policy #498)"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def safe_fetch_get(
    url: str,
    *,
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = _DEFAULT_READ_TIMEOUT_SECONDS,
    max_bytes: int | None = None,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
) -> FetchResult:
    """Safely perform single validated GET (Auth=None) request (#498).

    Args:
        url: Absolute https URL to fetch.
        connect_timeout: TCP+TLS connect timeout (seconds).
        read_timeout: Socket read timeout (seconds).
        max_bytes: Response body size limit. If omitted, use environment/default.
        max_redirects: Max redirects to follow. Per hop, repeat scheme/userinfo/DNS
            validation from scratch.

    Returns:
        FetchResult: Body bytes, Content-Type, final URL.

    Raises:
        IngestionError: SSRF policy violation, DNS failure, timeout, size exceeded, abnormal
            status, excessive redirects.
    """
    resolved_max_bytes = max_bytes if max_bytes is not None else default_max_fetch_bytes()
    current_url = url
    for _ in range(max_redirects + 1):
        _scheme, host, port, path = _validate_url_shape(current_url)
        pinned_ip = _resolve_and_validate(host, port)
        connection = _PinnedHTTPSConnection(
            host, pinned_ip, port, connect_timeout=connect_timeout, read_timeout=read_timeout
        )
        try:
            # Never pass user-defined headers — arbitrary headers forbidden (#498).
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/json, application/x-ndjson, text/csv;q=0.9, */*;q=0.1",
                    "User-Agent": "kpubdata-builder-url-source/1.0",
                },
            )
            response = connection.getresponse()
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                # Do not read redirect body — each hop closes connection in finally,
                # so "drain for connection reuse" reason doesn't hold; read() without amt here
                # would bypass _read_bounded()'s max_bytes cap bounding only final 200 response,
                # becoming unbounded read (BLOCKER, #538 review).
                if not location:
                    raise IngestionError("redirect response is missing a Location header")
                current_url = urljoin(current_url, location)
                continue
            if response.status != 200:
                raise IngestionError(f"unexpected HTTP status from url source: {response.status}")
            content_type = response.getheader("Content-Type", "") or ""
            content = _read_bounded(response, resolved_max_bytes)
            return FetchResult(content=content, content_type=content_type, final_url=current_url)
        except (TimeoutError, OSError, ssl.SSLError) as exc:
            raise IngestionError(f"failed to fetch url source: {exc}") from exc
        finally:
            connection.close()
    raise IngestionError(f"too many redirects (max {max_redirects}, SSRF policy #498)")


__all__ = ["FetchResult", "default_max_fetch_bytes", "safe_fetch_get"]
