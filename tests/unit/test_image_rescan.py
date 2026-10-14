"""Deployed images are scanned again, with one incident per image component (#1107).

``docker.yml`` scans Builder's image when it is built; nothing scanned it afterwards,
or the images the compose files pull from elsewhere. ``scripts/image_rescan.py`` does,
and these run it against a Trivy and a ``gh`` that answer from the test: which images
it scans, what it reads from a report, and that the same findings are never filed
twice. The scan itself runs in ``.github/workflows/image-rescan.yml``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "image-rescan.yml"
_DEPENDABOT = _ROOT / ".github" / "dependabot.yml"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rescan = _load_script("image_rescan")

_IMAGE = "caddy:2.11.6-alpine"
_TITLE = "fix(security): caddy image has HIGH or CRITICAL vulnerabilities"


def _vulnerability(identifier: str, fixed: str | None = "1.2.4", **extra: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "VulnerabilityID": identifier,
        "PkgName": "stdlib",
        "InstalledVersion": "1.2.3",
        "Severity": "HIGH",
        **extra,
    }
    if fixed is not None:
        item["FixedVersion"] = fixed
    return item


def _report(*vulnerabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "Metadata": {"RepoDigests": ["caddy@sha256:" + "a" * 64]},
        "Results": [{"Target": "usr/bin/caddy", "Vulnerabilities": list(vulnerabilities)}],
    }


class _Tools:
    """A Trivy that answers with one report and a ``gh`` that keeps what it is told."""

    def __init__(self, report: dict[str, Any], issues: list[dict[str, Any]] | None = None):
        self.report = report
        self.issues = issues or []
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []
        self.fail: str | None = None

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(command)
        self.calls.append(command)
        if self.fail and self.fail in " ".join(command):
            return subprocess.CompletedProcess(command, 1, "", "refused")
        out = ""
        if command[0] == "trivy":
            out = json.dumps(self.report)
        elif command[1:3] == ["issue", "list"]:
            out = json.dumps(self.issues)
        elif "--body-file" in command:
            path = Path(command[command.index("--body-file") + 1])
            self.bodies.append(path.read_text(encoding="utf-8"))
            out = "https://github.com/o/r/issues/7\n"
        return subprocess.CompletedProcess(command, 0, out, "")

    def gh(self, *verb: str) -> list[list[str]]:
        return [call for call in self.calls if call[: 1 + len(verb)] == ["gh", *verb]]


def _record(tools: _Tools, tmp_path: Path) -> str:
    scan = rescan.scan_image(_IMAGE, tools)
    return cast(
        str,
        rescan.record(scan, tools, today="2026-10-11", run_url="https://run", directory=tmp_path),
    )


def _filed_body(tools: _Tools, tmp_path: Path) -> str:
    """The body of the issue a first run opens for ``tools``' report."""
    first = _Tools(tools.report)
    _record(first, tmp_path)
    return first.bodies[0]


# ------------------------------------------------------------------ which images


def test_every_image_of_the_deployment_files_is_scanned() -> None:
    named: list[str] = []
    for name in rescan.COMPOSE_FILES:
        services = yaml.safe_load((_ROOT / name).read_text(encoding="utf-8"))["services"]
        named += [service["image"] for service in services.values() if "image" in service]

    assert len(named) == 2
    # Builder's image is the operator's to name (#1337); the scan reads the newest
    # main build for it. Caddy is read as pinned, tag and digest.
    assert rescan.deployed_images() == [
        "ghcr.io/kpubdata-lab/kpubdata-builder:main",
        "caddy:2.11.6-alpine@sha256:d44355d3c2149dc580ce2cac735955d1c08d3d00882c30489c241aa51a5c10d9",
    ]


def test_every_compose_file_in_the_repository_is_read() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "*compose*.yml", "*compose*.yaml"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()

    assert sorted(listed) == sorted(rescan.COMPOSE_FILES)


def test_a_variable_is_read_as_its_default_and_one_without_is_refused() -> None:
    text = 'services:\n  a:\n    image: "${IMAGE:-example/app:1}"  # pinned\n  b:\n    image: x:2\n'

    assert rescan.images_in(text) == ["example/app:1", "x:2"]
    with pytest.raises(ValueError, match="without a default"):
        rescan.images_in("    image: ${IMAGE}\n")


def test_a_required_variable_is_read_from_the_scan_list_and_an_unknown_one_is_refused() -> None:
    required = "    image: ${BUILDER_IMAGE:?set it to a release, e.g. repo:v1@sha256:...}\n"

    assert rescan.images_in(required) == [rescan.REQUIRED_IMAGES["BUILDER_IMAGE"]]
    with pytest.raises(ValueError, match="without a default"):
        rescan.images_in("    image: ${OTHER_IMAGE:?say what to set}\n")


def test_a_reference_pinned_by_tag_and_digest_is_read_whole() -> None:
    pinned = "caddy:2.11.6-alpine@sha256:" + "d" * 64

    assert rescan.images_in(f"    image: {pinned}  # comment\n") == [pinned]
    assert rescan.component_for(pinned) == "caddy"
    assert rescan.title_for(pinned) == _TITLE


# ------------------------------------------------------------------ what is read


def test_a_report_keeps_high_and_critical_with_and_without_a_fix() -> None:
    report = _report(
        _vulnerability("CVE-2026-0002", fixed=None),
        _vulnerability("CVE-2026-0001", Severity="CRITICAL"),
        _vulnerability("CVE-2026-0003", Severity="MEDIUM"),
        _vulnerability("CVE-2026-0002", fixed=None),
    )

    scan = rescan.parse_report(_IMAGE, report)

    assert scan.digest == "caddy@sha256:" + "a" * 64
    assert [(f.vulnerability, f.severity, f.fixed) for f in scan.findings] == [
        ("CVE-2026-0001", "CRITICAL", "1.2.4"),
        ("CVE-2026-0002", "HIGH", ""),
    ]


def test_the_fingerprint_ignores_order_and_changes_when_a_fix_appears() -> None:
    one, two = _vulnerability("CVE-2026-0001"), _vulnerability("CVE-2026-0002", fixed=None)
    forward = rescan.parse_report(_IMAGE, _report(one, two)).findings
    backward = rescan.parse_report(_IMAGE, _report(two, one)).findings
    fixed = rescan.parse_report(_IMAGE, _report(one, _vulnerability("CVE-2026-0002"))).findings

    assert rescan.fingerprint(forward) == rescan.fingerprint(backward)
    assert rescan.fingerprint(forward) != rescan.fingerprint(fixed)
    assert rescan.fingerprint([]) == "none"


def test_a_scan_that_did_not_finish_is_an_error_not_an_empty_result() -> None:
    tools = _Tools(_report())
    tools.fail = "trivy image"

    with pytest.raises(RuntimeError, match="could not scan caddy:2.11.6-alpine"):
        rescan.scan_image(_IMAGE, tools)


# ------------------------------------------------------------------ one issue per set


def test_findings_without_an_open_issue_open_one(tmp_path: Path) -> None:
    tools = _Tools(_report(_vulnerability("CVE-2026-0001"), _vulnerability("CVE-2026-0002", None)))

    done = _record(tools, tmp_path)

    (create,) = tools.gh("issue", "create")
    assert create[create.index("--title") + 1] == _TITLE
    assert [create[i + 1] for i, part in enumerate(create) if part == "--label"] == [
        "epic:distribution",
        "security",
    ]
    (body,) = tools.bodies
    assert "1 with a fixed version published, 1 without one yet" in body
    assert "| `CVE-2026-0001` | HIGH | stdlib | 1.2.3 | 1.2.4 | usr/bin/caddy |" in body
    assert "| `CVE-2026-0002` | HIGH | stdlib | 1.2.3 | no fix published | usr/bin/caddy |" in body
    assert "- Reachable in this deployment: not assessed" in body
    assert "- Urgent action needed: not assessed" in body
    assert done == "caddy:2.11.6-alpine: opened https://github.com/o/r/issues/7"


def test_the_same_findings_are_not_filed_or_written_again(tmp_path: Path) -> None:
    tools = _Tools(_report(_vulnerability("CVE-2026-0001")))
    tools.issues = [{"number": 7, "title": _TITLE, "body": _filed_body(tools, tmp_path)}]

    done = _record(tools, tmp_path)

    assert [call[1:3] for call in tools.calls if call[0] == "gh"] == [["issue", "list"]]
    assert done == "caddy:2.11.6-alpine: #7 already lists these findings"


def test_component_key_is_stable_across_tags_digests_and_registry_ports() -> None:
    assert rescan.component_for("caddy:2.8-alpine") == "caddy"
    assert rescan.component_for("caddy:2.11.6-alpine") == "caddy"
    assert rescan.component_for("caddy@sha256:" + "a" * 64) == "caddy"
    assert rescan.component_for("ghcr.io/org/app:latest") == "ghcr.io/org/app"
    assert rescan.component_for("registry:5000/org/app:1") == "registry:5000/org/app"
    assert rescan.component_for("registry:5000/org/app") == "registry:5000/org/app"
    assert rescan.title_for("caddy:2.8-alpine") == _TITLE
    assert rescan.title_for("caddy:2.11.6-alpine") == _TITLE


def test_old_tag_issue_is_reused_and_retitled_without_losing_human_triage(
    tmp_path: Path,
) -> None:
    before = _Tools(_report(_vulnerability("CVE-2026-0001")))
    old = _filed_body(before, tmp_path).replace(_IMAGE, "caddy:2.8-alpine")
    old = old.replace(
        "- Reachable in this deployment: not assessed",
        "- Reachable in this deployment: no, reviewed by a maintainer",
    )
    tools = _Tools(_report(_vulnerability("CVE-2026-0001")))
    tools.issues = [
        {
            "number": 1314,
            "title": "fix(security): caddy:2.8-alpine has HIGH or CRITICAL vulnerabilities",
            "body": old,
        }
    ]

    assert _record(tools, tmp_path) == f"{_IMAGE}: updated #1314"
    assert tools.gh("issue", "create") == []
    (edit,) = tools.gh("issue", "edit")
    assert edit[3] == "1314"
    assert edit[edit.index("--title") + 1] == _TITLE
    new_body, comment = tools.bodies
    assert f"in `{_IMAGE}`" in new_body
    assert "no, reviewed by a maintainer" in new_body
    assert "finding set is unchanged" in comment


def test_same_findings_new_digest_refreshes_existing_issue(
    tmp_path: Path,
) -> None:
    tools = _Tools(_report(_vulnerability("CVE-2026-0001")))
    old = _filed_body(tools, tmp_path).replace(
        "caddy@sha256:" + "a" * 64,
        "caddy@sha256:" + "b" * 64,
    )
    tools.issues = [{"number": 17, "title": _TITLE, "body": old}]

    assert _record(tools, tmp_path) == f"{_IMAGE}: updated #17"
    assert tools.gh("issue", "create") == []
    (updated, comment) = tools.bodies
    assert "caddy@sha256:" + "a" * 64 in updated
    assert "finding set is unchanged" in comment


def test_a_different_image_does_not_take_over_caddy_incident(tmp_path: Path) -> None:
    tools = _Tools(_report(_vulnerability("CVE-2026-0001")))
    tools.issues = [
        {
            "number": 42,
            "title": "fix(security): caddy-proxy:2.8-alpine has HIGH or CRITICAL vulnerabilities",
            "body": "",
        }
    ]

    _record(tools, tmp_path)

    assert len(tools.gh("issue", "create")) == 1
    assert tools.gh("issue", "edit") == []


def test_a_changed_list_rewrites_the_issue_and_keeps_what_a_person_wrote(tmp_path: Path) -> None:
    before = _Tools(_report(_vulnerability("CVE-2026-0001"), _vulnerability("CVE-2026-0002")))
    triaged = _filed_body(before, tmp_path).replace(
        "- Reachable in this deployment: not assessed",
        "- Reachable in this deployment: no, the module is not loaded",
    )
    tools = _Tools(_report(_vulnerability("CVE-2026-0002"), _vulnerability("CVE-2026-0003")))
    tools.issues = [{"number": 7, "title": _TITLE, "body": triaged}]

    done = _record(tools, tmp_path)

    assert not tools.gh("issue", "create")
    (edit,) = tools.gh("issue", "edit")
    (comment,) = tools.gh("issue", "comment")
    assert edit[3] == comment[3] == "7"
    body, said = tools.bodies
    assert "`CVE-2026-0003`" in body and "`CVE-2026-0001`" not in body
    assert body.count("image-rescan:begin") == 1
    assert "- Reachable in this deployment: no, the module is not loaded" in body
    assert "- New: `CVE-2026-0003`" in said
    assert "- No longer reported: `CVE-2026-0001`" in said
    assert done == "caddy:2.11.6-alpine: updated #7"


def test_a_fix_that_appears_is_a_change(tmp_path: Path) -> None:
    before = _Tools(_report(_vulnerability("CVE-2026-0001", fixed=None)))
    tools = _Tools(_report(_vulnerability("CVE-2026-0001", fixed="1.2.4")))
    tools.issues = [{"number": 7, "title": _TITLE, "body": _filed_body(before, tmp_path)}]

    _record(tools, tmp_path)

    body, said = tools.bodies
    assert "| 1.2.4 |" in body and "no fix published" not in body
    assert "with a different installed or fixed version" in said


def test_an_issue_whose_findings_are_gone_is_told_so_once_and_left_open(tmp_path: Path) -> None:
    before = _Tools(_report(_vulnerability("CVE-2026-0001")))
    tools = _Tools(_report())
    tools.issues = [{"number": 7, "title": _TITLE, "body": _filed_body(before, tmp_path)}]

    _record(tools, tmp_path)

    body, said = tools.bodies
    assert "finds no HIGH or CRITICAL vulnerability" in body
    assert "No HIGH or CRITICAL vulnerability is reported now." in said
    assert not tools.gh("issue", "close")

    again = _Tools(_report())
    again.issues = [{"number": 7, "title": _TITLE, "body": body}]
    _record(again, tmp_path)

    assert [call[1:3] for call in again.calls if call[0] == "gh"] == [["issue", "list"]]


def test_no_finding_and_no_issue_writes_nothing(tmp_path: Path) -> None:
    tools = _Tools(_report())

    assert _record(tools, tmp_path) == "caddy:2.11.6-alpine: no finding and no open issue"
    assert [call[1:3] for call in tools.calls if call[0] == "gh"] == [["issue", "list"]]


def test_an_issue_with_a_longer_title_is_not_taken_for_this_one(tmp_path: Path) -> None:
    tools = _Tools(_report(_vulnerability("CVE-2026-0001")))
    tools.issues = [{"number": 3, "title": _TITLE + " (old)", "body": ""}]

    _record(tools, tmp_path)

    assert len(tools.gh("issue", "create")) == 1


def test_a_long_list_stays_under_githubs_body_limit() -> None:
    many = [_vulnerability(f"CVE-2026-{n:05d}", PkgName="p" * 60) for n in range(900)]
    scan = rescan.parse_report(_IMAGE, _report(*many))

    body = rescan.render_body(rescan.render_block(scan, today="2026-10-11", run_url=""))

    assert len(body) < 65536
    assert f"{900 - rescan.MAX_ROWS} more are in the run's log." in body


# ------------------------------------------------------------------ the command


def test_a_dry_run_scans_every_image_and_calls_gh_for_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tools = _Tools(_report(_vulnerability("CVE-2026-0001")))

    status = rescan.main(["--dry-run", "--image", "example/extra@sha256:" + "b" * 64], run=tools)

    scanned = [call[-1] for call in tools.calls if call[0] == "trivy"]
    assert scanned == [*rescan.deployed_images(), "example/extra@sha256:" + "b" * 64]
    assert [call for call in tools.calls if call[0] == "gh"] == []
    assert status == 0
    assert "CVE-2026-0001 HIGH stdlib 1.2.3 1.2.4 usr/bin/caddy" in capsys.readouterr().out


def test_a_failed_scan_fails_the_run_and_the_other_images_are_still_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = _Tools(_report(_vulnerability("CVE-2026-0001")))
    tools.fail = "caddy:2.11.6-alpine"

    status = rescan.main(["--work-dir", str(tmp_path)], run=tools)

    assert status == 1
    assert len(tools.gh("issue", "create")) == 1
    assert "::error::trivy could not scan caddy:2.11.6-alpine" in capsys.readouterr().out


def test_a_refused_issue_write_fails_the_run(tmp_path: Path) -> None:
    tools = _Tools(_report(_vulnerability("CVE-2026-0001")))
    tools.fail = "issue create"

    assert rescan.main(["--work-dir", str(tmp_path)], run=tools) == 1


# ------------------------------------------------------------------ the workflow


def _workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8")))


def test_the_workflow_runs_on_a_schedule_and_by_hand() -> None:
    triggers = _workflow()[True]  # PyYAML reads the key `on` as a boolean

    assert triggers["schedule"] == [{"cron": "41 4 * * *"}]
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"images", "dry_run"}
    assert triggers["pull_request"]["paths"] == [
        ".github/workflows/image-rescan.yml",
        "scripts/image_rescan.py",
    ]


def test_a_pull_request_run_writes_no_issue() -> None:
    job = _workflow()["jobs"]["rescan"]
    step = job["steps"][-1]

    assert step["env"]["DRY_RUN"] == "${{ github.event_name == 'pull_request' || inputs.dry_run }}"
    assert 'if [ "${DRY_RUN}" = "true" ]; then\n  args+=(--dry-run)' in step["run"]
    assert step["env"]["GH_REPO"] == "${{ github.repository }}"


def test_the_workflow_may_read_packages_and_write_issues_and_nothing_more() -> None:
    workflow = _workflow()

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["rescan"]["permissions"] == {
        "contents": "read",
        "packages": "read",
        "issues": "write",
    }


# ------------------------------------------------------------------ Dependabot


def _updates() -> list[dict[str, Any]]:
    loaded = yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8"))
    return cast(list[dict[str, Any]], loaded["updates"])


def _directories(ecosystem: str) -> list[str]:
    found: list[str] = []
    for update in _updates():
        if update["package-ecosystem"] == ecosystem:
            found += update.get("directories", [update.get("directory")])
    return found


def test_dependabot_follows_every_dockerfile_and_compose_file() -> None:
    def directories_of(*patterns: str) -> list[str]:
        listed = subprocess.run(
            ["git", "ls-files", *patterns], cwd=_ROOT, check=True, capture_output=True, text=True
        ).stdout.split()
        return sorted({"/" + str(Path(name).parent).strip(".") for name in listed})

    assert sorted(_directories("docker")) == directories_of("Dockerfile", "*/Dockerfile")
    assert sorted(_directories("docker-compose")) == directories_of(
        "*compose*.yml", "*compose*.yaml"
    )


def test_no_dependabot_update_is_merged_without_a_person() -> None:
    """Nothing in the repository's automation turns auto-merge on (#1107)."""
    listed = subprocess.run(
        ["git", "ls-files", ".github"], cwd=_ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    assert ".github/dependabot.yml" in listed

    offending = [
        name
        for name in listed
        if any(
            word in (_ROOT / name).read_text(encoding="utf-8")
            for word in ("gh pr merge", "--auto", "enablePullRequestAutoMerge", "automerge")
        )
    ]

    assert offending == []
