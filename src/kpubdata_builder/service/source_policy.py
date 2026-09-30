"""Which sources a deployment lets a spec use (#685)."""

from __future__ import annotations

from typing import cast

from ..spec import BuildSpec, JsonValue
from . import ownership
from .responses import ServiceResponse


def url_source_refusal(spec: BuildSpec) -> ServiceResponse | None:
    """Refuse a spec with a ``url`` source in a multi-user deployment (#685).

    ADR 0012's 2026-09-30 amendment (D5). A bare ``url`` source carries no credential,
    so it cannot leak a key, but it lets any user make the server fetch any public host
    and keep what came back. A single-user deployment keeps allowing it, as before.
    The answer comes before any client or request exists, and names each offending
    source so a client can point at it.
    """
    if not ownership.multi_user_mode():
        return None
    offending = [
        {"index": index, "alias": source.alias or None, "path": f"sources[{index}].kind"}
        for index, source in enumerate(spec.sources)
        if source.kind == "url"
    ]
    if not offending:
        return None
    return ServiceResponse(
        403,
        {
            "error": "url sources are not allowed in a multi-user deployment",
            "code": "url_source_forbidden",
            "sources": cast(JsonValue, offending),
        },
    )


__all__ = ["url_source_refusal"]
