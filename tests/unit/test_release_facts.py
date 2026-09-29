"""A release states what it was tested with (#718).

The compatibility table pairs an application version with a kpubdata version, and
that pairing has to be what the release gates actually installed — uv.lock's pin, not
pyproject's range. These hold the script to the real files and to the workflow.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_release_facts", _ROOT / "scripts" / "release_facts.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


facts = _load()


def test_the_kpubdata_version_is_the_locked_one_not_the_range() -> None:
    lock = (_ROOT / "uv.lock").read_text(encoding="utf-8")
    locked = re.search(r'name = "kpubdata"\nversion = "([^"]+)"', lock)
    assert locked is not None

    assert facts.tested_kpubdata() == locked.group(1)
    assert "<" not in facts.tested_kpubdata() and ">" not in facts.tested_kpubdata()


def test_the_contract_version_is_the_document_one() -> None:
    contract = yaml.safe_load((_ROOT / "contract" / "builder-api.yaml").read_text("utf-8"))

    assert facts.contract_version() == contract["info"]["version"]


def test_the_line_is_appended_to_the_notes(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("## v9.9.9\n\n- a change\n", encoding="utf-8")

    assert facts.main([str(notes)]) == 0

    text = notes.read_text(encoding="utf-8")
    assert text.startswith("## v9.9.9\n\n- a change\n")
    assert f"Tested with kpubdata **{facts.tested_kpubdata()}**" in text


def test_the_release_records_it_before_the_tag_exists() -> None:
    """After the gates (so the version is the tested one), before the release is created."""
    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "release.yml").read_text("utf-8"))
    steps = [step.get("name") for step in workflow["jobs"]["release"]["steps"]]

    record = steps.index("Record what the release was tested with")
    assert steps.index("Run quality gates") < record < steps.index("Tag and release")
