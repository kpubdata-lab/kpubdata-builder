"""The contract lists every PublishIssue code readiness and publish can report (#939).

Three lists must agree:

- ``PUBLISH_ISSUE_CODES`` in ``service/publish.py`` (the registry),
- ``PublishIssue.code`` in ``contract/builder-api.yaml`` (``x-codes`` and its description),
- the codes the source actually emits: string literals passed to ``PublishIssue`` or
  ``PublishTermsIssue``, plus the validator codes that exist only because a publish
  re-validates the spec with ``publish=True``.

A code added to the implementation but not the contract, or left in the contract after
the implementation drops it, fails here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, cast

import yaml

from kpubdata_builder.service.publish import PUBLISH_ISSUE_CODES

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = _ROOT / "contract" / "builder-api.yaml"
_SOURCE = _ROOT / "src" / "kpubdata_builder"
_VALIDATOR = _SOURCE / "spec" / "validator.py"

_ISSUE_CLASSES = frozenset({"PublishIssue", "PublishTermsIssue"})
# The license gate reports ``license_missing`` instead (publish.license_blocker).
_VALIDATOR_CODES_REPLACED = frozenset({"missing_license_for_publish"})
_CODE_IN_TEXT = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")


def _code_schema() -> dict[str, Any]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schemas = contract["components"]["schemas"]
    return cast(dict[str, Any], schemas["PublishIssue"]["properties"]["code"])


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _first_string_arg(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        if isinstance(value, str):
            return value
    return None


def emitted_issue_codes(source_root: Path = _SOURCE) -> set[str]:
    """String literals passed as the code of a ``PublishIssue``/``PublishTermsIssue``."""
    codes: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee_name(node) in _ISSUE_CLASSES:
                code = _first_string_arg(node)
                if code is not None:
                    codes.add(code)
    return codes


def _mentions_spec_publish(test: ast.expr) -> bool:
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == "publish"
        and isinstance(node.value, ast.Name)
        and node.value.id == "spec"
        for node in ast.walk(test)
    )


def publish_only_validator_codes(validator: Path = _VALIDATOR) -> set[str]:
    """Problem codes the validator reports only under ``if spec.publish ...``.

    ``effective_publish_policy_blockers`` re-validates the stored spec with
    ``publish=True``, so these reach a publish even though the spec validated when it
    was stored.
    """
    tree = ast.parse(validator.read_text(encoding="utf-8"), filename=str(validator))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _mentions_spec_publish(node.test):
            for inner in node.body:
                for call in ast.walk(inner):
                    if isinstance(call, ast.Call) and _callee_name(call) == "_p":
                        code = _first_string_arg(call)
                        if code is not None:
                            codes.add(code)
    return codes - _VALIDATOR_CODES_REPLACED


def implementation_codes() -> set[str]:
    return emitted_issue_codes() | publish_only_validator_codes()


class TestTheRegistryMatchesTheImplementation:
    def test_registry_has_no_duplicates(self) -> None:
        assert len(PUBLISH_ISSUE_CODES) == len(set(PUBLISH_ISSUE_CODES))

    def test_every_emitted_code_is_registered(self) -> None:
        unregistered = implementation_codes() - set(PUBLISH_ISSUE_CODES)

        assert not unregistered, f"add to PUBLISH_ISSUE_CODES and the contract: {unregistered}"

    def test_every_registered_code_is_emitted(self) -> None:
        stale = set(PUBLISH_ISSUE_CODES) - implementation_codes()

        assert not stale, f"no longer emitted, remove from the registry and contract: {stale}"

    def test_the_scan_finds_the_codes_the_issue_named(self) -> None:
        # Guards the scan itself: if it silently found nothing, the two tests above
        # would compare empty sets against a registry and still say something useful,
        # but these are the codes #939 was opened for.
        found = implementation_codes()

        assert {
            "credential_required",
            "pii_allow_with_publish",
            "redistribution_forbidden",
            "redistribution_unknown",
            "non_commercial_unconfirmed",
            "non_commercial_marker_missing",
            "destination_public",
            "destination_visibility_unknown",
        } <= found


class TestTheContractListsEveryCode:
    def test_x_codes_equal_the_registry(self) -> None:
        listed = set(_code_schema()["x-codes"])

        assert listed - set(PUBLISH_ISSUE_CODES) == set(), "contract lists a code never emitted"
        assert set(PUBLISH_ISSUE_CODES) - listed == set(), "contract misses an emitted code"

    def test_x_codes_follow_the_registry_order(self) -> None:
        assert tuple(_code_schema()["x-codes"]) == PUBLISH_ISSUE_CODES

    def test_every_code_has_a_condition(self) -> None:
        for code, condition in _code_schema()["x-codes"].items():
            assert isinstance(condition, str) and condition.strip(), code

    def test_the_description_names_every_code(self) -> None:
        description = _code_schema()["description"]
        named = set(_CODE_IN_TEXT.findall(description))

        assert set(PUBLISH_ISSUE_CODES) <= named


class TestADivergenceFails:
    """Negative: the checks above would catch a code dropped from either side."""

    def test_a_code_removed_from_the_contract_is_caught(self) -> None:
        listed = dict(_code_schema()["x-codes"])
        del listed["credential_required"]

        assert set(PUBLISH_ISSUE_CODES) - set(listed) == {"credential_required"}

    def test_a_new_literal_code_is_caught(self, tmp_path: Path) -> None:
        (tmp_path / "new_gate.py").write_text(
            "def gate():\n    return PublishIssue('brand_new_blocker', 'nope')\n",
            encoding="utf-8",
        )

        assert emitted_issue_codes(tmp_path) - set(PUBLISH_ISSUE_CODES) == {"brand_new_blocker"}

    def test_a_new_publish_only_validator_code_is_caught(self, tmp_path: Path) -> None:
        validator = tmp_path / "validator.py"
        validator.write_text(
            "def check(spec, problems):\n"
            "    if spec.publish and spec.secret:\n"
            "        problems.append(_p('secret_with_publish', 'secret', 'no'))\n"
            "    if spec.other:\n"
            "        problems.append(_p('not_publish_related', 'other', 'no'))\n",
            encoding="utf-8",
        )

        assert publish_only_validator_codes(validator) == {"secret_with_publish"}
