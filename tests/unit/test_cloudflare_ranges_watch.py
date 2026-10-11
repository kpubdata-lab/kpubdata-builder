"""A difference from Cloudflare's published ranges is one issue (#1236).

``scripts/cloudflare_ranges_watch.py`` runs the comparison that
``check_cloudflare_ranges.py`` makes and records it in one issue. These run it against
a ``gh`` that answers from the test, and read the workflow that schedules it. Fetching
Cloudflare's lists and the firewall itself are checked on the real deployment.
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
_WORKFLOW = _ROOT / ".github" / "workflows" / "cloudflare-ranges.yml"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The watch script imports its two siblings by name, as it does when run as a script.
sys.path.insert(0, str(_ROOT / "scripts"))
try:
    watch = _load_script("cloudflare_ranges_watch")
finally:
    sys.path.remove(str(_ROOT / "scripts"))


class _Gh:
    """A ``gh`` that lists the issues it was given and keeps the bodies it is sent."""

    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(command)
        self.calls.append(command)
        out = ""
        if command[1:3] == ["issue", "list"]:
            out = json.dumps(self.issues)
        elif "--body-file" in command:
            path = Path(command[command.index("--body-file") + 1])
            self.bodies.append(path.read_text(encoding="utf-8"))
            out = "https://github.com/o/r/issues/9\n"
        return subprocess.CompletedProcess(command, 0, out, "")

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] != ["issue", "list"]]


def _record(gh: _Gh, tmp_path: Path, missing: list[str], extra: list[str]) -> str:
    return cast(
        str,
        watch.record(
            missing, extra, gh, today="2026-10-12", run_url="https://run", directory=tmp_path
        ),
    )


def _open_issue(body: str) -> dict[str, Any]:
    return {"number": 9, "title": watch.TITLE, "body": body}


def test_a_difference_with_no_open_issue_opens_one(tmp_path: Path) -> None:
    gh = _Gh()

    done = _record(gh, tmp_path, ["198.51.100.0/24"], ["203.0.113.0/24"])

    assert done.startswith("opened ")
    create = [c for c in gh.writes() if c[1:3] == ["issue", "create"]]
    assert len(create) == 1
    assert create[0][create[0].index("--title") + 1] == watch.TITLE
    assert "epic:byok" in create[0]
    assert "`198.51.100.0/24`" in gh.bodies[0]
    assert "`203.0.113.0/24`" in gh.bodies[0]


def test_no_difference_and_no_open_issue_writes_nothing(tmp_path: Path) -> None:
    gh = _Gh()

    assert _record(gh, tmp_path, [], []) == "no difference and no open issue"
    assert gh.writes() == []


def test_the_same_difference_is_not_written_twice(tmp_path: Path) -> None:
    first = _Gh()
    _record(first, tmp_path, ["198.51.100.0/24"], [])
    gh = _Gh([_open_issue(first.bodies[0])])

    done = _record(gh, tmp_path, ["198.51.100.0/24"], [])

    assert done == "#9 already lists this difference"
    assert gh.writes() == []


def test_a_changed_difference_rewrites_the_list_and_keeps_the_rest(tmp_path: Path) -> None:
    first = _Gh()
    _record(first, tmp_path, ["198.51.100.0/24"], [])
    body = first.bodies[0] + "\nA note from a person.\n"
    gh = _Gh([_open_issue(body)])

    done = _record(gh, tmp_path, ["198.51.100.0/24", "192.0.2.0/24"], [])

    assert done == "updated #9"
    assert [c[1:3] for c in gh.writes()] == [["issue", "edit"], ["issue", "comment"]]
    assert "`192.0.2.0/24`" in gh.bodies[0]
    assert "A note from a person." in gh.bodies[0]


def test_a_difference_that_went_away_is_said_and_the_issue_is_not_closed(tmp_path: Path) -> None:
    first = _Gh()
    _record(first, tmp_path, [], ["203.0.113.0/24"])
    gh = _Gh([_open_issue(first.bodies[0])])

    _record(gh, tmp_path, [], [])

    assert "matching Cloudflare's published ranges again" in gh.bodies[0]
    assert all(c[1:3] != ["issue", "close"] for c in gh.calls)


def test_the_fingerprint_ignores_order_and_tells_missing_from_extra() -> None:
    assert watch.fingerprint(["a", "b"], []) == watch.fingerprint(["b", "a"], [])
    assert watch.fingerprint(["a"], []) != watch.fingerprint([], ["a"])
    assert watch.fingerprint([], []) == "none"


def test_unreadable_published_lists_exit_two(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> list[Any]:
        raise OSError("no network")

    monkeypatch.setattr(watch.ranges, "fetch_published", refuse)

    assert watch.main(["--dry-run"]) == 2


def test_a_dry_run_compares_and_writes_no_issue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = watch.ranges.listed_ranges(watch.ranges.TRUSTED_PROXIES.read_text(encoding="utf-8"))
    monkeypatch.setattr(watch.ranges, "fetch_published", lambda: listed)
    gh = _Gh()

    assert watch.main(["--dry-run"], gh) == 0
    assert gh.calls == []
    assert "matches" in capsys.readouterr().out


# ------------------------------------------------------------------ the workflow


def _workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))


def test_the_workflow_runs_on_a_schedule_and_by_hand() -> None:
    triggers = _workflow()[True]  # PyYAML reads the key `on` as a boolean

    assert len(triggers["schedule"]) == 1
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"dry_run"}
    assert ".github/workflows/cloudflare-ranges.yml" in triggers["pull_request"]["paths"]


def test_a_pull_request_run_writes_no_issue() -> None:
    step = _workflow()["jobs"]["compare"]["steps"][-1]

    assert step["env"]["DRY_RUN"] == "${{ github.event_name == 'pull_request' || inputs.dry_run }}"
    assert 'if [ "${DRY_RUN}" = "true" ]; then\n  args+=(--dry-run)' in step["run"]


def test_the_workflow_may_read_the_repository_and_write_issues_and_nothing_more() -> None:
    workflow = _workflow()

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["compare"]["permissions"] == {"contents": "read", "issues": "write"}


def test_every_action_is_pinned_and_no_secret_but_the_token_is_used() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")

    for ref in re.findall(r"uses:\s*(\S+)", text):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref
    assert "secrets." not in text
