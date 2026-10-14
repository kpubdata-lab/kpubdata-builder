"""How often Builder may call one provider, for every user together (#1245).

Builds, previews and key probes call providers from the server's one address. A provider
that sees too many calls from it blocks the address — for every user of the deployment,
not only the one whose build made the calls. ``KPUBDATA_BUILDER_PROVIDER_CALLS_PER_MINUTE``
bounds how many calls to one provider Builder starts in any 60 seconds, counted per
provider across all users and all builds of the process. ``0``, the default, turns it off.

What one call is: one page Builder takes from kpubdata (a ``list`` call, or one page of
``list_all``), and one dataset of a key probe. kpubdata makes the HTTP requests; Builder
cannot see each one. Some kpubdata datasets request every page of a call before handing
the first over (#481), so those pages leave in one burst; the slots they take are then
counted against the calls that follow, which wait for them. Over a minute the bound
holds; inside one call of such a dataset it does not.

A build waits for its turn: it is background work, and a refusal would fail it. A
preview and a probe hold a request thread and a person waiting, so they are refused at
once with how long to wait (:class:`~kpubdata_builder.rate_limit.RateLimited`). A build
that is cancelled while it waits — by its user, its time limit or shutdown — stops
waiting within half a second and gives its slot back (#1328).
"""

from __future__ import annotations

from collections.abc import Callable

from .rate_limit import RateLimited, WaitCancelled, WindowLimiter, per_minute_from_env

PROVIDER_CALLS_PER_MINUTE_ENV = "KPUBDATA_BUILDER_PROVIDER_CALLS_PER_MINUTE"


def provider_calls_per_minute() -> int:
    """The configured limit; ``0`` (off) when unset or unusable."""
    return per_minute_from_env(PROVIDER_CALLS_PER_MINUTE_ENV)


#: One for the process: the provider sees the server's address, not a user or a build.
LIMITER = WindowLimiter(provider_calls_per_minute)


def wait_for_call(provider: str, *, cancelled: Callable[[], bool] | None = None) -> float:
    """Wait until ``provider`` may be called; the seconds waited.

    Raises:
        WaitCancelled: ``cancelled`` answered True during the wait (#1328). The slot
            was given back.
    """
    return LIMITER.wait(provider, cancelled=cancelled)


def admit_call(provider: str) -> None:
    """Count one call to ``provider`` now.

    Raises:
        RateLimited: The provider's window is full. Nothing was counted.
    """
    LIMITER.admit(provider)


__all__ = [
    "LIMITER",
    "PROVIDER_CALLS_PER_MINUTE_ENV",
    "RateLimited",
    "WaitCancelled",
    "admit_call",
    "provider_calls_per_minute",
    "wait_for_call",
]
