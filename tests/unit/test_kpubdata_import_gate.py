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


# Released vs unreleased kpubdata (#830). kpubdata main exported find_spec before a
# release did; against main the entry is a notice, against a release it still fails.

_NOW_PUBLIC = _PUBLIC | {"find_spec"}


def _gate_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    public: frozenset[str],
    *flags: str,
    env: str | None = None,
) -> tuple[int, str]:
    path = tmp_path / "mod.py"
    path.write_text(source, encoding="utf-8")
    init = tmp_path / "__init__.py"
    init.write_text(f"__all__ = {sorted(public)!r}\n", encoding="utf-8")
    monkeypatch.setattr(
        gate,
        "ALLOWLIST",
        {(path.as_posix(), "kpubdata.core.spec", "find_spec"): "reason (kpubdata#1)"},
    )
    if env is None:
        monkeypatch.delenv(gate.TARGET_ENV, raising=False)
    else:
        monkeypatch.setenv(gate.TARGET_ENV, env)
    code = gate.main(["--kpubdata-init", str(init), *flags, str(path)])
    return code, capsys.readouterr().out


_FIND_SPEC = "from kpubdata.core.spec import find_spec\n"


def test_a_now_public_entry_fails_against_a_released_kpubdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _gate_run(tmp_path, monkeypatch, capsys, _FIND_SPEC, _NOW_PUBLIC)

    assert code == 1, out
    assert "is public in kpubdata; import it with `from kpubdata import find_spec`" in out


@pytest.mark.parametrize(("flags", "env"), [(("--unreleased-kpubdata",), None), ((), "unreleased")])
def test_a_now_public_entry_is_a_notice_against_an_unreleased_kpubdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
    env: str | None,
) -> None:
    code, out = _gate_run(tmp_path, monkeypatch, capsys, _FIND_SPEC, _NOW_PUBLIC, *flags, env=env)

    assert code == 0, out
    assert "notice:" in out
    assert "switch to `from kpubdata import find_spec` when the pin includes it" in out


def test_an_unused_entry_still_fails_against_an_unreleased_kpubdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing imports find_spec any more: public or not, the entry is dead weight.
    for public in (_PUBLIC, _NOW_PUBLIC):
        result = gate.scan([_write(tmp_path, "from kpubdata import Client\n")], public, _ALLOW)
        assert result.unused == [_ALLOW_KEY]
        assert result.now_public == []

    monkeypatch.setenv(gate.TARGET_ENV, "unreleased")
    monkeypatch.setattr(gate, "ALLOWLIST", _ALLOW)
    monkeypatch.setattr(gate, "_tracked_files", lambda: [_write(tmp_path, "import kpubdata\n")])
    init = tmp_path / "__init__.py"
    init.write_text(f"__all__ = {sorted(_NOW_PUBLIC)!r}\n", encoding="utf-8")

    assert gate.main(["--kpubdata-init", str(init)]) == 1


def test_a_private_new_import_still_fails_against_an_unreleased_kpubdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _FIND_SPEC + "from kpubdata.core.executor import SpecExecutor\n"

    code, out = _gate_run(
        tmp_path, monkeypatch, capsys, source, _NOW_PUBLIC, "--unreleased-kpubdata"
    )

    assert code == 1, out
    assert "SpecExecutor  <- kpubdata private surface" in out


@pytest.mark.parametrize("env", [None, "unreleased"])
def test_a_private_and_used_entry_passes_in_both_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: str | None,
) -> None:
    code, out = _gate_run(tmp_path, monkeypatch, capsys, _FIND_SPEC, _PUBLIC, env=env)

    assert code == 0, out
    assert "notice:" not in out


def test_an_unknown_target_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gate.TARGET_ENV, "main")

    with pytest.raises(SystemExit) as exc:
        gate.main([])

    assert exc.value.code == 2


_ALLOW_KEY = ("used.py", "kpubdata.core.spec", "find_spec")
_ALLOW = {_ALLOW_KEY: "reason (kpubdata#1)"}


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "used.py"
    path.write_text(source, encoding="utf-8")
    return path
