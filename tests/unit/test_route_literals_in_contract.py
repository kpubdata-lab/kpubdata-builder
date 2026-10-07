"""A route the contract does not declare cannot be added unnoticed (#994).

``test_service_contract.py`` compared the contract with a table of routes kept by hand in
the test (removed in #1109). A route added to the service and to neither the table nor
the contract passed every check: nothing looked at the route modules themselves.

The routes are matched by hand (``path == "/builds"``, ``path.startswith("/uploads/")``,
``rest.endswith("/publish/reconcile")``), so every route needs a path literal. This reads
those literals out of the route modules and requires each to be a piece of a path the
contract declares. It does not check methods: a new method on a declared path is caught
by ``test_dispatch_answers_only_declared_operations.py`` (#1054), which asks the service.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = _ROOT / "contract" / "builder-api.yaml"
_SERVICE = _ROOT / "src" / "kpubdata_builder" / "service"
#: Where requests are routed: the route adapters and the dispatcher itself.
_ROUTE_SOURCES = (*sorted((_SERVICE / "routes").glob("*.py")), _SERVICE / "app.py")

#: Answered outside the contract on purpose: the liveness probe carries no API version
#: and is not an operation a client is generated for (#372).
_OUTSIDE_CONTRACT = frozenset({"/healthz"})

_PARAMETER = re.compile(r"\{[^}]+\}")
_PATH_LITERAL = re.compile(r"^/[A-Za-z0-9_\-/]*$")


def _contract_paths() -> list[str]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return sorted(document["paths"])


def _pieces(path: str) -> set[str]:
    """Every literal a hand-written matcher could use for ``path``.

    The static runs between parameters, each also cut at every segment boundary and with
    or without the slash that leads into the next parameter: ``/builds/{run_id}/publish/
    receipt`` gives ``/builds``, ``/builds/``, ``/publish``, ``/publish/``,
    ``/publish/receipt``.
    """
    pieces: set[str] = set()
    for run in _PARAMETER.split(path):
        if not run or run == "/":
            continue
        segments = [segment for segment in run.split("/") if segment]
        for start in range(len(segments)):
            for end in range(start + 1, len(segments) + 1):
                piece = "/" + "/".join(segments[start:end])
                pieces.update({piece, piece + "/"})
    return pieces


def _declared_pieces(paths: list[str]) -> set[str]:
    return set().union(*(_pieces(path) for path in paths)) if paths else set()


def _path_literals(source: str) -> set[str]:
    """String constants in ``source`` that are shaped like a path or a piece of one."""
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != "/"
        and _PATH_LITERAL.match(node.value)
    }


def _undeclared(sources: dict[str, str], paths: list[str]) -> list[str]:
    declared = _declared_pieces(paths) | _OUTSIDE_CONTRACT
    return sorted(
        f"{name}: {literal}"
        for name, source in sources.items()
        for literal in _path_literals(source)
        if literal not in declared
    )


def _route_sources() -> dict[str, str]:
    return {
        str(path.relative_to(_ROOT)): path.read_text(encoding="utf-8") for path in _ROUTE_SOURCES
    }


def test_every_path_literal_in_the_route_modules_is_part_of_a_declared_path() -> None:
    assert _undeclared(_route_sources(), _contract_paths()) == []


def test_the_scan_reads_the_routes_it_is_meant_to_cover() -> None:
    """If a refactor moved the routes elsewhere, the check above would pass on nothing."""
    literals = set().union(*(_path_literals(source) for source in _route_sources().values()))

    assert len(literals) >= 40
    assert {"/builds", "/uploads/", "/publish/reconcile", "/warehouse/tables/"} <= literals


def test_a_route_the_contract_does_not_declare_is_reported() -> None:
    """The failure the issue asks to see: a new route with no declaration."""
    paths = _contract_paths()
    added = {
        "service/routes/builds.py": (
            "def handle(method, path):\n"
            "    if method == 'POST' and path == '/builds':\n"
            "        return 1\n"
            "    if path.startswith('/builds/') and path.endswith('/secrets'):\n"
            "        return 2\n"
            "    if method == 'GET' and path == '/internal/dump':\n"
            "        return 3\n"
        )
    }

    assert _undeclared(added, paths) == [
        "service/routes/builds.py: /internal/dump",
        "service/routes/builds.py: /secrets",
    ]


def test_a_declared_route_is_not_reported_however_it_is_matched() -> None:
    paths = ["/builds", "/builds/{run_id}/publish/receipt", "/uploads/{upload_id}"]
    matched = {
        "routes.py": (
            "A = '/builds'\nB = '/builds/'\nC = '/publish/receipt'\nD = '/publish'\n"
            "E = '/uploads/'\nF = '/receipt'\n"
        )
    }

    assert _undeclared(matched, paths) == []


def test_a_path_removed_from_the_contract_turns_its_route_into_a_finding() -> None:
    """The other direction of drift: the declaration goes, the route stays."""
    paths = [path for path in _contract_paths() if not path.startswith("/uploads")]

    findings = _undeclared(_route_sources(), paths)

    assert any(finding.endswith(": /uploads") for finding in findings)
    assert any(finding.endswith(": /uploads/") for finding in findings)
