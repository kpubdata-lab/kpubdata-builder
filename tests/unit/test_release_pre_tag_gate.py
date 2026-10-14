"""Nothing is tagged until the release image has passed (#1324).

``release.yml`` used to push the git tag and create the GitHub Release in its gate job,
and call ``docker.yml`` afterwards (``needs: [release]``). A Docker build, runtime smoke
or Trivy failure then left a tag and a Release whose declared image did not exist.

The order is read from the workflow files: which job tags, what it needs, what the
image build runs before it publishes. ``ordering_problems`` is checked against the
files as they are and against the layout this issue replaced, which it must refuse.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import yaml

_WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"
_DOCKER = "./.github/workflows/docker.yml"
_TAGGING = ("git tag ", "gh release create")
_ALWAYS = ("always()", "failure()", "cancelled()")


def _load(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8")))


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _upstream(jobs: dict[str, Any], name: str) -> set[str]:
    found: set[str] = set()
    pending = _needs(jobs[name])
    while pending:
        job = pending.pop()
        if job not in found:
            found.add(job)
            pending.extend(_needs(jobs[job]))
    return found


def _step(job: dict[str, Any], name: str) -> dict[str, Any] | None:
    for step in job.get("steps", []):
        if step.get("name") == name:
            return cast(dict[str, Any], step)
    return None


def _index(job: dict[str, Any], name: str) -> int:
    names = [step.get("name") for step in job.get("steps", [])]
    return names.index(name) if name in names else -1


def ordering_problems(release: dict[str, Any], docker: dict[str, Any]) -> list[str]:
    """What lets a tag or a GitHub Release exist before the image passed; empty if none."""
    problems: list[str] = []
    jobs = cast(dict[str, Any], release["jobs"])

    tagging = sorted(
        name
        for name, job in jobs.items()
        if any(
            word in str(step.get("run", "")) for step in job.get("steps", []) for word in _TAGGING
        )
    )
    image = sorted(
        name
        for name, job in jobs.items()
        if job.get("uses") == _DOCKER and job.get("with", {}).get("publish") is True
    )
    if len(tagging) != 1 or len(image) != 1:
        return [f"expected one tagging job and one image job, found {tagging} and {image}"]
    (tag_job_name,) = tagging
    (image_job_name,) = image
    tag_job = jobs[tag_job_name]

    if image_job_name not in _upstream(jobs, tag_job_name):
        problems.append(f"{tag_job_name} tags without waiting for {image_job_name}")
    if any(word in str(tag_job.get("if", "")) for word in _ALWAYS):
        problems.append(f"{tag_job_name} runs even when the image failed")

    # The image is built from the commit that is tagged, under the tag's name.
    gate = _needs(jobs[image_job_name])
    with_ = jobs[image_job_name]["with"]
    if not any(with_.get("ref") == f"${{{{ needs.{g}.outputs.sha }}}}" for g in gate):
        problems.append(f"{image_job_name} does not build the gated commit")
    tag_run = str((_step(tag_job, "Tag and release") or {}).get("run", ""))
    if 'git tag "${TAG}" "${SHA}"' not in tag_run:
        problems.append(f"{tag_job_name} does not tag the gated commit")
    if tag_job.get("env", {}).get("SHA") != with_.get("ref"):
        problems.append(f"{tag_job_name} tags a commit other than the one built")

    # The published digest is read back and scanned before the tag.
    tag_at = _index(tag_job, "Tag and release")
    for name in ("The registry serves the published digest", "Trivy scan of the published digest"):
        if not 0 <= _index(tag_job, name) < tag_at:
            problems.append(f"{tag_job_name}: '{name}' does not come before the tag")

    # docker.yml publishes only after its build job's smokes and scan.
    build = docker["jobs"]["build"]
    publish = docker["jobs"]["publish"]
    if "build" not in _needs(publish) or any(w in str(publish.get("if", "")) for w in _ALWAYS):
        problems.append("docker.yml publishes without waiting for the build job")
    for step in build["steps"]:
        if step.get("continue-on-error"):
            problems.append(f"docker.yml build: '{step.get('name')}' cannot fail the build")
    smokes = [s for s in build["steps"] if "smoke" in str(s.get("name", "")).lower()]
    if not smokes:
        problems.append("docker.yml build runs no runtime smoke")
    for job_name, job in (("docker.yml build", build), (tag_job_name, tag_job)):
        scans = [s for s in job["steps"] if "trivy-action" in str(s.get("uses", ""))]
        policy = [s.get("with", {}) for s in scans]
        if not policy or any(
            (p.get("severity"), p.get("exit-code"), p.get("ignore-unfixed"))
            != ("CRITICAL,HIGH", "1", True)
            for p in policy
        ):
            problems.append(f"{job_name}: no Trivy scan that fails on fixable HIGH/CRITICAL")
    return problems


def test_the_release_tags_only_after_the_image_passed() -> None:
    assert ordering_problems(_load("release.yml"), _load("docker.yml")) == []


def test_the_layout_before_1324_is_refused() -> None:
    """The defect, kept as a failing case: the gate job tagged and the image came after."""
    release = _load("release.yml")
    old = copy.deepcopy(release)
    jobs = old["jobs"]
    tag_steps = jobs.pop("tag")["steps"]
    jobs["release"]["steps"].append(_step({"steps": tag_steps}, "Tag and release"))
    jobs["image"]["with"] = {"ref": "${{ needs.release.outputs.tag }}", "publish": True}

    problems = ordering_problems(old, _load("docker.yml"))

    assert "release tags without waiting for image" in problems
    assert "image does not build the gated commit" in problems


def test_a_tag_job_that_runs_after_a_failed_image_is_refused() -> None:
    release = _load("release.yml")
    release["jobs"]["tag"]["if"] = "${{ always() }}"

    assert ordering_problems(release, _load("docker.yml")) == [
        "tag runs even when the image failed"
    ]


def test_a_scan_that_does_not_fail_is_refused() -> None:
    docker = _load("docker.yml")
    (scan,) = [s for s in docker["jobs"]["build"]["steps"] if "trivy" in str(s.get("uses"))]
    scan["with"]["exit-code"] = "0"

    assert ordering_problems(_load("release.yml"), docker) == [
        "docker.yml build: no Trivy scan that fails on fixable HIGH/CRITICAL"
    ]


def test_a_smoke_allowed_to_fail_is_refused() -> None:
    docker = _load("docker.yml")
    smoke = cast(dict[str, Any], _step(docker["jobs"]["build"], "Compose profiles smoke"))
    smoke["continue-on-error"] = True

    assert len(ordering_problems(_load("release.yml"), docker)) == 1


def test_the_gate_job_writes_nothing() -> None:
    gate = _load("release.yml")["jobs"]["release"]

    assert gate["permissions"] == {"contents": "read"}
    assert gate["outputs"]["sha"] == "${{ steps.commit.outputs.sha }}"


def test_the_gates_install_the_lock_as_it_stands() -> None:
    gate = _load("release.yml")["jobs"]["release"]

    lock = cast(dict[str, Any], _step(gate, "The lock file is current"))
    install = cast(dict[str, Any], _step(gate, "Install dependencies"))
    assert lock["run"] == "uv lock --check --no-sources"
    assert "--locked" in install["run"].split() and "--no-sources" in install["run"].split()
    assert _index(gate, "The lock file is current") < _index(gate, "Install dependencies")
    assert _index(gate, "Install dependencies") < _index(gate, "Run quality gates")


def test_the_tag_is_checked_again_and_never_moved() -> None:
    tag_job = _load("release.yml")["jobs"]["tag"]
    run = "\n".join(str(step.get("run", "")) for step in tag_job["steps"])

    assert _index(tag_job, "The tag must not exist yet") < _index(tag_job, "Tag and release")
    assert "--force" not in run and "-f " not in run and ":refs/tags" not in run
    assert "--verify-tag" in run


def test_the_release_notes_name_the_digest() -> None:
    tag_job = _load("release.yml")["jobs"]["tag"]
    record = cast(dict[str, Any], _step(tag_job, "Record what the release was tested with"))

    assert '"${IMAGE}" "${DIGEST}"' in record["run"]
    assert _index(tag_job, "Record what the release was tested with") < _index(
        tag_job, "Tag and release"
    )
