"""stdlib http.server-based HTTP adapter (#36).

Expose BuilderService via HTTP without new dependencies. Only handle request
parsing/response serialization; delegate actual logic to app.dispatch.

Key components:
    - make_handler: Create request handler class bound to BuilderService
    - serve: Run blocking HTTP server
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import sys
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket as _socket
from threading import Lock
from typing import Any, cast
from urllib.parse import urlsplit

from ..spec import JsonValue
from ..store.backend import validate_storage_config
from ..uploads import resolve_max_upload_bytes
from .app import BuilderService, FileResponse, dispatch, refuse_before_body
from .auth import validate_dev_mode, validate_oidc_config
from .publish_credentials import PUBLISH_CREDENTIAL_HEADER
from .request_credentials import PROVIDER_KEY_HEADER

# Limit body size to prevent single request from exhausting memory or stopping
# single-thread server. Conservative upper bound sufficient for spec YAML requests
# while preventing abuse (#186).
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB

# POST /uploads (#498) carries binary file data, not JSON, so we use a larger
# limit than the general JSON limit — the actual limit is reused from
# uploads.resolve_max_upload_bytes() (adjustable via environment variable) to avoid
# maintaining two different numbers in two places.
_UPLOADS_PATH = "/uploads"

# Socket read timeout (seconds). Prevents slow clients/slowloris attacks from
# indefinitely occupying a thread (#219). Each connection thread is released
# here even when using ThreadingHTTPServer.
_SOCKET_TIMEOUT_SECONDS = 30.0

# Maximum number of threads that can process requests concurrently. ThreadingHTTPServer
# creates unlimited new threads per connection (~8MB stack per thread), so hundreds or
# thousands of concurrent connections alone can exhaust memory (#253). Use fixed-size
# ThreadPoolExecutor to cap concurrency.
_DEFAULT_MAX_WORKERS = 10

# Number of connections allowed to wait on socket while pool is full.
# ThreadPoolExecutor's job queue has no limit, so without this value connections queue
# indefinitely after submission — threads are capped but sockets (file descriptors) and
# queue items grow unbounded. Excess connections are rejected immediately with 503
# instead of being queued.
_DEFAULT_MAX_PENDING_REQUESTS = 100


def _overloaded_response(allowed_origins: frozenset[str] = frozenset()) -> bytes:
    """Minimal HTTP response sent directly to socket for connections exceeding wait limit.

    Bypass handler (that's why we're rejecting) and write directly to socket.
    Use ``Connection: close`` so client does not reuse this connection.

    The request is never read, so its ``Origin`` is unknown (#995). Without CORS headers
    a browser hides this response from a cross-origin page: it sees a network error, not
    a 503 with ``Retry-After``. What is sent instead depends only on the deployment:

    - no allowed origin (default-deny): no CORS header, as before;
    - one allowed origin: that origin, as any other response to it carries;
    - several: ``*``, which a browser accepts for a request made without credentials
      (a bearer header is not one). The body is a constant and says nothing a page from
      another origin could not learn by being refused.
    """
    body = b'{"error": "server overloaded", "code": "server_overloaded"}'
    cors = b""
    # This is written to the socket as bytes, so a configured origin with a line break
    # or a non-ASCII character would corrupt the response: send no CORS header then.
    if any(not origin.isascii() or not origin.isprintable() for origin in allowed_origins):
        allowed_origins = frozenset()
    if allowed_origins:
        if len(allowed_origins) == 1:
            (origin,) = allowed_origins
            cors = (
                b"Access-Control-Allow-Origin: " + origin.encode("ascii") + b"\r\n"
                b"Access-Control-Allow-Credentials: true\r\n"
                b"Vary: Origin\r\n"
            )
        else:
            cors = b"Access-Control-Allow-Origin: *\r\n"
        cors += b"Access-Control-Expose-Headers: Retry-After\r\n"
    return (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
        b"Retry-After: 1\r\n" + cors + b"Connection: close\r\n"
        b"\r\n" + body
    )


_OVERLOADED_RESPONSE = _overloaded_response()

# CORS allowed origin list (#322). Default-deny policy: when environment variable
# is unset, reject all cross-origin requests. Accepts comma-separated origin list
# (e.g., KPUBDATA_BUILDER_ALLOWED_ORIGINS=http://localhost:5173,https://studio.example.com).
_ALLOWED_ORIGINS_ENV = "KPUBDATA_BUILDER_ALLOWED_ORIGINS"

# Preflight request headers to allow. Include Authorization for Bearer auth (ADR 0009) (#382).
_CORS_ALLOWED_HEADERS = (
    f"Content-Type, X-API-Key, Authorization, {PROVIDER_KEY_HEADER}, {PUBLISH_CREDENTIAL_HEADER}"
)

# Response headers a cross-origin page may read (#995). A browser shows a script only
# the CORS-safelisted ones unless they are named here: without this Studio cannot read a
# download's file name, the request id to quote in a report, or how long to wait.
_CORS_EXPOSED_HEADERS = "Content-Disposition, X-Request-ID, Retry-After"

# Default MIME type (#323). Used when mimetypes.guess_type returns None.
_DEFAULT_MIME_TYPE = "application/octet-stream"

# Explicit MIME type mapping for common dataset file extensions.
# Conservative defaults when mimetypes library is inaccurate or missing.
_EXPLICIT_MIME_TYPES: dict[str, str] = {
    ".parquet": "application/vnd.apache.parquet",
    ".csv": "text/csv",
    ".json": "application/json",
    ".geojson": "application/geo+json",
    ".geojsonl": "application/geo+jsonl",
    ".ndjson": "application/x-ndjson",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".xml": "application/xml",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
}

# One-read size when streaming file response to socket.
_FILE_CHUNK_BYTES = 64 * 1024

# MIME suffixes that should include charset. Only text/* and these endings are treated as text.
_TEXTUAL_MIME_SUFFIXES = ("json", "xml", "yaml", "javascript")

_logger = logging.getLogger(__name__)

#: What a write raises when the client has already closed its end (#1132).
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


@lru_cache(maxsize=1)
def _get_allowed_origins() -> frozenset[str]:
    """Parse allowed origin list from environment variable (#322).

    Returns:
        frozenset of allowed origins. Empty frozenset if env var unset (default-deny).
    """
    env_value = os.environ.get(_ALLOWED_ORIGINS_ENV, "")
    if not env_value:
        return frozenset()
    # Parse comma-separated origin list and strip whitespace.
    origins = [origin.strip() for origin in env_value.split(",") if origin.strip()]
    return frozenset(origins)


def _clear_cors_cache() -> None:
    """Test utility: clear CORS allowed origin cache (#322)."""
    _get_allowed_origins.cache_clear()


def _is_origin_allowed(request_origin: str | None, allowed: frozenset[str]) -> bool:
    """Check if request origin is in allowed list (#322).

    Same-origin requests (origin is None) are always allowed.

    Args:
        request_origin: Origin header value from request.
        allowed: Set of allowed origins.

    Returns:
        True if origin is allowed, False otherwise.
    """
    if request_origin is None:
        # Same-origin request (browser doesn't send Origin header)
        return True
    return request_origin in allowed


def _get_mime_type(file_path: Path) -> str:
    """Infer MIME type from file path (#323).

    Check explicit mapping first, then use mimetypes library.
    Return default value (application/octet-stream) if still None.

    Args:
        file_path: File path to infer MIME type for.

    Returns:
        MIME type string.
    """
    suffix = file_path.suffix.lower()
    if suffix in _EXPLICIT_MIME_TYPES:
        return _EXPLICIT_MIME_TYPES[suffix]
    guessed = mimetypes.guess_type(file_path.name)[0]
    return guessed if guessed else _DEFAULT_MIME_TYPE


def _content_type_header(mime_type: str) -> str:
    """Build Content-Type header value. Add charset only for text formats.

    Previously we unconditionally added ``; charset=utf-8`` to file responses —
    even parquet got ``application/vnd.apache.parquet; charset=utf-8``.
    Declaring character encoding on binary is simply wrong.
    """
    if mime_type.startswith("text/") or mime_type.endswith(_TEXTUAL_MIME_SUFFIXES):
        return f"{mime_type}; charset=utf-8"
    return mime_type


def make_handler(service: BuilderService) -> type[BaseHTTPRequestHandler]:
    """Create request handler class bound to given BuilderService."""

    class _Handler(BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler.timeout is applied to socket if set (#219).
        timeout = _SOCKET_TIMEOUT_SECONDS

        def _dispatch(self, method: str) -> None:
            self._request_id = uuid.uuid4().hex[:12]
            # Route using only path component to prevent query string leaking into
            # path/run_id, pass query separately to dispatch (#252). Must parse before
            # reading body because body size limit selection requires method+path.
            # getattr default is never used — BaseHTTPRequestHandler.parse_request()
            # always sets self.path before calling do_*() — only unit tests creating
            # handler via object.__new__() omit self.path (#498 pre-existing fixture,
            # TestHttpRobustness).
            split = urlsplit(getattr(self, "path", ""))
            path = split.path
            # Only POST /uploads (#498) receives file binary body; all other endpoints
            # receive JSON body like before.
            is_binary_upload = method == "POST" and path == _UPLOADS_PATH
            max_body_bytes = resolve_max_upload_bytes() if is_binary_upload else _MAX_BODY_BYTES
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._write(400, {"error": "invalid Content-Length header"})
                return
            if length < 0:
                self._write(400, {"error": "invalid Content-Length header"})
                return
            # If declared length exceeds limit, reject with 413 without reading body (#186).
            if length > max_body_bytes:
                self._write(413, {"error": "request body too large"})
                return
            # Throttle and authenticate before reading a body (#1069): an upload is up to
            # 20 MiB, and a request with no token made the server read all of it first.
            # The refused request's body stays unread, so the connection is closed.
            client_id = service._auth_throttle.client_id(
                self.client_address[0] if self.client_address else None,
                self.headers.get_all("X-Forwarded-For") or [],
            )
            if length:
                refusal = refuse_before_body(
                    service,
                    api_key=self.headers.get("X-API-Key"),
                    bearer_token=self.headers.get("Authorization"),
                    client_id=client_id,
                )
                if refusal is not None:
                    self.close_connection = True
                    self._write(refusal.status_code, refusal.body)
                    return
            # Read body: timeout or incomplete read is handled as JSON 400 instead of
            # dropped connection (#219).
            if length:
                try:
                    raw = self.rfile.read(length)
                except TimeoutError:
                    self._write(400, {"error": "request body read timed out"})
                    return
                if len(raw) < length:
                    self._write(400, {"error": "incomplete request body"})
                    return
            else:
                raw = b""
            body: dict[str, JsonValue] | None = None
            raw_body: bytes | None = None
            if is_binary_upload:
                raw_body = raw
            elif raw:
                try:
                    parsed = cast(object, json.loads(raw))
                except json.JSONDecodeError:
                    self._write(400, {"error": "invalid JSON body"})
                    return
                # HTTP adapter accepts any JSON top-level type, but service handles only
                # mappings (objects). Reject scalar/array body with 400 instead of TypeError (#183).
                if not isinstance(parsed, dict):
                    self._write(400, {"error": "JSON body must be an object"})
                    return
                body = cast(dict[str, JsonValue], parsed)
            # If dispatch() raises unexpected exception, return JSON 500 instead of dropping
            # connection. Log detailed info only to server log, do not expose to client (#218).
            try:
                response = dispatch(
                    service,
                    method,
                    path,
                    body,
                    query=split.query,
                    api_key=self.headers.get("X-API-Key"),
                    bearer_token=self.headers.get("Authorization"),
                    raw_body=raw_body,
                    # Provider keys for this request only (#683) — a header, never a
                    # URL query that proxies and access logs would keep.
                    provider_key_headers=self.headers.get_all(PROVIDER_KEY_HEADER) or [],
                    # Publish credentials for this request only (#925), on the same terms.
                    publish_credential_headers=(
                        self.headers.get_all(PUBLISH_CREDENTIAL_HEADER) or []
                    ),
                    # Client ID for auth failure throttling. The TCP peer address,
                    # unless that peer is a proxy the deployment names: only then is
                    # X-Forwarded-For read, and only the part that proxy wrote — a
                    # header from anyone else can be forged (#1031).
                    client_id=client_id,
                )
            except Exception:
                _logger.error(
                    "Unhandled exception in dispatch: %s %s\n%s",
                    method,
                    path,
                    traceback.format_exc(),
                    extra={"request_id": getattr(self, "_request_id", None)},
                )
                self._write(500, {"error": "internal server error"})
                return
            # Branch on FileResponse vs ServiceResponse (#323)
            if isinstance(response, FileResponse):
                self._write_file(response)
            else:
                self._write(response.status_code, response.body)

        def _send_cors_headers(self, origin: str | None = None) -> None:
            """Send CORS headers (#322).

            Args:
                origin: Origin header value from request. None = same-origin.
            """
            allowed = _get_allowed_origins()
            # CORS headers in response vary by request Origin, so always send Vary: Origin
            # regardless of allow/deny. Without this, caching proxy/CDN may reuse one
            # origin's Access-Control-Allow-Origin response for another origin.
            self.send_header("Vary", "Origin")
            # Send CORS headers if same-origin or in allow list.
            if _is_origin_allowed(origin, allowed):
                if origin is not None:
                    # Cross-origin request: specify allowed origin.
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Credentials", "true")
                else:
                    # Same-origin request: allow without specific origin restriction.
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", _CORS_ALLOWED_HEADERS)
                self.send_header("Access-Control-Max-Age", "86400")
                self.send_header("Access-Control-Expose-Headers", _CORS_EXPOSED_HEADERS)

        def _write(self, status_code: int, body: dict[str, JsonValue]) -> None:
            payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            if hasattr(self, "_request_id"):
                self.send_header("X-Request-ID", self._request_id)
            self._send_cors_headers(origin=self.headers.get("Origin"))
            self.end_headers()
            _ = self.wfile.write(payload)

        def _write_file(self, response: FileResponse) -> None:
            """Write file response (#323).

            Stream file in chunks without loading entire file into memory. Previously
            used ``read_bytes()`` — each response consumed memory equal to file size.
            Build artifacts (parquet/jsonl) are unbounded in size; a few concurrent
            downloads could kill the process.

            Open/stat failures occur before sending headers and can respond with 500.
            Read failures mid-stream cannot — headers already sent. In that case, close
            connection so client doesn't accept truncated file as complete.
            """
            try:
                size = response.file_path.stat().st_size
                handle = response.file_path.open("rb")
            except OSError as exc:
                _logger.error("Failed to read file %s: %s", response.file_path, exc)
                self._write(500, {"error": "failed to read file"})
                return

            self.send_response(response.status_code)
            self.send_header(
                "Content-Type", _content_type_header(_get_mime_type(response.file_path))
            )
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{response.filename}"')
            self._send_cors_headers(origin=self.headers.get("Origin"))
            self.end_headers()

            remaining = size
            try:
                with handle:
                    while remaining > 0:
                        chunk = handle.read(min(_FILE_CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        _ = self.wfile.write(chunk)
                        remaining -= len(chunk)
            except OSError as exc:
                _logger.error("Failed while streaming %s: %s", response.file_path, exc)
                self.close_connection = True
                return
            if remaining:
                # Did not send promised Content-Length (file shrank while streaming).
                _logger.error(
                    "File %s shrank while streaming: %d bytes short",
                    response.file_path,
                    remaining,
                )
                self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802 - http.server convention
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server convention
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802 - http.server convention
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802 - http.server convention
            self._dispatch("DELETE")

        def do_OPTIONS(self) -> None:  # noqa: N802 - http.server convention
            # Respond to CORS preflight request (#322). Return 204 with allow headers only.
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._send_cors_headers(origin=self.headers.get("Origin"))
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            # Suppress default stderr access log.
            return

    return _Handler


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with bounded concurrent threads (#253).

    ThreadingHTTPServer creates unlimited new threads per connection, so malicious or
    misbehaving clients opening many concurrent connections can exhaust threads/memory
    and break service. Delegate request processing to fixed-size ThreadPoolExecutor to
    cap throughput.

    Capping threads alone is insufficient. ThreadPoolExecutor's job queue is unbounded,
    so connections pile up on the queue after pool fills — threads are capped but open
    sockets (file descriptors) and queue items grow unbounded. slowloris-style attacks
    that just open connections hit this actual limit.

    Cap total "in-flight + pending" connections to ``max_workers`` + ``max_pending_requests``,
    reject excess with 503 immediately instead of queuing. Immediate rejection is more
    honest than waiting 30s then timing out — lets client make retry decision right away.
    """

    daemon_threads = True

    def __init__(
        self,
        *args: Any,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        max_pending_requests: int = _DEFAULT_MAX_PENDING_REQUESTS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kpubdata-http"
        )
        self._admission_lock = Lock()
        self._inflight = 0
        self._max_inflight = max_workers + max(0, max_pending_requests)

    def process_request(
        self, request: _socket | tuple[bytes, _socket], client_address: Any
    ) -> None:
        # Delegate to fixed-size pool instead of creating new thread directly. When
        # pool fills, excess requests wait in pool queue, but queue itself has an upper
        # bound.
        with self._admission_lock:
            admitted = self._inflight < self._max_inflight
            if admitted:
                self._inflight += 1
        if not admitted:
            self._reject(request, client_address)
            return
        try:
            future = self._executor.submit(self.process_request_thread, request, client_address)
        except RuntimeError:
            # Connection after server_close(). Not queued, so decrement counter —
            # done callback won't be attached, so no one else will do it.
            with self._admission_lock:
                self._inflight -= 1
            self.shutdown_request(request)
            return
        future.add_done_callback(self._release)

    def _release(self, _future: object) -> None:
        with self._admission_lock:
            self._inflight -= 1

    def _reject(self, request: _socket | tuple[bytes, _socket], client_address: Any) -> None:
        """Reject connection exceeding wait limit with 503 immediately."""
        _logger.warning(
            "rejecting connection from %s: %d requests already in flight",
            client_address,
            self._max_inflight,
        )
        try:
            cast(_socket, request).sendall(_overloaded_response(_get_allowed_origins()))
        except OSError:
            # Peer already closed. Just close our end.
            pass
        finally:
            self.shutdown_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Keep a client that left early out of stderr (#1132).

        socketserver calls this from inside the ``except`` that caught a handler's
        exception, and its default prints the whole traceback to stderr. When the client
        closed the connection before the response was written — a page navigated away,
        a tab closed, a ``fetch`` was aborted — that is ordinary client behaviour, not a
        server error, and about twenty lines of traceback per occurrence buried the real
        errors. Those are logged at debug level instead; anything else still goes to the
        default handler.
        """
        error = sys.exc_info()[1]
        if isinstance(error, _CLIENT_GONE):
            _logger.debug(
                "client %s closed the connection before the response was written: %s",
                client_address,
                type(error).__name__,
            )
            return
        super().handle_error(request, client_address)

    def server_close(self) -> None:
        super().server_close()
        self._executor.shutdown(wait=False, cancel_futures=True)


def serve(
    service: BuilderService,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> None:
    """Serve BuilderService as blocking HTTP server.

    Use BoundedThreadingHTTPServer instead of single-thread HTTPServer so slow
    clients don't stall entire server (#219), while capping concurrent thread count
    to prevent DoS (#253).

    On SIGTERM, stop serve_forever and gracefully shut down in-flight requests (#374).
    ACA/K8s rolling updates send SIGTERM to container; work is not forcibly severed.
    SIGINT (Ctrl-C) is untouched so KeyboardInterrupt propagates naturally.

    Args:
        service: BuilderService to expose.
        host: Bind host.
        port: Bind port. ``0`` lets the operating system choose one; the address is then
            printed as ``listening on http://<host>:<port>``.
        max_workers: Maximum concurrent request-handling threads.
    """
    # Validate OIDC config on startup (fail-closed, #385). No-op if OIDC disabled.
    validate_oidc_config()
    # When running in dev-mode, auth is entirely off — log warning and reject
    # conflicting combinations (OIDC config + dev-mode) to prevent prod accidents.
    validate_dev_mode()
    # Validate storage backend config on startup (fail-closed, ADR 0016). No-op for
    # sqlite default; cubrid checks URL and driver early.
    validate_storage_config()
    # A multi-user deployment keeps job keys in memory only (#683): runs a previous
    # process left unfinished can never resume, so they are failed now, as
    # credentials_required, rather than left looking in progress.
    interrupted = service.mark_interrupted_runs()
    if interrupted:
        _logger.warning(
            "marked %d interrupted run(s) failed as credentials_required", len(interrupted)
        )
    # Uploads past the retention period go now (#1045); an owner's are also dropped
    # whenever they upload. Only a multi-user deployment keeps a retention period.
    expired = service.purge_expired_uploads()
    if expired:
        _logger.info("deleted %d upload(s) past the retention period", expired)
    server = BoundedThreadingHTTPServer(
        (host, port), make_handler(service), max_workers=max_workers
    )
    if port == 0:
        # The operating system chose the port; say which, so whoever started the
        # process can reach it without a race for a port picked beforehand.
        print(f"listening on http://{host}:{server.server_port}", flush=True)

    def _shutdown(_signum: int, _frame: object) -> None:
        # Must shutdown from separate thread so serve_forever block unblocks (http.server
        # recommended pattern).
        import threading

        threading.Thread(target=server.shutdown, daemon=True).start()

    import signal

    # Wire only SIGTERM to custom handler (container orchestrator).
    # Don't touch SIGINT so Ctrl-C's KeyboardInterrupt propagates naturally.
    previous_term = signal.signal(signal.SIGTERM, _shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous_term)


__all__ = ["BoundedThreadingHTTPServer", "make_handler", "serve"]
