"""A merged release pull request publishes its image (#1142).

release.yml calls docker.yml with the release tag. In a called workflow the ``github``
context is the caller's, so a release that a merged pull request started reached
docker.yml as a ``pull_request`` event, and ``publish`` — guarded by
``github.event_name != 'pull_request'`` — was skipped. The tag and the GitHub Release
existed; the image did not, and nothing failed.

The workflow expressions are evaluated here for each way in, with a small evaluator of
the expression syntax they use, rather than read as strings: what matters is what they
decide.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"


def _load(name: str) -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8")))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads a bare `on:` key as True.
    return cast(dict[str, Any], workflow.get("on", workflow.get(True)))


# ------------------------------------------------------------------ the evaluator

_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||!|\(|\)|,)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.-]*))"
)


def _tokens(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    position = 0
    text = text.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        assert match is not None, f"cannot read {text[position:]!r}"
        kind = cast(str, match.lastgroup)
        found.append((kind, match.group(kind)))
        position = match.end()
    return found


def evaluate(expression: str, context: dict[str, object]) -> object:
    """``${{ … }}`` with the operators these workflows use: ``== != && || !``, string
    literals, ``true``/``false``/``null``, ``format(…)`` and context paths."""
    expression = expression.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2]
    tokens = _tokens(expression)
    position = 0

    def peek() -> str | None:
        return tokens[position][1] if position < len(tokens) else None

    def take() -> tuple[str, str]:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def primary() -> object:
        kind, text = take()
        if text == "(":
            value = either()
            assert take()[1] == ")"
            return value
        if text == "!":
            return not truthy(primary())
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if text in ("true", "false"):
            return text == "true"
        if text == "null":
            return None
        if text == "format" and peek() == "(":
            take()
            arguments = [either()]
            while peek() == ",":
                take()
                arguments.append(either())
            assert take()[1] == ")"
            template = str(arguments[0])
            return re.sub(r"\{(\d+)\}", lambda m: str(arguments[int(m.group(1)) + 1]), template)
        found: object = context
        for part in text.split("."):
            found = found.get(part) if isinstance(found, dict) else None
        return found

    def comparison() -> object:
        left = primary()
        while peek() in ("==", "!="):
            op = take()[1]
            right = primary()
            left = (left == right) if op == "==" else (left != right)
        return left

    def both() -> object:
        left = comparison()
        while peek() == "&&":
            take()
            right = comparison()
            left = right if truthy(left) else left
        return left

    def either() -> object:
        left = both()
        while peek() == "||":
            take()
            right = both()
            left = left if truthy(left) else right
        return left

    value = either()
    assert position == len(tokens), f"unread: {tokens[position:]}"
    return value


def render(template: str, context: dict[str, object]) -> str:
    """A string with ``${{ … }}`` parts, each replaced by its value (``docker-${{ … }}``)."""
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(evaluate(m.group(1), context)), template)


def truthy(value: object) -> bool:
    return value not in (None, False, 0, "")


def _context(
    event: str, github_ref: str, inputs: dict[str, object] | None = None
) -> dict[str, object]:
    return {"github": {"event_name": event, "ref": github_ref}, "inputs": inputs or {}}


def test_the_evaluator_reads_these_expressions() -> None:
    context = _context("push", "refs/heads/main", {"ref": "v1.2.3"})
    assert evaluate("${{ github.event_name != 'pull_request' }}", context) is True
    assert evaluate("inputs.ref && format('refs/tags/{0}', inputs.ref) || github.ref", context) == (
        "refs/tags/v1.2.3"
    )
    assert evaluate("inputs.ref && 'x' || github.ref", _context("push", "refs/heads/main")) == (
        "refs/heads/main"
    )
    assert evaluate("!inputs.publish", _context("pull_request", "x")) is True


# -------------------------------------------------------------------- docker.yml

#: Each way docker.yml runs: (event docker.yml sees, github.ref, inputs, publishes?).
WAYS_IN = {
    "an ordinary pull request": ("pull_request", "refs/pull/7/merge", {}, False),
    "a push to main": ("push", "refs/heads/main", {}, True),
    "a pushed tag": ("push", "refs/tags/v1.2.3", {}, True),
    "a manual run": ("workflow_dispatch", "refs/heads/main", {}, True),
    # The caller's event: release.yml started by a merged release pull request.
    "release.yml, from a merged release pull request": (
        "pull_request",
        "refs/pull/9/merge",
        {"ref": "v1.2.3", "publish": True},
        True,
    ),
    "release.yml, from a manual release": (
        "workflow_dispatch",
        "refs/heads/main",
        {"ref": "v1.2.3", "publish": True},
        True,
    ),
}


@pytest.mark.parametrize("way", sorted(WAYS_IN))
def test_each_way_in_publishes_or_not(way: str) -> None:
    event, ref, inputs, publishes = WAYS_IN[way]
    publish_job = _load("docker.yml")["jobs"]["publish"]

    assert truthy(evaluate(publish_job["if"], _context(event, ref, inputs))) is publishes


def test_the_release_path_was_skipped_by_the_old_condition() -> None:
    """The defect, kept as a fact: the old guard skips a merged release pull request."""
    event, ref, inputs, _ = WAYS_IN["release.yml, from a merged release pull request"]

    assert (
        evaluate("${{ github.event_name != 'pull_request' }}", _context(event, ref, inputs))
        is False
    )


def test_publish_is_its_own_input_and_defaults_off() -> None:
    call = _triggers(_load("docker.yml"))["workflow_call"]

    assert call["inputs"]["publish"] == {
        "description": call["inputs"]["publish"]["description"],
        "required": False,
        "type": "boolean",
        "default": False,
    }
    assert call["outputs"]["digest"]["value"] == "${{ jobs.publish.outputs.digest }}"
    publish_job = _load("docker.yml")["jobs"]["publish"]
    assert publish_job["outputs"]["digest"] == "${{ steps.build-publish.outputs.digest }}"


def test_a_tag_push_and_the_release_call_share_one_concurrency_group() -> None:
    concurrency = _load("docker.yml")["concurrency"]
    pushed = _context("push", "refs/tags/v1.2.3")
    called = _context("pull_request", "refs/pull/9/merge", {"ref": "v1.2.3", "publish": True})

    assert render(concurrency["group"], pushed) == "docker-refs/tags/v1.2.3"
    assert render(concurrency["group"], called) == "docker-refs/tags/v1.2.3"
    assert render(concurrency["group"], _context("push", "refs/heads/main")) == (
        "docker-refs/heads/main"
    )


@pytest.mark.parametrize("way", sorted(WAYS_IN))
def test_only_an_ordinary_pull_request_is_cancelled_by_a_newer_run(way: str) -> None:
    event, ref, inputs, publishes = WAYS_IN[way]
    cancel = _load("docker.yml")["concurrency"]["cancel-in-progress"]

    assert truthy(evaluate(cancel, _context(event, ref, inputs))) is (not publishes)


# ------------------------------------------------------------------- release.yml


def test_release_yml_asks_for_the_image_to_be_published() -> None:
    image = _load("release.yml")["jobs"]["image"]

    assert image["uses"] == "./.github/workflows/docker.yml"
    assert image["with"]["publish"] is True
    assert image["with"]["ref"] == "${{ needs.release.outputs.tag }}"


def test_a_release_without_a_published_image_fails() -> None:
    job = _load("release.yml")["jobs"]["image-published"]

    assert job["needs"] == ["release", "image"]
    (step,) = job["steps"]
    assert step["env"]["DIGEST"] == "${{ needs.image.outputs.digest }}"
    assert 'if [ -z "${DIGEST}" ]' in step["run"] and "exit 1" in step["run"]
