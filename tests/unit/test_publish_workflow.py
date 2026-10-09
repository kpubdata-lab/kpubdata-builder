"""Verify publish workflow structure of scheduled dataset update workflow (#70).

Without GitHub Actions runner, cannot validate execution results; structurally verify
workflow YAML parses and includes reuse/manual trigger, publish script call, secret guard.
Per-dataset cron strategy defined in DATA_FRESHNESS.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
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
    # Must check the secret before anything runs; a missing one fails the run (#1190).
    assert "KPUBDATA_DATAGO_API_KEY" in run_blocks


def test_data_freshness_policy_doc_exists() -> None:
    # Must have single source document for schedule strategy.
    assert (_ROOT / "DATA_FRESHNESS.md").is_file()


# --- The publish step's script, run (#1190) -------------------------------------------


def _publish_script() -> str:
    steps = _load_workflow()["jobs"]["publish"]["steps"]
    (step,) = [s for s in steps if "scripts/publish_to_hf.py" in s.get("run", "")]
    return cast(str, step["run"])


def _run_publish_step(
    tmp_path: Path, *, datago_key: str, hf_token: str, mode: str
) -> tuple[int, str, list[str]]:
    """Run the step as Actions does (bash -e), with ``uv`` replaced by a recorder."""
    calls = tmp_path / "uv-calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n', encoding="utf-8")
    fake_uv.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "KPUBDATA_DATAGO_API_KEY": datago_key,
        "HF_TOKEN": hf_token,
        "CONFIG": "scripts/configs/weather.yaml",
        "MODE": mode,
    }
    result = subprocess.run(
        ["bash", "-e", "-c", _publish_script()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    recorded = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return result.returncode, result.stdout + result.stderr, recorded


def test_a_run_without_the_api_key_fails_and_publishes_nothing(tmp_path: Path) -> None:
    code, output, calls = _run_publish_step(tmp_path, datago_key="", hf_token="t", mode="")

    assert code == 1
    assert "::error::KPUBDATA_DATAGO_API_KEY is not set" in output
    assert calls == []


def test_a_live_run_without_the_hf_token_fails_before_it_builds(tmp_path: Path) -> None:
    code, output, calls = _run_publish_step(tmp_path, datago_key="k", hf_token="", mode="")

    assert code == 1
    assert "::error::HF_TOKEN is not set" in output
    assert calls == []


@pytest.mark.parametrize("mode", ["--local-only", "--dry-run"])
def test_a_local_run_needs_no_hf_token(tmp_path: Path, mode: str) -> None:
    code, _, calls = _run_publish_step(tmp_path, datago_key="k", hf_token="", mode=mode)

    assert code == 0
    assert len(calls) == 1
    assert calls[0].endswith(f"--target hf {mode}")


def test_a_live_run_with_both_secrets_publishes(tmp_path: Path) -> None:
    code, output, calls = _run_publish_step(tmp_path, datago_key="k", hf_token="t", mode="")

    assert code == 0, output
    assert calls == [
        "run --no-sources python scripts/publish_to_hf.py scripts/configs/weather.yaml --target hf"
    ]


def test_no_branch_of_the_step_passes_without_publishing() -> None:
    # The old guard printed a notice and exited 0.
    assert "::notice::" not in _publish_script()
    assert "exit 0" not in _publish_script()
