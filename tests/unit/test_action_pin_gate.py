"""The workflow action pin gate has to actually refuse (#1003).

A gate nobody has watched fail is a gate nobody knows works, so most of these feed it a
movable reference and expect a violation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_action_pins.py"

_SHA = "807c21f6a78f22b6ed64e19b38585225ec95e1f7"
_OTHER_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("_action_pin_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()


def _workflow(tmp_path: Path, steps: str) -> Path:
    path = tmp_path / "wf.yml"
    path.write_text(f"jobs:\n  a:\n    steps:\n{steps}", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: kpubdata-lab/kpubdata/.github/actions/r3-review@main\n",
        "        uses: kpubdata-lab/kpubdata/.github/actions/release-notes@main\n",
        "      - uses: actions/checkout@v7\n",
        "      - uses: actions/checkout@v7.0.1  # v7.0.1\n",
        '      - uses: "actions/setup-python@v7"\n',
        "      - uses: 'astral-sh/setup-uv@v7'\n",
        # A short SHA can be ambiguous and is resolved like a ref name.
        "      - uses: actions/checkout@3d3c42e\n",
        "      - uses: actions/checkout\n",
        "      - uses: docker://alpine:3.20\n",
        "    uses: org/repo/.github/workflows/reusable.yml@main\n",
    ],
)
def test_movable_reference_is_refused(tmp_path: Path, line: str) -> None:
    path = _workflow(tmp_path, line)

    violations = gate.check([path])

    assert len(violations) == 1
    assert violations[0].line == 4
    assert gate.main([str(path)]) == 1


@pytest.mark.parametrize(
    "line",
    [
        f"      - uses: kpubdata-lab/kpubdata/.github/actions/r3-review@{_SHA}  # main\n",
        f"      - uses: actions/checkout@{_OTHER_SHA}  # v7.0.1\n",
        f'      - uses: "actions/checkout@{_OTHER_SHA}"\n',
        "    uses: ./.github/workflows/publish-dataset.yml\n",
        "      - uses: docker://alpine@sha256:" + "a" * 64 + "\n",
        # Not a step: text that only mentions a ref is not run.
        "      - run: echo 'uses: actions/checkout@v7'\n",
        "      # - uses: actions/checkout@v7\n",
    ],
)
def test_pinned_or_local_reference_passes(tmp_path: Path, line: str) -> None:
    path = _workflow(tmp_path, line)

    assert gate.check([path]) == []
    assert gate.main([str(path)]) == 0


def test_repository_workflows_are_pinned() -> None:
    paths = gate.default_paths(_ROOT)

    assert any(p.name == "release.yml" for p in paths)
    assert gate.check(paths) == []


def test_bump_moves_every_kpubdata_action_and_nothing_else(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        "      - uses: kpubdata-lab/kpubdata/.github/actions/r3-review@main\n"
        f"      - uses: kpubdata-lab/kpubdata/.github/actions/release-notes@{_OTHER_SHA}  # old\n"
        f"      - uses: actions/checkout@{_OTHER_SHA}  # v7.0.1\n",
    )

    assert gate.bump_kpubdata([path], _SHA) == [path]

    text = path.read_text(encoding="utf-8")
    assert text.count(f"@{_SHA}  # kpubdata-lab/kpubdata main\n") == 2
    assert f"actions/checkout@{_OTHER_SHA}  # v7.0.1\n" in text
    assert gate.check([path]) == []
    # A second run with the same commit changes nothing.
    assert gate.bump_kpubdata([path], _SHA) == []


@pytest.mark.parametrize("sha", ["main", "807c21f", _SHA.upper(), _SHA + "0"])
def test_bump_refuses_anything_but_a_full_sha(tmp_path: Path, sha: str) -> None:
    path = _workflow(tmp_path, "      - uses: kpubdata-lab/kpubdata/.github/actions/x@main\n")

    with pytest.raises(ValueError, match="full 40-character"):
        gate.bump_kpubdata([path], sha)
    assert gate.main(["--bump-kpubdata", sha, str(path)]) == 2
    assert "@main" in path.read_text(encoding="utf-8")
