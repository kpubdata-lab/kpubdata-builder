"""``serve`` starts with the multi-user (OIDC) profile and answers (#992).

The plumbing tests (``test_prod_oidc_plumbing.py``) hold that the image and the compose
file carry the OIDC settings; they start nothing. This starts the real ``serve`` command
in a process of its own with those settings — and with the operator's keys shut out, as
the profile has it — and asks it over HTTP.

It could not run before kpubdata 0.9: with ``REQUIRE_OWN_PROVIDER_CREDENTIAL`` on,
``serve`` refuses to start on a kpubdata whose client cannot be kept from the
environment's keys (#990).

No identity provider is reached. A token is never accepted here, so this says the service
comes up and stays closed, not that sign-in works — that needs a real realm.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

_ISSUER = "https://idp.invalid/realms/kpubdata"
_START = "from kpubdata_builder.cli import main; raise SystemExit(main())"
_PROFILE = {
    "OIDC_ISSUER": _ISSUER,
    "OIDC_AUDIENCE": "kpubdata-builder",
    "KPUBDATA_BUILDER_ADMIN_SUBJECTS": f"{_ISSUER}|administrator",
    "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL": "true",
}
#: What a deployment of this profile does not set, and the test run may have.
_NOT_IN_THE_PROFILE = (
    "KPUBDATA_BUILDER_DEV_MODE",
    "KPUBDATA_BUILDER_API_KEY",
    "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY",
)


def _environment(overrides: dict[str, str]) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in _NOT_IN_THE_PROFILE and not name.startswith("OIDC_")
    }
    environment.update(overrides)
    return environment


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(tmp_path: Path, port: int, environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _START,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--output-dir",
            str(tmp_path),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _get(port: int, path: str, token: str | None = None) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[int]:
    """The port of a ``serve`` process started with the profile, once it answers."""
    port = _free_port()
    process = _serve(tmp_path, port, _environment(_PROFILE))
    try:
        deadline = time.monotonic() + 60
        while True:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(f"serve exited with {process.returncode} before answering:\n{output}")
            try:
                _get(port, "/healthz")
                break
            except (urllib.error.URLError, ConnectionError):
                if time.monotonic() > deadline:
                    pytest.fail("serve did not answer within 60 seconds")
                time.sleep(0.2)
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_the_profile_starts_and_healthz_answers(served: int) -> None:
    assert _get(served, "/healthz") == (200, {"status": "ok"})


def test_nothing_else_is_answered_without_a_token(served: int) -> None:
    for path in ("/providers", "/builds", "/version"):
        status, body = _get(served, path)

        assert status == 401, path
        assert body["code"] == "unauthorized"


def test_a_token_it_cannot_check_is_not_let_in(served: int) -> None:
    """The issuer cannot be reached here, so no token can be verified. The answer is a
    refusal that says so, never a pass."""
    status, body = _get(served, "/providers", token="not-a-token")

    assert status in (401, 503)
    assert body["code"] in ("unauthorized", "auth_unavailable")


@pytest.mark.parametrize(
    "missing",
    ["OIDC_AUDIENCE", "KPUBDATA_BUILDER_ADMIN_SUBJECTS"],
)
def test_an_incomplete_profile_does_not_start(tmp_path: Path, missing: str) -> None:
    """Negative: without an audience, or with nobody who could let a user in, ``serve``
    exits instead of serving."""
    profile = {name: value for name, value in _PROFILE.items() if name != missing}
    port = _free_port()
    process = _serve(tmp_path, port, _environment(profile))
    try:
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert process.returncode != 0
    assert "refusing to start" in output
