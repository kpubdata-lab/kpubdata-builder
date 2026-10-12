"""Stop a query whose requester has gone (#1245).

A query runs in a child process until it answers or times out. When the client that
asked for it closes the connection, nobody reads the answer, and the child kept its
query slot and its memory reservation until the end anyway. The HTTP layer now watches
the request's connection while the request is answered (:func:`watching`), and the
query engine looks at the watch between short waits for its child: once the client has
gone, the child is stopped and the engine raises ``QueryCancelledError``.

The watch is a context variable, so it belongs to the thread answering the request and
to nothing the request does not run itself. Code that runs a query outside a request — a
test, the CLI — has no watch and waits for its child as before.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class Watch:
    """Whether the requester has gone, as the HTTP layer can tell."""

    def __init__(self, gone: Callable[[], bool]) -> None:
        self._gone = gone
        #: Set once a look found the requester gone; the request is not answered then.
        self.fired = False

    def requester_gone(self) -> bool:
        if not self.fired and self._gone():
            self.fired = True
        return self.fired


_CURRENT: ContextVar[Watch | None] = ContextVar("kpubdata_builder_query_watch", default=None)


@contextmanager
def watching(gone: Callable[[], bool]) -> Iterator[Watch]:
    """Let queries run inside the block stop when ``gone()`` is true."""
    watch = Watch(gone)
    token = _CURRENT.set(watch)
    try:
        yield watch
    finally:
        _CURRENT.reset(token)


def current() -> Watch | None:
    """The watch of the request being answered, or None outside one."""
    return _CURRENT.get()


__all__ = ["Watch", "current", "watching"]
