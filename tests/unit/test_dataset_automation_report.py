"""The agent commands run on a schedule and report in one issue (kpubdata#448).

``scripts/dataset_automation_report.py`` runs ``monitor --check`` and ``discover``
and records what they find. These run it against a command runner and a ``gh`` that
answer from the test, and read the workflow that schedules it. The live API and
data.go.kr are not reached here.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "dataset-automation.yml"
_SCRIPT = _ROOT / "scripts" / "dataset_automation_report.py"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The script imports image_rescan by name, as it does when run as a script.
sys.path.insert(0, str(_ROOT / "scripts"))
try:
    report = _load_script("dataset_automation_report")
finally:
    sys.path.remove(str(_ROOT / "scripts"))

_KEY = {"KPUBDATA_DATAGO_API_KEY": "test-key"}
_PAGE = "https://www.data.go.kr/data/15059468/openapi.do"
_PENDING = [
    report.Pending("datago.mid_fcst", _PAGE),
    report.Pending("datago.mid_ta", _PAGE),
    report.Pending("semas.zone_one", "https://www.data.go.kr"),
]


class _Runner:
    """Answers ``kpubdata-builder`` and ``gh`` from the test, and keeps every call."""

    def __init__(
        self,
        *,
        approved: Sequence[str] = (),
        discover: tuple[int, str, str] = (0, "  Endpoint: https://apis.data.go.kr/x/op\n", ""),
        monitor_exit: int = 0,
        issues: list[dict[str, Any]] | None = None,
    ) -> None:
        self.approved = set(approved)
        self.discover = discover
        self.monitor_exit = monitor_exit
        self.issues = issues or []
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(command)
        self.calls.append(command)
        if command[:2] == ["kpubdata-builder", "monitor"]:
            if self.monitor_exit:
                return subprocess.CompletedProcess(command, self.monitor_exit, "", "boom\n")
            state = Path(command[command.index("--state-file") + 1])
            data = yaml.safe_load(state.read_text(encoding="utf-8"))
            for item in data["pending"]:
                if item["dataset_id"] in self.approved:
                    item["status"] = "HEALTHY"
            state.write_text(yaml.safe_dump(data), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["kpubdata-builder", "discover"]:
            code, out, err = self.discover
            return subprocess.CompletedProcess(command, code, out, err)
        out = ""
        if command[1:3] == ["issue", "list"]:
            out = json.dumps(self.issues)
        elif "--body-file" in command:
            path = Path(command[command.index("--body-file") + 1])
            self.bodies.append(path.read_text(encoding="utf-8"))
            out = "https://github.com/o/r/issues/9\n"
        return subprocess.CompletedProcess(command, 0, out, "")

    def commands(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == name]

    def writes(self) -> list[list[str]]:
        return [c for c in self.commands("gh") if c[1:3] != ["issue", "list"]]


def _main(runner: _Runner, tmp_path: Path, *extra: str, env: dict[str, str] = _KEY) -> int:
    return cast(
        int,
        report.main(
            ["--work-dir", str(tmp_path), "--run-url", "https://run", *extra],
            run=runner,
            env=env,
            catalogue=lambda: list(_PENDING),
        ),
    )


def _open_issue(body: str) -> dict[str, Any]:
    return {"number": 9, "title": report.TITLE, "body": body}


# ------------------------------------------------------------------ the secret


@pytest.mark.parametrize(
    "env", [{}, {"KPUBDATA_DATAGO_API_KEY": ""}, {"KPUBDATA_DATAGO_API_KEY": " "}]
)
def test_without_the_secret_the_run_is_skipped_with_a_notice_and_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], env: dict[str, str]
) -> None:
    runner = _Runner(approved=["datago.mid_fcst"])
    summary = tmp_path / "summary.md"

    def catalogue() -> list[Any]:
        raise AssertionError("the catalogue is not read without the secret")

    code = report.main(
        ["--work-dir", str(tmp_path)],
        run=runner,
        env={**env, "GITHUB_STEP_SUMMARY": str(summary)},
        catalogue=catalogue,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("::notice title=Dataset automation report skipped::")
    assert "KPUBDATA_DATAGO_API_KEY is not configured" in out
    assert "is not configured" in summary.read_text(encoding="utf-8")
    assert runner.calls == []


def test_without_the_secret_a_dry_run_is_skipped_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Runner()

    assert _main(runner, tmp_path, "--dry-run", env={}) == 0
    assert "is not configured" in capsys.readouterr().out
    assert runner.calls == []


# ------------------------------------------------------------------ the commands


def test_monitor_checks_every_pending_dataset_from_a_state_file_in_the_work_dir(
    tmp_path: Path,
) -> None:
    runner = _Runner(approved=["datago.mid_ta"])

    statuses = report.run_monitor(_PENDING, runner, tmp_path)

    (call,) = runner.commands("kpubdata-builder")
    assert call == [
        "kpubdata-builder",
        "monitor",
        "--state-file",
        str(tmp_path / "monitor-state.yaml"),
        "--check",
    ]
    assert statuses == {
        "datago.mid_fcst": "NEEDS_APPLICATION",
        "datago.mid_ta": "HEALTHY",
        "semas.zone_one": "NEEDS_APPLICATION",
    }


def test_a_failing_monitor_exits_one_and_writes_no_issue(tmp_path: Path) -> None:
    runner = _Runner(monitor_exit=1)

    assert _main(runner, tmp_path) == 1
    assert runner.writes() == []


def test_discover_runs_once_per_api_page_and_writes_no_spec(tmp_path: Path) -> None:
    runner = _Runner()

    (found,) = report.run_discover(_PENDING, runner, limit=30)

    assert runner.commands("kpubdata-builder") == [["kpubdata-builder", "discover", _PAGE]]
    assert found.datasets == ("datago.mid_fcst", "datago.mid_ta")
    assert found.outcome == "endpoint"
    assert found.detail == "https://apis.data.go.kr/x/op"


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ((3, "", "error: discovery unreachable: 403\n"), "unreachable"),
        ((1, "", "error: discovery failed: x\n"), "failed"),
        ((0, "  Endpoint: /\n", ""), "no endpoint"),
    ],
)
def test_discover_outcomes_follow_the_exit_status(
    answer: tuple[int, str, str], outcome: str
) -> None:
    (found,) = report.run_discover(_PENDING, _Runner(discover=answer), limit=30)

    assert found.outcome == outcome


def test_pipeline_is_never_run(tmp_path: Path) -> None:
    runner = _Runner(approved=["datago.mid_fcst"])

    assert _main(runner, tmp_path) == 0
    assert all(call[1] != "pipeline" for call in runner.commands("kpubdata-builder"))
    assert all("pr" not in call[1:2] for call in runner.commands("gh"))


# ------------------------------------------------------------------ the issue


def test_an_approval_with_no_open_issue_opens_one(tmp_path: Path) -> None:
    runner = _Runner(approved=["datago.mid_fcst"])

    assert _main(runner, tmp_path) == 0

    (create,) = runner.writes()
    assert create[1:3] == ["issue", "create"]
    assert create[create.index("--label") + 1] == "epic:datasets"
    (body,) = runner.bodies
    assert "- `datago.mid_fcst`" in body
    assert "## What to do" in body


def test_nothing_to_act_on_and_no_open_issue_writes_nothing(tmp_path: Path) -> None:
    runner = _Runner(discover=(3, "", "unreachable\n"))

    assert _main(runner, tmp_path) == 0
    assert runner.writes() == []


def test_the_same_report_is_not_written_twice(tmp_path: Path) -> None:
    first = _Runner(approved=["datago.mid_fcst"])
    _main(first, tmp_path)
    again = _Runner(approved=["datago.mid_fcst"], issues=[_open_issue(first.bodies[0])])

    assert _main(again, tmp_path) == 0
    assert again.writes() == []


def test_a_changed_report_rewrites_the_lists_and_keeps_the_rest(tmp_path: Path) -> None:
    first = _Runner(approved=["datago.mid_fcst"])
    _main(first, tmp_path)
    body = first.bodies[0] + "\n## Notes\n\nwritten by a person\n"
    again = _Runner(approved=["datago.mid_ta"], issues=[_open_issue(body)])

    assert _main(again, tmp_path) == 0

    edit, comment = again.writes()
    assert edit[1:4] == ["issue", "edit", "9"]
    assert comment[1:4] == ["issue", "comment", "9"]
    assert "- `datago.mid_ta`" in again.bodies[0]
    assert "- `datago.mid_fcst`" not in again.bodies[0]
    assert "written by a person" in again.bodies[0]
    assert all(c[1:3] != ["issue", "close"] for c in again.writes())


def test_the_fingerprint_ignores_order_and_other_statuses() -> None:
    discovered = [report.Discovery(_PAGE, ("a",), "endpoint", "x")]
    one = {"a": "HEALTHY", "b": "NEEDS_APPLICATION"}
    other = {"b": "RATE_LIMITED", "a": "HEALTHY"}

    assert report.fingerprint(one, discovered) == report.fingerprint(other, discovered)
    assert report.fingerprint({}, []) == "none"


def test_a_dry_run_runs_and_writes_no_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Runner(approved=["datago.mid_fcst"])

    assert _main(runner, tmp_path, "--dry-run") == 0
    assert runner.commands("gh") == []
    assert "<!-- dataset-automation:end -->" in capsys.readouterr().out


def test_an_unreadable_catalogue_exits_two(tmp_path: Path) -> None:
    def catalogue() -> list[Any]:
        raise OSError("no catalogue")

    code = report.main(["--work-dir", str(tmp_path)], run=_Runner(), env=_KEY, catalogue=catalogue)

    assert code == 2


# ------------------------------------------------------------------ the workflow


def _workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))


def test_the_workflow_runs_on_a_schedule_and_by_hand() -> None:
    triggers = _workflow()[True]  # PyYAML reads the key `on` as a boolean

    assert len(triggers["schedule"]) == 1
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"dry_run"}
    assert ".github/workflows/dataset-automation.yml" in triggers["pull_request"]["paths"]
    assert "scripts/dataset_automation_report.py" in triggers["pull_request"]["paths"]


def test_a_pull_request_run_writes_no_issue() -> None:
    step = _workflow()["jobs"]["report"]["steps"][-1]

    assert step["env"]["DRY_RUN"] == "${{ github.event_name == 'pull_request' || inputs.dry_run }}"
    assert 'if [ "${DRY_RUN}" = "true" ]; then\n  args+=(--dry-run)' in step["run"]
    assert "scripts/dataset_automation_report.py" in step["run"]


def test_the_workflow_may_read_the_repository_and_write_issues_and_nothing_more() -> None:
    workflow = _workflow()

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["report"]["permissions"] == {"contents": "read", "issues": "write"}


def test_every_action_is_pinned_and_only_the_data_go_kr_key_is_a_secret() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")

    for ref in re.findall(r"uses:\s*(\S+)", text):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref
    assert re.findall(r"secrets\.(\w+)", text) == ["KPUBDATA_DATAGO_API_KEY"]
    # The secret reaches only the step that runs the script, not the whole job.
    step = _workflow()["jobs"]["report"]["steps"][-1]
    assert step["env"]["KPUBDATA_DATAGO_API_KEY"] == "${{ secrets.KPUBDATA_DATAGO_API_KEY }}"
    assert "env" not in _workflow()["jobs"]["report"]


def test_the_script_reads_the_secret_by_the_name_the_workflow_passes() -> None:
    assert report.SECRET == "KPUBDATA_DATAGO_API_KEY"
    assert report.SECRET in _SCRIPT.read_text(encoding="utf-8")
