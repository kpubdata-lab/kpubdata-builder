"""Which contract operation a request is (#1109).

The contract (`contract/builder-api.yaml`) is where a method and a path become an
operation, and where an operation says which credential headers it reads. The service
used to spell that out again wherever it needed it. It is now read from one table,
`_contract_operations`, generated from the contract and held to it by the unit tests.

This module only answers questions about a request. Routing itself is still done by
the route adapters; `tests/unit/test_dispatch_answers_only_declared_operations.py` asks
the service with every method of every declared path, so the adapters and the contract
cannot disagree unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from ._contract_operations import OPERATIONS

#: Path parameters that take the rest of the path, slashes included. The contract has no
#: way to say so; `GET /artifacts/{run_id}/{file_path}` is the one route that reads a
#: run-relative file path. A test fails if another such parameter appears.
REST_OF_PATH_PARAMETERS = frozenset({"file_path"})


@dataclass(frozen=True)
class Operation:
    """One operation of the contract."""

    method: str
    path: str
    operation_id: str
    #: The operation declares the `X-Provider-Key` header.
    provider_key: bool
    #: The operation declares the `X-Publish-Credential` header.
    publish_credential: bool
    #: False where the contract says `security: []`.
    authenticated: bool

    @property
    def segments(self) -> tuple[str, ...]:
        return _segments(self.path)


@lru_cache(maxsize=1)
def all_operations() -> tuple[Operation, ...]:
    """Every operation the contract declares, in the contract's order."""
    return tuple(Operation(*row) for row in OPERATIONS)


def _segments(path: str) -> tuple[str, ...]:
    """A path's segments, or `("",)` for one that is not `/a/b` — no leading slash, a
    trailing slash, an empty segment. Such a path fits no template: the route adapters
    compare paths exactly, and this must not call something an operation that they
    would answer 404."""
    if not path.startswith("/") or path.endswith("/") or "//" in path:
        return ("",)
    return tuple(path[1:].split("/"))


def _is_parameter(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _matches(template: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    """Whether a request's path segments fit an operation's path template.

    A `{parameter}` takes exactly one non-empty segment; one named in
    `REST_OF_PATH_PARAMETERS` takes one or more, and only as the template's last.
    """
    takes_rest = bool(template) and template[-1][1:-1] in REST_OF_PATH_PARAMETERS
    if takes_rest:
        if len(actual) < len(template):
            return False
    elif len(actual) != len(template):
        return False
    for expected, given in zip(template, actual, strict=False):
        if not given:
            return False
        if not _is_parameter(expected) and expected != given:
            return False
    return all(actual[len(template) - 1 :]) if takes_rest else True


def find_operation(method: str, path: str) -> Operation | None:
    """The contract operation a request for `method` `path` is, or None.

    Where two templates fit — a literal segment and a parameter in the same place — the
    one with more literal segments is the operation, as the route adapters decide it.
    `path` is the request's path without its query string.
    """
    actual = _segments(path)
    best: Operation | None = None
    best_literals = -1
    for operation in all_operations():
        if operation.method != method:
            continue
        template = operation.segments
        if not _matches(template, actual):
            continue
        literals = sum(1 for segment in template if not _is_parameter(segment))
        if literals > best_literals:
            best, best_literals = operation, literals
    return best


__all__ = ["REST_OF_PATH_PARAMETERS", "Operation", "all_operations", "find_operation"]
