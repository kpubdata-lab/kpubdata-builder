"""Verify publish workflow structure of scheduled dataset update workflow (#70).

Without GitHub Actions runner, cannot validate execution results; structurally verify
workflow YAML parses and includes reuse/manual trigger, publish script call, secret guard.
Per-dataset cron strategy defined in DATA_FRESHNESS.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_ROOT = Path(__file__).parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "publish-dataset.yml"


def _load_workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses bare `on:` key as boolean True (GitHub Actions convention).
    raw = workflow.get("on", workflow.get(True))
    return cast(dict[str, Any], raw)


def test_workflow_file_exists_and_parses() -> None:
    assert _WORKFLOW.is_file()
    assert isinstance(_load_workflow(), dict)


def test_supports_reusable_and_manual_triggers() -> None:
    triggers = _triggers(_load_workflow())

    assert "workflow_call" in triggers
    assert "workflow_dispatch" in triggers
    # Reusable call requires config input.
    assert "config" in triggers["workflow_call"]["inputs"]


def test_publish_step_invokes_publish_script_with_guard() -> None:
    workflow = _load_workflow()
    steps = workflow["jobs"]["publish"]["steps"]
    run_blocks = "\n".join(step.get("run", "") for step in steps)

    assert "scripts/publish_to_hf.py" in run_blocks
    # Must have guard that skips live execution when secret is not set.
    assert "KPUBDATA_DATAGO_API_KEY" in run_blocks


def test_data_freshness_policy_doc_exists() -> None:
    # Must have single source document for schedule strategy.
    assert (_ROOT / "DATA_FRESHNESS.md").is_file()
