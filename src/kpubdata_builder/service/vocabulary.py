"""Builder-owned wire vocabulary, translated from kpubdata (#831, Independence Rule 7).

Every enum the service contract (``contract/builder-api.yaml``) declares belongs to
Builder, even where the values currently mirror kpubdata's. A kpubdata enum value
never goes over the wire as-is: it is looked up in an explicit table here, and a value
the table does not know becomes a declared Builder fallback instead of an off-contract
string. A new kpubdata value therefore cannot change the wire; mapping it is a
Builder decision, made here, with a contract version raise if the wire gains a value.

The tables are keyed by kpubdata's string values, not by its enum members, so this
module imports nothing from kpubdata.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

AccessStatus = Literal[
    "available",
    "auth_unknown",
    "application_required",
    "params_invalid",
    "rate_limited",
    "temporarily_unavailable",
    "network_error",
    "insufficient_metadata",
    "retired",
    "unknown",
]
"""Whether a user can reach a dataset's source (``DatasetStatusAxes.access``).

Builder owns this vocabulary. The values currently mirror kpubdata's probe
classification, plus ``unknown`` for "no probe result" and for any probe status
Builder has not mapped.
"""

ACCESS_STATUSES: tuple[AccessStatus, ...] = (
    "available",
    "auth_unknown",
    "application_required",
    "params_invalid",
    "rate_limited",
    "temporarily_unavailable",
    "network_error",
    "insufficient_metadata",
    "retired",
    "unknown",
)

_ACCESS_FROM_PROBE: dict[str, AccessStatus] = {
    "available": "available",
    "auth_unknown": "auth_unknown",
    "application_required": "application_required",
    "params_invalid": "params_invalid",
    "rate_limited": "rate_limited",
    "temporarily_unavailable": "temporarily_unavailable",
    "network_error": "network_error",
    "insufficient_metadata": "insufficient_metadata",
    "retired": "retired",
}

Representation = Literal["api_json", "api_xml", "file_csv", "file_excel", "sheet", "other"]
"""How a catalog dataset is served (``CatalogDataset.representation``)."""

REPRESENTATIONS: tuple[Representation, ...] = (
    "api_json",
    "api_xml",
    "file_csv",
    "file_excel",
    "sheet",
    "other",
)

_REPRESENTATION_FROM_KPUBDATA: dict[str, Representation] = {
    "api_json": "api_json",
    "api_xml": "api_xml",
    "file_csv": "file_csv",
    "file_excel": "file_excel",
    "sheet": "sheet",
    "other": "other",
}

Operation = Literal["list", "get", "schema", "raw", "download"]
"""What a catalog dataset supports (``CatalogDataset.operations`` items)."""

OPERATIONS: tuple[Operation, ...] = ("list", "get", "schema", "raw", "download")

_OPERATION_FROM_KPUBDATA: dict[str, Operation] = {
    "list": "list",
    "get": "get",
    "schema": "schema",
    "raw": "raw",
    "download": "download",
}

PaginationMode = Literal["offset", "index", "cursor", "none"]
"""How a catalog dataset pages (``CatalogQuerySupport.pagination``)."""

PAGINATION_MODES: tuple[PaginationMode, ...] = ("offset", "index", "cursor", "none")

_PAGINATION_FROM_KPUBDATA: dict[str, PaginationMode] = {
    "offset": "offset",
    "index": "index",
    "cursor": "cursor",
    "none": "none",
}


def _value(member: object) -> str | None:
    """A kpubdata enum member's string value, or the string itself."""
    value = getattr(member, "value", member)
    return value if isinstance(value, str) else None


def access_status(probe_status: object | None) -> AccessStatus:
    """Map a kpubdata probe status to Builder's; anything unmapped is ``unknown``."""
    mapped = _ACCESS_FROM_PROBE.get(_value(probe_status) or "")
    return mapped if mapped is not None else "unknown"


def representation(member: object) -> Representation:
    """Map kpubdata's ``Representation``; anything unmapped is ``other``."""
    mapped = _REPRESENTATION_FROM_KPUBDATA.get(_value(member) or "")
    return mapped if mapped is not None else "other"


def operations(members: Iterable[object]) -> list[Operation]:
    """Map kpubdata's ``Operation`` set, sorted. An unmapped operation is left out.

    A client cannot act on an operation it has no word for, so dropping it is the
    honest answer; the contract has no fallback value for this list.
    """
    found: set[Operation] = set()
    for member in members:
        operation = _OPERATION_FROM_KPUBDATA.get(_value(member) or "")
        if operation is not None:
            found.add(operation)
    return sorted(found)


def pagination_mode(member: object) -> PaginationMode | None:
    """Map kpubdata's ``PaginationMode``; ``None`` when unmapped.

    The caller reports the whole ``query_support`` as unknown (null) rather than
    naming a paging mode the dataset may not have.
    """
    return _PAGINATION_FROM_KPUBDATA.get(_value(member) or "")
