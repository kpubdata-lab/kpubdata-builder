"""How often one user may run queries in a multi-user deployment (#1245).

``KPUBDATA_QUERY_MAX_CONCURRENCY`` and the memory budget bound how many queries run at
once, for the whole service. One user sending queries back to back could take every
slot as it freed, and every other user's query was refused with ``query_busy``. Each
user may now start ``KPUBDATA_BUILDER_QUERY_RATE_PER_MINUTE`` queries in any 60 seconds;
one more is refused with 429 ``query_rate_limited`` and a ``Retry-After``, before
anything is read or run.

A query is any request that runs SQL in a query child: ``POST /query``,
``POST /warehouse/query``, ``POST /warehouse/rows``, ``POST /warehouse/aggregate`` and
``POST /warehouse/exports``. A profile (``GET /warehouse/tables/{name}/profile``) is not
counted: it is computed once per snapshot and then served from a file.

``0``, the default, turns it off: which number fits is to be measured on a real
deployment (#1245). A single-user deployment has one user and applies none.
"""

from __future__ import annotations

from ..rate_limit import RateLimited, WindowLimiter, per_minute_from_env
from .ownership import multi_user_mode
from .responses import ServiceResponse

QUERY_RATE_PER_MINUTE_ENV = "KPUBDATA_BUILDER_QUERY_RATE_PER_MINUTE"

#: The routes that run SQL in a query child, as ``(method, path)``.
QUERY_ROUTES = frozenset(
    {
        ("POST", "/query"),
        ("POST", "/warehouse/query"),
        ("POST", "/warehouse/rows"),
        ("POST", "/warehouse/aggregate"),
        ("POST", "/warehouse/exports"),
    }
)


def query_rate_per_minute() -> int:
    """Queries one user may start in any 60 seconds; ``0`` when there is no limit."""
    if not multi_user_mode():
        return 0
    return per_minute_from_env(QUERY_RATE_PER_MINUTE_ENV)


class QueryRateLimiter:
    """The per-user query limit of one service. Thread-safe; holds owner ids only."""

    def __init__(self, limiter: WindowLimiter | None = None) -> None:
        self._limiter = limiter or WindowLimiter(query_rate_per_minute)

    def refusal(self, method: str, path: str, owner_id: str | None) -> ServiceResponse | None:
        """The 429 for a query past the user's limit; None when it may run.

        A request that is not a query is never counted.
        """
        if (method, path) not in QUERY_ROUTES:
            return None
        try:
            self._limiter.admit(owner_id or "")
        except RateLimited as limited:
            return ServiceResponse(
                429,
                {
                    "error": "you have run as many queries as the server allows for now; "
                    "try again shortly",
                    "code": "query_rate_limited",
                    "retry_after_seconds": limited.retry_after_seconds,
                },
            )
        return None


__all__ = [
    "QUERY_RATE_PER_MINUTE_ENV",
    "QUERY_ROUTES",
    "QueryRateLimiter",
    "query_rate_per_minute",
]
