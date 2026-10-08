"""How many async builds one user may have at once in a multi-user deployment (#1189).

The async build queue was metered for the whole service: ten queued jobs, whoever
submitted them. One user could fill it and every other user's build was refused with
``build_queue_full`` until theirs ran. Each owner may now have a number of builds
queued or running; past that, ``POST /builds`` answers 429 ``build_owner_limit`` and
the queue stays open to everyone else.

The default, 2, was chosen without measurement (kpubdata#812), like the upload limits
(#1045), and is to be revisited after the first deployment. ``0`` turns it off. A
single-user deployment has one owner and applies none.
"""

from __future__ import annotations

import os

from .ownership import multi_user_mode

MAX_ACTIVE_BUILDS_PER_OWNER_ENV = "KPUBDATA_BUILDER_MAX_ACTIVE_BUILDS_PER_OWNER"
DEFAULT_MAX_ACTIVE_BUILDS_PER_OWNER = 2


def resolve_owner_build_limit() -> int | None:
    """Builds one owner may have queued or running, or None when there is no limit.

    An unset, malformed or negative value is the default; the start-up check reports a
    malformed one (``startup_settings``).
    """
    if not multi_user_mode():
        return None
    raw = os.environ.get(MAX_ACTIVE_BUILDS_PER_OWNER_ENV, "").strip()
    value = DEFAULT_MAX_ACTIVE_BUILDS_PER_OWNER
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            value = parsed
    return value or None


__all__ = [
    "DEFAULT_MAX_ACTIVE_BUILDS_PER_OWNER",
    "MAX_ACTIVE_BUILDS_PER_OWNER_ENV",
    "resolve_owner_build_limit",
]
