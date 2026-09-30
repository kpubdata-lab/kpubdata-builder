#!/usr/bin/env python3
"""Refuse imports of kpubdata's private surface (#830, Independence Rule 6).

Builder consumes only kpubdata's public API. An import is private when:

1. any segment of the module path starts with ``_`` (``kpubdata.transport._sensitive``,
   ``kpubdata._hosts``), or
2. it names something ``kpubdata.__all__`` does not export: ``from kpubdata.core.spec
   import find_spec`` or a bare ``import kpubdata.transport.http``. A public symbol
   imported through its defining module (``from kpubdata.exceptions import AuthError``)
   is still that public symbol and passes.

What is swept: every tracked ``*.py`` under ``src/`` and ``scripts/`` (``git ls-files``).
Tests are not swept — they monkeypatch kpubdata internals on purpose.

What is recognised: ``import``/``from ... import`` statements, ``importlib.import_module``
and ``__import__`` with a literal name, and Python source held in a string literal
(``python -c "from kpubdata.core.spec import find_spec"``).

The public names are read from the installed kpubdata's ``__init__.py`` without
importing it. Remaining private uses live in ``ALLOWLIST`` below, each with the reason
and the kpubdata issue that tracks a public replacement. An entry nothing uses any more
also fails, so the list only shrinks.

An entry whose import is still in the code but whose name kpubdata now exports is also
"no longer needed", and against a *released* kpubdata that fails too: switch to
``from kpubdata import X``. Against an unreleased kpubdata (the cross-repo early warning
installs kpubdata ``main``) the switch cannot happen yet — the released kpubdata Builder
pins still keeps the name private, so switching would break the supported range. There
such an entry is printed as a notice instead. The mode is chosen explicitly, by
``--unreleased-kpubdata`` or ``KPUBDATA_IMPORT_GATE_TARGET=unreleased`` (set only by
cross-repo-contract.yml), never inferred from the installed version: kpubdata ``main``
carries the version of its last release (0.8.0 while 0.8.0 is out), so a version check
cannot tell the two apart. An entry nothing imports any more fails in both modes.

Usage:
    python scripts/check_kpubdata_imports.py
    python scripts/check_kpubdata_imports.py --unreleased-kpubdata
    python scripts/check_kpubdata_imports.py --kpubdata-init path/to/__init__.py FILE...
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

ROOTS = ("src", "scripts")

#: Set by cross-repo-contract.yml, which installs kpubdata main instead of a release.
TARGET_ENV = "KPUBDATA_IMPORT_GATE_TARGET"
_TARGETS = ("released", "unreleased")

_VERIFY_REASON = (
    "verify/ re-implements kpubdata's `make verify` against its spec model, executor "
    "and transport; kpubdata has no public verify API (kpubdata#667). Signatures are "
    "pinned by tests/unit/test_kpubdata_internal_surface.py."
)
_SPEC_LOOKUP_REASON = (
    "Looks up a SpecDefinition by id; the public Client returns DatasetRef, not the "
    "spec model verify needs (kpubdata#667)."
)
_CONFIG_REASON = (
    "Reads the operator's provider key the way kpubdata resolves it from the "
    "environment; Client does not expose key resolution publicly (kpubdata#667)."
)

#: (path, module, name) -> reason. ``name`` is "" for ``import module``.
ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "src/kpubdata_builder/logging_redaction.py",
        "kpubdata.transport._sensitive",
        "SENSITIVE_PARAM_KEYS",
    ): (
        "The one canonical list of credential parameter names; Builder keeps no copy "
        "so the two cannot drift. Needs a public export (kpubdata#667)."
    ),
    ("src/kpubdata_builder/service/providers.py", "kpubdata.config", "KPubDataConfig"): (
        _CONFIG_REASON
    ),
    ("src/kpubdata_builder/verify/runner.py", "kpubdata.config", "KPubDataConfig"): (
        _VERIFY_REASON
    ),
    ("src/kpubdata_builder/verify/runner.py", "kpubdata.core.executor", "SpecExecutor"): (
        _VERIFY_REASON
    ),
    (
        "src/kpubdata_builder/verify/runner.py",
        "kpubdata.core.executor",
        "check_payload_error",
    ): _VERIFY_REASON,
    ("src/kpubdata_builder/verify/runner.py", "kpubdata.core.executor", "extract_items"): (
        _VERIFY_REASON
    ),
    (
        "src/kpubdata_builder/verify/runner.py",
        "kpubdata.core.executor",
        "extract_total_count",
    ): _VERIFY_REASON,
    ("src/kpubdata_builder/verify/runner.py", "kpubdata.core.spec", "ExampleSpec"): (
        _VERIFY_REASON
    ),
    ("src/kpubdata_builder/verify/runner.py", "kpubdata.core.spec", "SpecDefinition"): (
        _VERIFY_REASON
    ),
    ("src/kpubdata_builder/verify/runner.py", "kpubdata.transport.http", "HttpTransport"): (
        _VERIFY_REASON
    ),
    ("src/kpubdata_builder/agent/monitor.py", "kpubdata.core.spec", "find_spec"): (
        _SPEC_LOOKUP_REASON
    ),
    ("src/kpubdata_builder/agent/pipeline.py", "kpubdata.core.spec", "find_spec"): (
        _SPEC_LOOKUP_REASON
    ),
    ("src/kpubdata_builder/cli.py", "kpubdata.core.spec", "find_spec"): _SPEC_LOOKUP_REASON,
    ("src/kpubdata_builder/cli.py", "kpubdata.core.spec", "discover_specs"): (_SPEC_LOOKUP_REASON),
}


class Use(NamedTuple):
    path: str
    line: int
    module: str
    name: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.module, self.name)

    def __str__(self) -> str:
        target = f"from {self.module} import {self.name}" if self.name else f"import {self.module}"
        return f"{self.path}:{self.line}: {target}"


def _is_kpubdata(module: str) -> bool:
    return module == "kpubdata" or module.startswith("kpubdata.")


def _uses_in(tree: ast.AST, path: str, line_offset: int = 0) -> Iterator[Use]:
    for node in ast.walk(tree):
        line = line_offset + getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_kpubdata(alias.name):
                    yield Use(path, line, alias.name, "")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _is_kpubdata(node.module):
                for alias in node.names:
                    yield Use(path, line, node.module, alias.name)
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0] if node.args else None
            if (
                called in ("import_module", "__import__")
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _is_kpubdata(first.value)
            ):
                yield Use(path, line, first.value, "")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "kpubdata" in node.value
            and "import" in node.value
        ):
            # Source code passed to a subprocess (`python -c "..."`).
            try:
                inner = ast.parse(node.value)
            except SyntaxError:
                continue
            yield from _uses_in(inner, path, line - 1)


def uses(path: Path, display: str) -> list[Use]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=display)
    return sorted(set(_uses_in(tree, display)), key=lambda u: (u.line, u.module, u.name))


def is_private(use: Use, public: frozenset[str]) -> bool:
    if any(part.startswith("_") for part in use.module.split(".")):
        return True
    if not use.name:
        # `import kpubdata` is the public root; any submodule import reaches past it.
        return use.module != "kpubdata"
    return use.name not in public


def public_names(init_path: Path) -> frozenset[str]:
    """Return ``__all__`` from kpubdata's ``__init__.py``, read without importing it."""
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        value = getattr(node, "value", None)
        names_all = any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)
        if value is not None and names_all:
            return frozenset(str(name) for name in ast.literal_eval(value))
    raise SystemExit(f"error: no literal __all__ in {init_path}")


def _installed_init() -> Path:
    spec = importlib.util.find_spec("kpubdata")
    if spec is None or spec.origin is None:
        raise SystemExit("error: kpubdata is not installed; pass --kpubdata-init")
    return Path(spec.origin)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--", *(f"{root}/*.py" for root in ROOTS)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [Path(line) for line in out.splitlines() if line]


class Result(NamedTuple):
    violations: list[Use]
    """Private uses no allowlist entry covers."""
    unused: list[tuple[str, str, str]]
    """Allowlist entries whose import is gone from the code."""
    now_public: list[tuple[str, str, str]]
    """Allowlist entries whose import is still there but now names a public symbol."""


def scan(
    files: list[Path],
    public: frozenset[str],
    allowlist: dict[tuple[str, str, str], str],
) -> Result:
    violations: list[Use] = []
    private_seen: set[tuple[str, str, str]] = set()
    public_seen: set[tuple[str, str, str]] = set()
    for path in files:
        display = path.as_posix()
        for use in uses(path, display):
            if not is_private(use, public):
                public_seen.add(use.key)
                continue
            if use.key in allowlist:
                private_seen.add(use.key)
            else:
                violations.append(use)
    not_needed = set(allowlist) - private_seen
    now_public = sorted(not_needed & public_seen)
    unused = sorted(not_needed - public_seen)
    return Result(violations, unused, now_public)


def check(
    files: list[Path],
    public: frozenset[str],
    allowlist: dict[tuple[str, str, str], str],
) -> tuple[list[Use], list[tuple[str, str, str]]]:
    """Return (private uses not allowlisted, allowlist entries no longer needed).

    This is the released-kpubdata rule: a now-public entry counts as not needed.
    """
    result = scan(files, public, allowlist)
    return result.violations, sorted(result.unused + result.now_public)


def _installed_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("kpubdata")
    except PackageNotFoundError:
        return "(unknown version)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="default: git ls-files src scripts")
    parser.add_argument("--kpubdata-init", type=Path, help="kpubdata/__init__.py to read")
    parser.add_argument(
        "--unreleased-kpubdata",
        action="store_true",
        help=f"kpubdata under test is not a release (also {TARGET_ENV}=unreleased): "
        "report allowlist entries whose name is now public as notices, not failures",
    )
    args = parser.parse_args(argv)

    target = os.environ.get(TARGET_ENV, "released")
    if target not in _TARGETS:
        parser.error(f"{TARGET_ENV}={target!r}; expected one of {', '.join(_TARGETS)}")
    unreleased = args.unreleased_kpubdata or target == "unreleased"

    public = public_names(args.kpubdata_init or _installed_init())
    files = args.files or _tracked_files()
    result = scan(files, public, ALLOWLIST)
    unused, now_public = result.unused, result.now_public
    # An explicit file list checks only those files, so unused entries are expected.
    if args.files:
        unused = []
    notices: list[tuple[str, str, str]] = []
    if unreleased:
        notices, now_public = now_public, []

    for use in result.violations:
        print(f"{use}  <- kpubdata private surface (not in kpubdata.__all__ or a _module)")
    for path, module, name in unused:
        print(f"{path}: allowlist entry ({module}, {name or '<module>'}) is no longer used")
    for path, module, name in now_public:
        print(
            f"{path}: allowlist entry ({module}, {name}) is public in kpubdata; "
            f"import it with `from kpubdata import {name}` and remove the entry"
        )
    if notices:
        # With --kpubdata-init the installed distribution may be a different kpubdata.
        installed = (
            f"read from {args.kpubdata_init}" if args.kpubdata_init else _installed_version()
        )
        for path, module, name in notices:
            print(
                f"notice: {path}: allowlist entry ({module}, {name}) is public in kpubdata "
                f"{installed} (unreleased); switch to `from kpubdata import {name}` when "
                "the pin includes it"
            )
    if result.violations or unused or now_public:
        print(
            "\nUse `from kpubdata import ...` for public names. If there is no public "
            "equivalent, add an ALLOWLIST entry in scripts/check_kpubdata_imports.py that "
            "names the kpubdata issue tracking one; remove entries that are no longer used."
        )
        return 1
    print(f"checked {len(files)} files: no unlisted kpubdata private imports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
