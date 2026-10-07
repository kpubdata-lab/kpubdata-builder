"""docker-entrypoint.sh contract test (#371, #1122).

Container entry point reads the same environment variable as service/app.py:_is_dev_mode()
(``KPUBDATA_BUILDER_DEV_MODE``), and verifies that the fail-closed gate
works correctly for API key, OIDC and dev-mode combinations. The legacy name
(``KPUBDATA_BUILDER_DEV``) must no longer be allowed (regression prevention).

The stub ``kpubdata-builder`` records the arguments it was exec'd with, so a test can
tell the gate passed from the gate failing; what ``serve`` then checks (the rest of the
OIDC configuration, dev-mode with OIDC) has its own tests in test_auth*.py.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"

# Skip when sh is not available (this project's CI is Linux).
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="entrypoint is a POSIX sh script")

# Stub to intercept ``kpubdata-builder`` that the entry point execs after gate passes.
_STUB_SCRIPT = '#!/bin/sh\necho "stub-serve $*"\nexit 0\n'

# A key the gate accepts: as long as secrets.token_urlsafe(32) gives (43 characters).
_STRONG_KEY = "k" * 43


def _stub_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "kpubdata-builder"
    stub.write_text(_STUB_SCRIPT)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run_entrypoint(env: dict[str, str], bin_dir: Path) -> subprocess.CompletedProcess[bytes]:
    # Prevent key variable leakage from test runner environment by clearing and re-injecting
    # controlled vars.
    full_env = {k: v for k, v in os.environ.items() if k not in _CONTROLLED_ENVS}
    full_env["PATH"] = f"{bin_dir}:{full_env.get('PATH', '')}"
    for key, value in env.items():
        if value:
            full_env[key] = value
    return subprocess.run(["sh", str(ENTRYPOINT)], env=full_env, capture_output=True, check=False)


_CONTROLLED_ENVS = {
    "KPUBDATA_BUILDER_API_KEY",
    "KPUBDATA_BUILDER_DEV_MODE",
    "KPUBDATA_BUILDER_DEV",
    "OIDC_ISSUER",
}


@pytest.fixture()
def bin_dir(tmp_path: Path) -> Path:
    return _stub_bin_dir(tmp_path)


class TestEntrypointDevModeContract:
    """Verify that entry point gate matches app.py dev-mode semantics (#371)."""

    def test_no_key_no_dev_mode_rejected(self, bin_dir: Path) -> None:
        # fail-closed: reject startup if both are absent (exit 1).
        result = _run_entrypoint({}, bin_dir)
        assert result.returncode == 1, result.stderr
        assert b"no authentication is configured" in result.stderr
        assert b"stub-serve" not in result.stdout

    def test_api_key_set_proceeds(self, bin_dir: Path) -> None:
        result = _run_entrypoint({"KPUBDATA_BUILDER_API_KEY": _STRONG_KEY}, bin_dir)
        assert result.returncode == 0, result.stderr
        assert b"stub-serve serve" in result.stdout

    def test_dev_mode_1_allows_no_key(self, bin_dir: Path) -> None:
        result = _run_entrypoint({"KPUBDATA_BUILDER_DEV_MODE": "1"}, bin_dir)
        assert result.returncode == 0, result.stderr

    def test_dev_mode_true_case_insensitive(self, bin_dir: Path) -> None:
        # app.py:_is_dev_mode() accepts 'true'/'1' case-insensitively, so entry point matches.
        for value in ("true", "TRUE", "True"):
            result = _run_entrypoint({"KPUBDATA_BUILDER_DEV_MODE": value}, bin_dir)
            assert result.returncode == 0, f"DEV_MODE={value!r} should allow startup"

    def test_dev_mode_explicit_false_rejected(self, bin_dir: Path) -> None:
        result = _run_entrypoint({"KPUBDATA_BUILDER_DEV_MODE": "0"}, bin_dir)
        assert result.returncode == 1, result.stderr

    def test_legacy_DEV_name_not_recognized(self, bin_dir: Path) -> None:
        # Regression prevention: legacy name KPUBDATA_BUILDER_DEV must no longer take effect.
        result = _run_entrypoint({"KPUBDATA_BUILDER_DEV": "1"}, bin_dir)
        assert result.returncode == 1, "legacy KPUBDATA_BUILDER_DEV must NOT bypass fail-closed"


class TestEntrypointAuthMatrix:
    """Which authentication configurations start (#1122)."""

    @pytest.mark.parametrize(
        ("env", "starts"),
        [
            # OIDC only: no service key needed; serve checks the rest of the OIDC config.
            ({"OIDC_ISSUER": "https://id.example.com/realms/kpubdata"}, True),
            ({"OIDC_ISSUER": "https://a.example/r, https://b.example/r"}, True),
            ({"KPUBDATA_BUILDER_API_KEY": _STRONG_KEY}, True),
            (
                {
                    "OIDC_ISSUER": "https://id.example.com/realms/kpubdata",
                    "KPUBDATA_BUILDER_API_KEY": _STRONG_KEY,
                },
                True,
            ),
            ({}, False),
            # An issuer list of nothing but separators is no issuer.
            ({"OIDC_ISSUER": " , ,"}, False),
            # Dev-mode still starts here; serve refuses it together with OIDC.
            ({"KPUBDATA_BUILDER_DEV_MODE": "1"}, True),
        ],
    )
    def test_starts_only_with_some_authentication(
        self, bin_dir: Path, env: dict[str, str], starts: bool
    ) -> None:
        result = _run_entrypoint(env, bin_dir)
        assert (result.returncode == 0) is starts, result.stderr
        assert (b"stub-serve serve" in result.stdout) is starts

    @pytest.mark.parametrize(
        ("keys", "reason"),
        [
            (
                [
                    "replace-with-strong-random-api-key",
                    "change-me-strong-secret",
                    "your-secret-key",
                    "<secret>",
                ],
                b"example value from the documentation",
            ),
            (["secret", "k" * 31, "x"], b"shorter than 32 characters"),
        ],
    )
    def test_refuses_an_example_or_short_key_without_printing_it(
        self, bin_dir: Path, keys: list[str], reason: bytes
    ) -> None:
        # Also with OIDC set: a weak service key is a weak admin key either way.
        for extra in ({}, {"OIDC_ISSUER": "https://id.example.com/realms/kpubdata"}):
            messages = set()
            for key in keys:
                result = _run_entrypoint({"KPUBDATA_BUILDER_API_KEY": key, **extra}, bin_dir)
                assert result.returncode == 1, result.stderr
                assert b"stub-serve" not in result.stdout
                assert reason in result.stderr
                messages.add(result.stderr)
            # The same words whatever the key: the value is not in the message.
            assert len(messages) == 1

    def test_accepts_a_key_of_exactly_32_characters(self, bin_dir: Path) -> None:
        result = _run_entrypoint({"KPUBDATA_BUILDER_API_KEY": "k" * 32}, bin_dir)
        assert result.returncode == 0, result.stderr

    def test_never_prints_an_accepted_key(self, bin_dir: Path) -> None:
        key = "accepted-" + "z" * 40
        result = _run_entrypoint({"KPUBDATA_BUILDER_API_KEY": key}, bin_dir)
        assert result.returncode == 0, result.stderr
        assert key.encode() not in result.stderr + result.stdout
