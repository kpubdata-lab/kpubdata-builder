"""The Studio drift job runs Studio's test against this contract and gates CI (#1004).

The job itself needs GitHub Actions and Studio's checkout, so its shape is checked here:
it is covered by `CI gate`, it is never skipped by a condition, it runs Studio ``main``'s
drift test with ``BUILDER_CONTRACT`` pointing at this repository's contract, and the pull
request body reaches it only through the environment. The follow-up line parser is tested
directly, mostly with lines it must not accept.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_SCRIPT = _ROOT / "scripts" / "studio_drift_follow_up.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("_studio_drift_follow_up", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _jobs() -> dict[str, Any]:
    workflow = cast(dict[Any, Any], yaml.safe_load(_CI.read_text(encoding="utf-8")))
    return cast(dict[str, Any], workflow["jobs"])


def _job() -> dict[str, Any]:
    return cast(dict[str, Any], _jobs()["studio-drift"])


def _step(name: str) -> dict[str, Any]:
    steps = [s for s in _job()["steps"] if s.get("name") == name]
    assert len(steps) == 1, name
    return cast(dict[str, Any], steps[0])


def test_the_gate_covers_the_job() -> None:
    gate = _jobs()["gate"]

    assert "studio-drift" in gate["needs"]
    assert gate["if"] == "always()"


def test_the_job_is_never_skipped_and_has_no_matrix() -> None:
    # A required job that is skipped, or whose name carries a matrix suffix, is a check
    # some pull requests never produce — they stay BLOCKED for ever.
    job = _job()

    assert "if" not in job
    assert "strategy" not in job
    assert job["name"] == "Studio drift"


def test_the_change_detection_watches_the_contract() -> None:
    run = _step("Did the contract change?")["run"]

    assert "-- contract" in run
    assert "scripts/studio_drift_follow_up.py" in run
    assert 'echo "run=true"' in run


def test_studio_main_is_checked_out_without_credentials() -> None:
    step = _step("Checkout Studio (main)")

    assert step["with"]["repository"] == "kpubdata-lab/kpubdata-studio"
    assert step["with"]["ref"] == "main"
    assert step["with"]["persist-credentials"] is False


def test_studio_drift_test_reads_this_contract() -> None:
    step = _step("Studio's drift test against this contract")

    assert step["working-directory"] == ".studio"
    assert step["env"]["BUILDER_CONTRACT"] == "${{ github.workspace }}/contract/builder-api.yaml"
    assert "npx vitest run src/shared/lib/contractDrift.test.ts" in step["run"]
    # Without a follow-up, drift fails the job.
    assert step["run"].rstrip().endswith("exit 1")


def test_every_studio_step_waits_for_a_contract_change() -> None:
    for step in _job()["steps"][2:]:
        assert step["if"] == "steps.changed.outputs.run == 'true'", step["name"]


def test_the_pull_request_body_never_reaches_a_shell_as_text() -> None:
    # `${{ }}` in `run:` is pasted into the script before the shell reads it, so a body
    # holding `$(...)` would run. No event field is interpolated into a script.
    for step in _job()["steps"]:
        assert "github.event" not in step.get("run", ""), step["name"]


def test_the_body_is_read_when_the_step_runs() -> None:
    # A re-run replays the original event, so a line added to the body afterwards is
    # seen only if the body is fetched at run time rather than taken from the event.
    step = _step("Studio follow-up named in the pull request")

    assert "body" not in " ".join(step["env"].values())
    assert step["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert 'gh api "repos/${REPO}/pulls/${PR_NUMBER}"' in step["run"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Studio-Follow-Up: kpubdata-lab/kpubdata-studio#123", 123),
        ("Closes #1\n\nStudio-Follow-Up: kpubdata-lab/kpubdata-studio#7  \nmore", 7),
        ("Closes #1\r\nStudio-Follow-Up:\tkpubdata-lab/kpubdata-studio#42\r\n", 42),
    ],
)
def test_a_follow_up_line_is_read(body: str, expected: int) -> None:
    assert script.follow_up(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        # A bare number reads as a Builder issue to everyone else.
        "Studio-Follow-Up: #123",
        "Studio-Follow-Up: kpubdata-lab/kpubdata-builder#123",
        "Studio-Follow-Up: https://github.com/kpubdata-lab/kpubdata-studio/issues/123",
        "Studio-Follow-Up: kpubdata-lab/kpubdata-studio#0",
        "Studio-Follow-Up: kpubdata-lab/kpubdata-studio#12a",
        # Mentioned inside a sentence, or quoted, is not the line.
        "See Studio-Follow-Up: kpubdata-lab/kpubdata-studio#123",
        "> Studio-Follow-Up: kpubdata-lab/kpubdata-studio#123",
        "studio-follow-up: kpubdata-lab/kpubdata-studio#123",
    ],
)
def test_anything_else_is_not_a_follow_up(body: str | None) -> None:
    assert script.follow_up(body) is None


def test_the_script_prints_the_number_or_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PR_BODY", "Studio-Follow-Up: kpubdata-lab/kpubdata-studio#9")
    assert script.main() == 0
    assert capsys.readouterr().out == "9\n"

    monkeypatch.setenv("PR_BODY", "$(touch /tmp/pwned)")
    assert script.main() == 0
    assert capsys.readouterr().out == ""

    monkeypatch.delenv("PR_BODY")
    assert script.main() == 0
    assert capsys.readouterr().out == ""
