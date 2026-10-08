"""The service answers only the (method, path) pairs the contract declares (#1054).

``test_route_literals_in_contract.py`` (#994) checks the path literals the route modules
match on. It does not see a new method on a declared path, or a new path put together
from pieces that are already declared. (``test_service_contract.py`` used to compare the
contract with a table of routes kept by hand; that table is gone, #1109.)

This asks the service itself. The dispatcher has one answer for a request no route took —
404 ``not found: <METHOD> <path>`` — so anything else means a route answered. Every method
is sent to every declared path, and to every path that can be assembled from the declared
pieces; what answers must be a declared operation.

The routes match loosely (``path.startswith("/builds/") and path.endswith("/cancel")``),
so an assembled path is often taken by a declared route that reads the extra segments as
part of an id and refuses it. That is told apart from a route of its own by a control:
the same declared route asked with another id of as many segments. A loose match gives
the control the same answer; a route of its own does not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest
import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.stages.bronze.build import SourceDataset

_CONTRACT = Path(__file__).resolve().parents[2] / "contract" / "builder-api.yaml"
_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
_PARAMETER = re.compile(r"\{[^}]+\}")
#: Stands in for every path parameter.
_ID = "id0"
#: A segment of a control id; no route matches on it.
_OTHER = "zz9"

#: (method, path) -> an answer, or None when no route took the request.
Answer = Callable[[str, str], object | None]


def _declared() -> dict[str, set[str]]:
    """Each declared path template and the methods the contract gives it."""
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return {
        path: {method.upper() for method in operations if method.upper() in _METHODS}
        for path, operations in document["paths"].items()
    }


def _matcher(template: str) -> re.Pattern[str]:
    escaped = re.escape(template).replace(r"\{", "{").replace(r"\}", "}")
    return re.compile("^" + _PARAMETER.sub("[^/]+", escaped) + "$")


def _is_declared(method: str, path: str, declared: Mapping[str, set[str]]) -> bool:
    return any(
        method in methods and _matcher(template).match(path)
        for template, methods in declared.items()
    )


def _pieces(template: str) -> set[tuple[str, ...]]:
    """The runs of static segments in a template, cut at every segment boundary."""
    found: set[tuple[str, ...]] = set()
    for run in _PARAMETER.split(template):
        segments = [segment for segment in run.split("/") if segment]
        for start in range(len(segments)):
            for end in range(start + 1, len(segments) + 1):
                found.add(tuple(segments[start:end]))
    return found


def _assembled(declared: Mapping[str, set[str]]) -> list[str]:
    """Every path made of a declared prefix up to a parameter and a declared piece."""
    prefixes: set[str] = set()
    for template in declared:
        built = ""
        for run in _PARAMETER.split(template)[:-1]:
            built += run + _ID
            prefixes.add(built)
    pieces = set().union(*(_pieces(template) for template in declared))
    return sorted(f"{prefix}/{'/'.join(piece)}" for prefix in prefixes for piece in pieces)


def _loose_matches(
    method: str, path: str, declared: Mapping[str, set[str]]
) -> Iterator[tuple[str, frozenset[str]]]:
    """Declared routes of this method that take ``path`` when an id may hold slashes: the
    control path, and the segments of the ids read from ``path``."""
    for template, methods in declared.items():
        if method not in methods:
            continue
        escaped = re.escape(template).replace(r"\{", "{").replace(r"\}", "}")
        match = re.fullmatch(_PARAMETER.sub("(.+)", escaped), path)
        if match is None:
            continue
        ids = match.groups()
        runs = _PARAMETER.split(template)
        control = runs[0] + "".join(
            "/".join(_OTHER for _ in value.split("/")) + run
            for value, run in zip(ids, runs[1:], strict=True)
        )
        yield control, frozenset(segment for value in ids for segment in value.split("/"))


def _shape(answered: object | None, segments: frozenset[str]) -> str:
    """An answer with the id segments it may quote taken out. Both answers of a pair go
    through the same replacement, so a word that is also a segment changes in both."""
    text = json.dumps(answered, sort_keys=True, default=str)
    for segment in sorted(segments | {_OTHER}, key=len, reverse=True):
        text = text.replace(segment, "<id>")
    return text


def _undeclared(answer: Answer, declared: Mapping[str, set[str]]) -> list[str]:
    """The (method, path) pairs that answer and are not declared operations."""
    findings: list[str] = []
    concrete = {_PARAMETER.sub(_ID, template): template for template in declared}
    for path, template in concrete.items():
        for method in _METHODS:
            if method not in declared[template] and answer(method, path) is not None:
                findings.append(f"{method} {template}")
    for path in _assembled(declared):
        if path in concrete:
            continue
        for method in _METHODS:
            answered = answer(method, path)
            if answered is None:
                continue
            if not any(
                _shape(answered, segments) == _shape(answer(method, control), segments)
                for control, segments in _loose_matches(method, path, declared)
            ):
                findings.append(f"{method} {path}")
    return sorted(findings)


def _unanswered(answer: Answer, declared: Mapping[str, set[str]]) -> list[str]:
    """Declared operations no route takes."""
    return sorted(
        f"{method} {template}"
        for template, methods in declared.items()
        for method in methods
        if answer(method, _PARAMETER.sub(_ID, template)) is None
    )


@pytest.fixture()
def answer(tmp_path: Path) -> Answer:
    class _NoProvider:
        def dataset(self, source_key: str) -> SourceDataset:
            raise RuntimeError("the probe reaches no provider")

    service = BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: _NoProvider())

    def ask(method: str, path: str) -> object | None:
        response = dispatch(service, method, path, None)
        if not isinstance(response, ServiceResponse):
            return ("file", type(response).__name__)
        if response.status_code == 405:
            return None
        if (
            response.status_code == 404
            and response.body.get("error") == f"not found: {method} {path}"
        ):
            return None
        return (response.status_code, response.body)

    return ask


def test_only_declared_operations_answer(answer: Answer) -> None:
    assert _undeclared(answer, _declared()) == []


def test_every_declared_operation_is_taken_by_a_route(answer: Answer) -> None:
    assert _unanswered(answer, _declared()) == []


def test_the_probe_covers_the_contract_and_its_assembled_paths() -> None:
    """If the contract stopped parsing, the checks above would pass on nothing."""
    declared = _declared()

    assert len(declared) >= 50
    assert sum(len(methods) for methods in declared.values()) >= 60
    assert len(_assembled(declared)) >= 500


def _with_routes(base: Answer, extra: Mapping[tuple[str, str], object]) -> Answer:
    """``base`` plus routes of its own for the given (method, path-pattern) pairs."""
    patterns = [(method, re.compile(pattern), body) for (method, pattern), body in extra.items()]

    def ask(method: str, path: str) -> object | None:
        for route_method, pattern, body in patterns:
            if method == route_method and pattern.fullmatch(path):
                return body
        return base(method, path)

    return ask


def test_a_new_method_on_a_declared_path_is_reported(answer: Answer) -> None:
    """The failure the issue asks to see: ``DELETE /builds/{run_id}/manifest``."""
    added = _with_routes(answer, {("DELETE", r"/builds/[^/]+/manifest"): (200, {"deleted": True})})

    assert _undeclared(added, _declared()) == ["DELETE /builds/{run_id}/manifest"]


def test_a_path_assembled_from_declared_pieces_is_reported(answer: Answer) -> None:
    """``/builds/`` and ``/history`` are both declared pieces; the path is not."""
    added = _with_routes(answer, {("GET", r"/builds/[^/]+/history"): (200, {"history": []})})

    assert _undeclared(added, _declared()) == [f"GET /builds/{_ID}/history"]


def test_a_declared_route_refusing_a_many_segment_id_is_not_a_finding(answer: Answer) -> None:
    """``/datasets/{dataset_id}/runs`` takes ``/datasets/id0/admin/runs`` and looks for a
    dataset called ``id0/admin``. That is the declared route, not a new one."""
    path = f"/datasets/{_ID}/admin/runs"

    assert answer("GET", path) is not None
    assert path in _assembled(_declared())
    assert [found for found in _undeclared(answer, _declared()) if "admin/runs" in found] == []
