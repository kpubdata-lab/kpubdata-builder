"""release-please opens Builder's release pull request, and only that (kpubdata#895).

kpubdata moved to release-please in kpubdata#819; Builder follows the same shape with
its own rules kept. The version comes from the commit titles, so nobody picks
``bump``. The CHANGELOG stays hand-written, the lock file is written by kpubdata's
``set_version.py``, and the tag is made by the release job after the gates —
release-please does none of the three. It runs only inside the monthly release week
and only by hand, and a critical patch still goes through ``mode=prepare``.

Each of these is a line or two of configuration, and losing one is quiet: the release
would carry commit titles for notes, be tagged the moment it merged, or open outside
the freeze. So the lines are held here, and the shell the workflow runs is run here
too, so its refusals are seen to refuse.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "release-please-config.json"
_MANIFEST = _ROOT / ".release-please-manifest.json"
_RELEASE = _ROOT / ".github" / "workflows" / "release.yml"
_WORKFLOWS = _ROOT / ".github" / "workflows"
#: release-please names the branch ``<prefix>--components--<package>``; kpubdata's first
#: release-please pull request (kpubdata#830) had that suffix, which an exact match on
#: the bare prefix would have left with no release job.
_BRANCH = "release-please--branches--main"
_OBSERVED_BRANCH = "release-please--branches--main--components--kpubdata-builder"
_APP_TOKEN = "${{ steps.app.outputs.token || github.token }}"
_KPUBDATA_USES = re.compile(r"uses: kpubdata-lab/kpubdata/[^@\s]+@([0-9a-f]{40})")
_KPUBDATA_CHECKOUT = re.compile(
    r"repository: kpubdata-lab/kpubdata\n\s+ref: ([0-9a-f]{40})\n", re.MULTILINE
)


def _workflow() -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(_RELEASE.read_text(encoding="utf-8")))


def _job(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], _workflow()["jobs"][name])


def _steps(name: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _job(name)["steps"])


def _step(job: str, name: str) -> dict[str, Any]:
    (step,) = [s for s in _steps(job) if s.get("name") == name or s.get("id") == name]
    return step


def _declared_version() -> str:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match
    return match.group(1)


def _run(script: str, env: dict[str, str], tmp_path: Path) -> tuple[int, str, str]:
    """Run a step's shell the way a runner does (bash, -e), with its env."""
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), **env},
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout, output.read_text(encoding="utf-8")


# ------------------------------------------------------------------ configuration


def test_the_manifest_says_the_version_pyproject_declares() -> None:
    """release-please raises from the manifest. Behind ``pyproject.toml``, it would
    open a pull request for a version that is already out."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))

    assert manifest == {".": _declared_version()}


def test_the_changelog_stays_hand_written_and_the_bump_follows_the_0x_rule() -> None:
    config = json.loads(_CONFIG.read_text(encoding="utf-8"))

    assert config["skip-changelog"] is True
    # Breaking raises the minor in 0.x; `feat` already does. Patch-for-minor would
    # make a new feature a patch release, which is not the rule.
    assert config["bump-minor-pre-major"] is True
    assert "bump-patch-for-minor-pre-major" not in config
    assert config["packages"] == {
        ".": {"release-type": "python", "package-name": "kpubdata-builder"}
    }


def test_the_tag_and_title_are_the_ones_the_release_job_reads() -> None:
    config = json.loads(_CONFIG.read_text(encoding="utf-8"))
    release = _RELEASE.read_text(encoding="utf-8")

    assert config["include-v-in-tag"] is True
    assert config["include-component-in-tag"] is False
    # The release job compares the merged pull request's title with this, literally.
    assert config["pull-request-title-pattern"] == "chore: release v${version}"
    assert '"${TITLE}" != "chore: release ${TAG}"' in release


def test_release_please_writes_only_the_version_files() -> None:
    """The python release type also rewrites any tracked ``version.py`` and a
    ``__version__ = "X.Y.Z"`` literal in the package's ``__init__.py``. Builder has
    neither — its ``__version__`` comes from the package metadata — so the pull request
    changes ``pyproject.toml`` and the manifest only. One added later would be
    rewritten without anyone choosing it."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    init = (_ROOT / "src" / "kpubdata_builder" / "__init__.py").read_text(encoding="utf-8")

    assert [path for path in tracked if path.rsplit("/", 1)[-1] == "version.py"] == []
    # release-please's own pattern (updaters/python/python-file-with-version.ts).
    assert not re.search(r"(__version__ ?= ?[\"'])[0-9]+\.[0-9]+\.[0-9]+(?:-\w+)?([\"'])", init)


# ------------------------------------------------------------ when it may run


def test_it_runs_only_by_hand_and_only_from_main() -> None:
    """No schedule and no push: Builder releases once a month, and the week starts
    with a person. A dispatch from another branch would run that branch's job."""
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    condition = str(_job("release-please")["if"])

    assert set(triggers) == {"workflow_dispatch", "pull_request"}
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "inputs.mode == 'release-please'" in condition
    assert "github.ref == 'refs/heads/main'" in condition


def test_release_please_is_the_default_and_prepare_stays_for_a_critical_patch() -> None:
    inputs = _workflow().get("on", _workflow().get(True))["workflow_dispatch"]["inputs"]

    assert inputs["mode"]["options"] == ["release-please", "prepare", "release"]
    assert inputs["mode"]["default"] == "release-please"
    assert {"bump", "critical_patch", "critical_issue"} <= set(inputs)
    assert "critical_patch" in str(_step("prepare", "Release window")["with"])


def test_the_monthly_window_is_checked_first_and_cannot_be_waived() -> None:
    """Outside the release week the job refuses before it writes anything. It takes no
    critical patch and does not merely report: a release pull request opened outside
    the week could not be released and would break the freeze."""
    first = _steps("release-please")[0]

    assert first["name"] == "Release window"
    assert "/.github/actions/release-window@" in first["uses"]
    assert first["with"] == {"policy": "monthly"}


def test_release_please_neither_tags_nor_labels() -> None:
    please = _step("release-please", "please")
    shell = "\n".join(str(s.get("run", "")) for s in _steps("release-please"))

    assert please["with"]["skip-github-release"] is True
    assert please["with"]["skip-labeling"] is True
    assert please["with"]["config-file"] == _CONFIG.name
    assert please["with"]["manifest-file"] == _MANIFEST.name
    assert "gh release create" not in shell
    assert "git tag" not in shell
    assert "gh pr merge" not in shell


def test_the_release_branch_gets_the_lock_file_and_the_dated_changelog() -> None:
    write = _step("release-please", "Write the version into the lock file")
    promote = _step("release-please", "Date the CHANGELOG section")

    assert '--root "${GITHUB_WORKSPACE}"' in write["run"]
    assert "/.github/actions/release-notes@" in promote["uses"]
    assert promote["with"]["command"] == "promote"
    assert "check_version_consistency.py" in str(
        _step("release-please", "Check the version sources agree")["run"]
    )


# ------------------------------------------------------- the merge reaches release


def test_merging_the_release_please_branch_reaches_the_gated_release_job() -> None:
    release = _RELEASE.read_text(encoding="utf-8")

    assert f"startsWith(github.event.pull_request.head.ref, '{_BRANCH}')" in release
    assert f"{_BRANCH}*) from_release_please=true" in release
    # An exact comparison is what missed kpubdata's real branch name.
    assert f"head.ref == '{_BRANCH}'" not in release
    assert _OBSERVED_BRANCH.startswith(_BRANCH)
    # The hand-prepared path is still there for a critical patch.
    assert "startsWith(github.event.pull_request.head.ref, 'release/')" in release


@pytest.mark.parametrize(
    ("branch", "title", "tag", "passes"),
    [
        (_OBSERVED_BRANCH, "chore: release v0.5.0", "v0.5.0", True),
        (_BRANCH, "chore: release v0.5.0", "v0.5.0", True),
        # Someone edited the release pull request: title and file disagree.
        (_OBSERVED_BRANCH, "chore: release v0.5.0", "v0.6.0", False),
        (_OBSERVED_BRANCH, "chore: release 0.5.0", "v0.5.0", False),
        ("release/v0.5.0", "anything", "v0.5.0", True),
        ("release/v0.5.0", "chore: release v0.5.0", "v0.6.0", False),
        ("feature/x", "chore: release v0.5.0", "v0.5.0", False),
    ],
)
def test_the_branch_or_title_must_name_the_version_being_tagged(
    tmp_path: Path, branch: str, title: str, tag: str, passes: bool
) -> None:
    step = _step("release", "The branch name and the file must agree")

    code, out, _ = _run(step["run"], {"BRANCH": branch, "TITLE": title, "TAG": tag}, tmp_path)

    assert (code == 0) is passes, out


# ---------------------------------------------------------------- the App token


def test_the_release_app_token_is_pinned_and_holds_two_permissions() -> None:
    """The App token is the owner's choice (kpubdata#895, 2026-10-12): minted with
    contents and pull requests write and nothing else, from a pinned action."""
    mint = _step("release-please", "Mint the release App token")

    assert re.fullmatch(r"actions/create-github-app-token@[0-9a-f]{40}", mint["uses"])
    assert mint["if"] == "steps.app-secrets.outputs.use == 'true'"
    assert mint["with"] == {
        "client-id": "${{ secrets.RELEASE_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.RELEASE_APP_PRIVATE_KEY }}",
        "permission-contents": "write",
        "permission-pull-requests": "write",
    }


def test_the_app_token_reaches_only_release_please_and_the_branch_checkout() -> None:
    """Used anywhere else — a ``gh`` call, an approval — it would act with the App's
    identity. Two uses: release-please opens the pull request, checkout pushes to it."""
    users = [s for s in _steps("release-please") if "steps.app.outputs.token" in str(s)]
    text = _RELEASE.read_text(encoding="utf-8")

    assert [s["uses"].split("@")[0] for s in users] == [
        "googleapis/release-please-action",
        "actions/checkout",
    ]
    assert all(s["with"]["token"] == _APP_TOKEN for s in users)
    assert text.count("steps.app.outputs.token") == 2
    assert "GH_TOKEN" not in str(_job("release-please"))


@pytest.mark.parametrize(
    ("client_id", "private_key", "code", "use", "annotation"),
    [
        ("true", "true", 0, "use=true", ""),
        ("false", "false", 0, "use=false", "::warning title=No release App token::"),
        ("true", "false", 1, "", "::error title=Release App half configured::"),
        ("false", "true", 1, "", "::error title=Release App half configured::"),
    ],
)
def test_the_app_secrets_are_both_set_or_both_absent(
    tmp_path: Path, client_id: str, private_key: str, code: int, use: str, annotation: str
) -> None:
    """Releases must not stop on the day the App is not set up yet: no secrets is a
    warning and GITHUB_TOKEN, as before. One secret without the other is an error."""
    step = _step("release-please", "app-secrets")
    env = {"HAS_CLIENT_ID": client_id, "HAS_PRIVATE_KEY": private_key}

    returned, out, output = _run(step["run"], env, tmp_path)

    assert returned == code
    assert output.strip() == use
    assert annotation in out


# ------------------------------------------------------- one version script


def test_the_version_is_written_against_this_checkout() -> None:
    """kpubdata's ``set-version`` action runs its script without ``--root``, and the
    script's default root is the action's own download of kpubdata. From here it
    rewrote kpubdata's files and left Builder's alone. So no workflow uses it, and
    every call names this checkout."""
    calls = [
        str(step["run"])
        for job in ("prepare", "release-please")
        for step in _steps(job)
        if "set_version.py" in str(step.get("run", ""))
    ]

    assert len(calls) == 2
    assert all('--root "${GITHUB_WORKSPACE}"' in call for call in calls)
    assert all("rm -rf .kpubdata-release" in call for call in calls)
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        assert "/.github/actions/set-version@" not in path.read_text(encoding="utf-8"), path


def test_the_scripts_come_from_the_same_kpubdata_commit_as_its_actions() -> None:
    """``check_action_pins.py --bump-kpubdata`` moves ``uses:`` pins only. A checkout
    ``ref`` left behind would run one kpubdata commit's scripts beside another's
    actions."""
    text = _RELEASE.read_text(encoding="utf-8")
    action_pins = set(_KPUBDATA_USES.findall(text))
    checkout_pins = _KPUBDATA_CHECKOUT.findall(text)

    assert len(action_pins) == 1
    assert len(checkout_pins) == 2
    assert set(checkout_pins) == action_pins
