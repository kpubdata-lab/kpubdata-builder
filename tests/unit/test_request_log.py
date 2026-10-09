"""Every request leaves one line, and no line holds a secret (#1100).

The lines are read from a real server answering over a socket: what is checked is what a
deployment's log collector would be handed.
"""

from __future__ import annotations

import ast
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path

import pytest
import yaml

from kpubdata_builder import cli
from kpubdata_builder.service import BuilderService, build_runs_api, pii_reads, request_log
from kpubdata_builder.service._contract_operations import OPERATIONS
from kpubdata_builder.service.auth import AuthError, Principal
from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler

_API_KEY = "marker-value-of-the-api-key"
#: Values that must be in no line, each sent where a deployment's users send secrets.
_CANARIES = {
    "authorization": "canary-bearer-51d0e2",
    "provider key": "canary-provider-key-88b1",
    "publish credential": "canary-publish-token-c41f",
    "cookie": "canary-cookie-0a9d",
    "query": "canary-query-value-6e2b",
    "body": "canary-body-value-93fd",
    "path": "canary-path-segment-2c7a",
}


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def parsed(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.lines]

    def wait(self, count: int) -> None:
        """A line is written once the answer has been sent: the client can be ahead of it."""
        deadline = time.monotonic() + 10
        while len(self.lines) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.lines) == count, self.lines


@pytest.fixture()
def lines() -> Iterator[_Lines]:
    handler = _Lines()
    logger = logging.getLogger("kpubdata_builder.request")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


@pytest.fixture()
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", _API_KEY)
    service = BuilderService(output_root=tmp_path, client_factory=cli._create_client)
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _call(
    base: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, str]]:
    request = urllib.request.Request(
        base + path,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
        data=json.dumps(body).encode("utf-8") if body is not None else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return answer.status, dict(answer.headers.items())
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items())


# ------------------------------------------------------------------ the line


def test_an_answered_request_leaves_one_line_with_its_route_and_outcome(
    base: str, lines: _Lines
) -> None:
    status, headers = _call(base, "GET", "/builds/some-run-id", headers={"X-API-Key": _API_KEY})

    assert status == 404
    lines.wait(1)
    (line,) = lines.parsed()
    assert line["event"] == "request"
    assert line["method"] == "GET"
    assert line["route"] == "/builds/{run_id}"
    assert line["status"] == 404
    assert line["request_id"] == headers["X-Request-ID"]
    assert isinstance(line["duration_ms"], float) and line["duration_ms"] >= 0
    assert str(line["ts"]).endswith("Z")
    assert line["principal"] == "service"
    assert "some-run-id" not in lines.lines[0]


def test_a_request_that_is_refused_is_a_line_too_and_names_no_requester(
    base: str, lines: _Lines
) -> None:
    status, headers = _call(base, "GET", "/builds")

    assert status == 401
    lines.wait(1)
    (line,) = lines.parsed()
    assert (line["route"], line["status"]) == ("/builds", 401)
    assert line["request_id"] == headers["X-Request-ID"]
    assert line["principal"] is None and line["owner"] is None


def test_the_requester_of_one_request_is_not_the_requester_of_the_next(
    base: str, lines: _Lines
) -> None:
    """The server answers on reused threads: a refused request after an admitted one."""
    for _ in range(3):
        _call(base, "GET", "/builds", headers={"X-API-Key": _API_KEY})
        _call(base, "GET", "/builds")

    lines.wait(6)
    assert [line["principal"] for line in lines.parsed()] == ["service", None] * 3


def test_the_line_keeps_the_code_of_a_refusal(base: str, lines: _Lines) -> None:
    """How a request ended is told apart by Builder's code, and joined by the request id."""
    _call(base, "POST", "/query", headers={"X-API-Key": _API_KEY}, body={"sql": "DROP TABLE x"})
    _call(base, "GET", "/builds")

    lines.wait(2)
    answered = [(line["route"], line["status"], line.get("code")) for line in lines.parsed()]
    assert answered[1] == ("/builds", 401, "unauthorized")
    assert answered[0][0] == "/query" and answered[0][1] == 400


def test_a_path_no_route_matches_is_not_written(base: str, lines: _Lines) -> None:
    _call(base, "GET", f"/nothing/{_CANARIES['path']}", headers={"X-API-Key": _API_KEY})

    lines.wait(1)
    (line,) = lines.parsed()
    assert line["route"] == request_log.UNMATCHED_ROUTE
    assert _CANARIES["path"] not in lines.lines[0]


# ------------------------------------------------------------------ leakage


def test_no_line_holds_a_header_a_query_a_body_or_a_path_value(base: str, lines: _Lines) -> None:
    secret_headers = {
        "Authorization": f"Bearer {_CANARIES['authorization']}",
        "X-Provider-Key": f"datago={_CANARIES['provider key']}",
        "X-Publish-Credential": f"huggingface={_CANARIES['publish credential']}",
        "Cookie": f"session={_CANARIES['cookie']}",
    }
    spec = f"dataset_id: t\ntitle: t\ndescription: {_CANARIES['body']}\nsources: []\nexports: []\n"
    calls: list[tuple[str, str, dict[str, object] | None]] = [
        ("GET", f"/builds?cursor={_CANARIES['query']}", None),
        ("GET", f"/builds/{_CANARIES['path']}", None),
        ("GET", f"/artifacts/{_CANARIES['path']}/gold/{_CANARIES['path']}.jsonl", None),
        ("GET", f"/warehouse/tables/{_CANARIES['path']}", None),
        ("POST", "/preview", {"spec": spec}),
        ("POST", "/builds", {"spec": spec, "run_id": _CANARIES["path"]}),
        ("POST", "/query", {"sql": f"SELECT '{_CANARIES['body']}'"}),
        ("PUT", "/providers/datago/credential", {"api_key": _CANARIES["body"]}),
    ]
    for with_key in (True, False):
        headers = {**secret_headers, **({"X-API-Key": _API_KEY} if with_key else {})}
        for method, path, body in calls:
            _call(base, method, path, headers=headers, body=body)

    lines.wait(2 * len(calls))
    everything = "\n".join(lines.lines)
    for what, value in {**_CANARIES, "api key": _API_KEY}.items():
        assert value not in everything, f"the {what} is in a request line"
    # Only these fields, whatever the request was.
    allowed = {
        "ts",
        "event",
        "request_id",
        "method",
        "route",
        "status",
        "duration_ms",
        "principal",
        "owner",
        "code",
    }
    assert {key for line in lines.parsed() for key in line} <= allowed


def test_a_code_that_is_not_one_of_builders_is_not_written(lines: _Lines) -> None:
    """A value that only looks like a code — a key of letters and digits does — is not kept."""
    looks_like_one = "letters_and_digits_0123456789"
    for code in ("has space", "x" * 65, 'quote"d', "키", looks_like_one, "Unauthorized"):
        request_log.record(
            request_id="r", method="GET", route="/x", status=400, duration_ms=1.0, code=code
        )
    for code in (7, None, ""):
        request_log.record(
            request_id="r", method="GET", route="/x", status=400, duration_ms=1.0, code=code
        )
    request_log.record(
        request_id="r", method="GET", route="/x", status=400, duration_ms=1.0, code="auth_throttled"
    )

    assert [line.get("code") for line in lines.parsed()] == (
        [request_log.OTHER_CODE] * 6 + [None] * 3 + ["auth_throttled"]
    )
    assert looks_like_one not in "\n".join(lines.lines)


def _literal_codes_in_the_source() -> dict[str, str]:
    """Every ``"code": "<text>"`` written in a dict of the package, with where it is."""
    found: dict[str, str] = {}
    package = Path(request_log.__file__).resolve().parents[1]
    for source in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "code"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.setdefault(value.value, f"{source.relative_to(package)}:{value.lineno}")
    return found


def test_every_code_written_in_the_source_is_a_known_code() -> None:
    """A new code is added to the list, or its lines say only ``other``."""
    written = _literal_codes_in_the_source()

    assert len(written) > 40, "the scan found too few codes to be reading the source"
    missing = {
        code: where for code, where in written.items() if code not in request_log.KNOWN_CODES
    }
    assert missing == {}


def test_every_code_the_contract_names_is_a_known_code() -> None:
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "contract" / "builder-api.yaml").read_text("utf-8")
    )
    named: set[str] = set()

    def walk(node: object, key: str | None = None) -> None:
        if isinstance(node, dict):
            if key == "code" and isinstance(node.get("enum"), list):
                named.update(item for item in node["enum"] if isinstance(item, str))
            # An example answer: `code: unsafe_query` beside its `error`.
            example = node.get("code")
            if isinstance(example, str) and "error" in node:
                named.add(example)
            for child_key, child in node.items():
                walk(child, child_key)
        elif isinstance(node, list):
            for child in node:
                walk(child, key)

    walk(contract)

    assert len(named) > 20, "the walk found too few codes to be reading the contract"
    assert named - request_log.KNOWN_CODES == set()


@pytest.mark.parametrize(
    "code",
    [
        build_runs_api.INTERRUPTED_CODE,
        pii_reads.DECLARED_PII_WITHHELD,
        pii_reads.PII_DECLARATION_UNAVAILABLE,
        AuthError("r").code,
        AuthError("r", expired=True).code,
        AuthError("r", email_unverified=True).code,
        AuthError("r", status_code=503).code,
    ],
)
def test_the_codes_that_are_not_written_as_literals_are_known_too(code: str) -> None:
    assert code in request_log.KNOWN_CODES


def test_a_connection_refused_at_the_limit_leaves_a_line(tmp_path: Path, lines: _Lines) -> None:
    """The request is never read: the line has no method, route or requester."""

    class _Socket:
        def sendall(self, data: bytes) -> None:
            return

        def shutdown(self, how: int) -> None:
            return

        def close(self) -> None:
            return

    service = BuilderService(output_root=tmp_path, client_factory=cli._create_client)
    server = BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(service), max_workers=1, max_pending_requests=0
    )
    try:
        server._reject(_Socket(), ("10.0.0.1", 1))  # type: ignore[arg-type]
    finally:
        server.server_close()

    (line,) = lines.parsed()
    assert (line["route"], line["status"], line["code"]) == (
        request_log.UNREAD_ROUTE,
        503,
        "server_overloaded",
    )
    assert line["principal"] is None and line["owner"] is None
    assert "10.0.0.1" not in lines.lines[0]


# ------------------------------------------------------------------ owner


def test_owner_says_same_or_different_without_saying_who(lines: _Lines) -> None:
    alice = Principal("oidc", "alice@example.test", "oidc:alice-subject")
    bob = Principal("oidc", "bob@example.test", "oidc:bob-subject")

    for principal in (alice, bob, alice):
        request_log.begin()
        request_log.note_principal(principal)
        request_log.record(request_id="r", method="GET", route="/builds", status=200, duration_ms=1)

    first, second, third = (line["owner"] for line in lines.parsed())
    assert first == third and first != second
    everything = "\n".join(lines.lines)
    for value in ("alice", "bob", "example.test", "subject"):
        assert value not in everything


def test_owner_is_not_a_plain_hash_of_the_id(lines: _Lines) -> None:
    """A hash anyone can recompute would be the id written another way."""
    import hashlib

    owner_id = "oidc:alice-subject"
    request_log.begin()
    request_log.note_principal(Principal("oidc", "alice", owner_id))
    request_log.record(request_id="r", method="GET", route="/builds", status=200, duration_ms=1)

    owner = str(lines.parsed()[0]["owner"])
    for name in ("sha256", "sha1", "md5", "sha512"):
        assert not hashlib.new(name, owner_id.encode()).hexdigest().startswith(owner)


# ------------------------------------------------------------------ routes


@pytest.mark.parametrize(("method", "template"), [(op[0], op[1]) for op in OPERATIONS])
def test_every_route_of_the_contract_is_named_by_its_template(method: str, template: str) -> None:
    path = "/".join(
        "value-1" if segment.startswith("{") else segment for segment in template.split("/")
    )

    assert request_log.route_of(method, path) == template


def test_a_file_path_with_slashes_is_still_the_artifact_route() -> None:
    assert (
        request_log.route_of("GET", "/artifacts/run-1/gold/part/data.jsonl")
        == "/artifacts/{run_id}/{file_path}"
    )


def test_a_literal_segment_wins_over_a_parameter() -> None:
    assert (
        request_log.route_of("GET", "/warehouse/tables/profile/profile")
        == "/warehouse/tables/{name}/profile"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/"), ("GET", "/no/such/route"), ("PATCH", "/builds"), ("GET", "/builds/a/b/c/d")],
)
def test_anything_else_is_unmatched(method: str, path: str) -> None:
    assert request_log.route_of(method, path) == request_log.UNMATCHED_ROUTE
