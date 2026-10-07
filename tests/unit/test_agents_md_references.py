"""What AGENTS.md tells an agent to run exists (#1144).

AGENTS.md told agents to run the release workflow with ``dry_run`` — an input
``release.yml`` never had; its default ``mode=prepare`` opens a real release pull
request. It also described a comment ratchet CI no longer runs and an exporter
signature ``exporters/base.py`` does not have. These checks read the file the way an
agent does and ask the repository whether each thing it names is there.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, cast

import yaml

from kpubdata_builder.exporters.base import BaseExporter

_ROOT = Path(__file__).parents[2]
_AGENTS = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
_WORKFLOWS = _ROOT / ".github" / "workflows"


def _lines_naming(pattern: str) -> list[tuple[str, str]]:
    """(line, match) for each backticked match of ``pattern`` in AGENTS.md."""
    return [
        (line, match)
        for line in _AGENTS.splitlines()
        for match in re.findall(rf"`({pattern})`", line)
    ]


def test_every_script_it_names_exists() -> None:
    named = [
        path
        for line, path in _lines_naming(r"scripts/[\w./-]+\.py")
        # Scripts of the kpubdata repository are named as such on their line.
        if "kpubdata's" not in line and "(in kpubdata)" not in line
    ]
    assert named, "AGENTS.md names no script; the pattern is wrong"
    assert [path for path in named if not (_ROOT / path).is_file()] == []


def test_every_workflow_it_names_exists() -> None:
    named = {name for _, name in _lines_naming(r"[\w-]+\.yml")}
    assert "release.yml" in named
    assert sorted(name for name in named if not (_WORKFLOWS / name).is_file()) == []


def _dispatch_inputs(workflow: str) -> set[str]:
    loaded = cast(dict[Any, Any], yaml.safe_load((_WORKFLOWS / workflow).read_text()))
    triggers = cast(dict[str, Any], loaded.get("on", loaded.get(True)))
    return set(triggers["workflow_dispatch"]["inputs"])


def test_every_release_input_it_names_is_one_release_yml_takes() -> None:
    """On a line about dispatching a release, a backticked ``snake_case`` word or
    ``key=value`` names an input — ``dry_run`` was one release.yml never had."""
    inputs = _dispatch_inputs("release.yml")
    named = {
        token.split("=", 1)[0]
        for line, token in _lines_naming(r"[a-z]+(?:_[a-z]+)*(?:=[\w-]+)?")
        if "dispatch" in line or "release workflow" in line
        if "_" in token or "=" in token
    }
    assert named, "AGENTS.md names no release input; the pattern is wrong"
    assert sorted(named - inputs) == []
    assert "dry_run" not in _AGENTS


def test_the_comment_gate_it_describes_is_the_one_ci_runs() -> None:
    ci = (_WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    assert "scripts/check_english_comments.py src tests scripts" in ci
    assert "`scripts/check_english_comments.py src tests scripts`" in _AGENTS


def test_the_exporter_signature_it_gives_is_base_exporters() -> None:
    (described,) = re.findall(r"`export\(self, ([^)]*)\) -> (\w+)`", _AGENTS)
    params, returned = described
    signature = inspect.signature(BaseExporter.export)

    assert [p.split(":")[0].strip() for p in params.split(",")] == list(signature.parameters)[1:]
    assert returned == signature.return_annotation
    assert isinstance(inspect.getattr_static(BaseExporter, "name"), property)
