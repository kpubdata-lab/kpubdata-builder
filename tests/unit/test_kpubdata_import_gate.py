"""The kpubdata private-import gate has to actually refuse (#830).

Mostly negative tests: a gate nobody has watched fail is a gate nobody knows works.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_kpubdata_imports.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("_kpubdata_import_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()

_PUBLIC = frozenset({"Client", "AuthError", "DatasetRef"})


def _check(tmp_path: Path, source: str, allowlist: dict[tuple[str, str, str], str] | None = None):
    path = tmp_path / "mod.py"
    path.write_text(source, encoding="utf-8")
    return gate.check([path], _PUBLIC, allowlist or {})


@pytest.mark.parametrize(
    "source",
    [
        "from kpubdata.transport._sensitive import SENSITIVE_PARAM_KEYS\n",
        "from kpubdata._hosts import PROVIDER_ALLOWED_HOSTS\n",
        "from kpubdata.core.spec import find_spec\n",
        "from kpubdata import _private_helper\n",
        "import kpubdata.transport.http\n",
        "import kpubdata._hosts as hosts\n",
        "def f():\n    from kpubdata.config import KPubDataConfig\n",
        "import importlib\nimportlib.import_module('kpubdata.core.executor')\n",
        "__import__('kpubdata._hosts')\n",
        "CODE = 'import sys; from kpubdata.core.spec import find_spec; sys.exit(0)'\n",
    ],
)
def test_a_private_import_fails(tmp_path: Path, source: str) -> None:
    violations, _ = _check(tmp_path, source)

    assert violations, f"the gate let a private import through: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "from kpubdata import Client, AuthError\n",
        "import kpubdata\n",
        # A public symbol imported through its defining module is the same symbol.
        "from kpubdata.exceptions import AuthError\n",
        "from kpubdata.core.models import DatasetRef\n",
        "from kpubdata_builder.spec import BuildSpec\n",
        "import kpubdatax\n",
    ],
)
def test_a_public_import_passes(tmp_path: Path, source: str) -> None:
    violations, _ = _check(tmp_path, source)

    assert violations == []


def test_an_allowlisted_private_import_passes(tmp_path: Path) -> None:
    path = (tmp_path / "mod.py").as_posix()
    allow = {(path, "kpubdata.core.spec", "find_spec"): "reason (kpubdata#1)"}

    violations, stale = _check(tmp_path, "from kpubdata.core.spec import find_spec\n", allow)

    assert violations == []
    assert stale == []


def test_an_allowlist_entry_nothing_uses_is_reported(tmp_path: Path) -> None:
    allow = {("gone.py", "kpubdata.core.spec", "find_spec"): "reason (kpubdata#1)"}

    _, stale = _check(tmp_path, "from kpubdata import Client\n", allow)

    assert stale == [("gone.py", "kpubdata.core.spec", "find_spec")]


def test_public_names_are_read_from_all_without_importing(tmp_path: Path) -> None:
    init = tmp_path / "__init__.py"
    init.write_text("raise RuntimeError('must not run')\n__all__ = ['Client', 'Query']\n")

    assert gate.public_names(init) == frozenset({"Client", "Query"})


def test_every_allowlist_entry_names_a_kpubdata_issue() -> None:
    for key, reason in gate.ALLOWLIST.items():
        assert "kpubdata#" in reason, f"{key} does not name the kpubdata issue tracking it"


def test_the_repository_passes_against_the_installed_kpubdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_ROOT)

    assert gate.main([]) == 0
