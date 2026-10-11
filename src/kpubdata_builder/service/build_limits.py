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
import threading

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


MAX_QUEUED_BUILDS_ENV = "KPUBDATA_BUILDER_MAX_QUEUED_BUILDS"
#: What the queue held when its size was written into ``BuilderService`` (#1108).
DEFAULT_MAX_QUEUED_BUILDS = 10
#: A waiting job keeps its spec, and in a multi-user deployment the request's provider
#: keys, in memory; the queue's size bounds how much of that there is. Chosen without
#: measurement, like the other limits (kpubdata#812).
MAX_QUEUED_BUILDS_CEILING = 1000


def resolve_max_queued_builds() -> int:
    """How many async builds may wait for a worker, for the whole service.

    Unset or empty is the default.

    Raises:
        ValueError: The value is not an integer from 1 to the ceiling. Unlike the
            limits above, this one does not fall back: a queue of another size than
            the operator wrote is not a setting to find out about under load, and
            ``serve`` refuses to start (``startup_settings``).
    """
    raw = os.environ.get(MAX_QUEUED_BUILDS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_QUEUED_BUILDS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_QUEUED_BUILDS_CEILING:
        raise ValueError(
            f"{MAX_QUEUED_BUILDS_ENV} must be an integer from 1 to "
            f"{MAX_QUEUED_BUILDS_CEILING}, got {raw!r}"
        )
    return value


BUILD_TIME_LIMIT_ENV = "KPUBDATA_BUILDER_BUILD_TIME_LIMIT_SECONDS"
#: Six hours: generous for a large ``param_grid`` build, and short enough that a stuck
#: job gives its build slot back the same day. Chosen without measurement, like the
#: other limits (kpubdata#812); ``0`` turns it off.
DEFAULT_BUILD_TIME_LIMIT_SECONDS = 6 * 60 * 60


def resolve_build_time_limit() -> float | None:
    """How long an async build may run once it started, in seconds; None when unlimited.

    Counted from when the job leaves the queue (#1119): time spent waiting for a build
    slot is not running time. An unset, malformed, negative or non-finite value is the
    default; the start-up check reports a malformed one (``startup_settings``). A
    number larger than a timer can wait is read as the longest one it can.
    """
    raw = os.environ.get(BUILD_TIME_LIMIT_ENV, "").strip()
    value = float(DEFAULT_BUILD_TIME_LIMIT_SECONDS)
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = -1.0
        if parsed >= 0 and parsed != float("inf"):
            # No longer than a timer can wait: a larger number raises in the timer's
            # thread instead of counting down.
            value = min(parsed, threading.TIMEOUT_MAX)
    return value or None


__all__ = [
    "BUILD_TIME_LIMIT_ENV",
    "DEFAULT_BUILD_TIME_LIMIT_SECONDS",
    "DEFAULT_MAX_ACTIVE_BUILDS_PER_OWNER",
    "DEFAULT_MAX_QUEUED_BUILDS",
    "MAX_ACTIVE_BUILDS_PER_OWNER_ENV",
    "MAX_QUEUED_BUILDS_CEILING",
    "MAX_QUEUED_BUILDS_ENV",
    "resolve_build_time_limit",
    "resolve_max_queued_builds",
    "resolve_owner_build_limit",
]
