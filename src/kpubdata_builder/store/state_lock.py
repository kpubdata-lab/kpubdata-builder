"""Who may write a state directory while a backup or a restore runs (#1095).

A backup of ten SQLite stores and the files beside them is one point in time only if
nothing writes while it is taken. An online backup of each store is consistent for that
store alone: a build that finishes between two of them leaves its ending in one and not
in the other. So a backup does not run beside a writer at all.

Every command that writes state — ``serve`` for as long as it runs, ``build``,
``rebuild-index``, ``prune-cancelled``, ``warehouse-gc`` and ``warehouse-hold`` — holds
a *shared* lock on the directory it writes. ``backup`` and ``restore`` take it
*exclusively* and do not wait: while anything holds it they refuse, and while they hold
it every writer refuses to start. Stopping the server is what drains it: ``SIGTERM``
finishes or cancels the running builds as on any shutdown (``docs/deploy.md`` §7).

The lock is ``flock`` on ``.state.lock`` in the directory. The kernel releases it when
the process ends, however it ends, so a killed server leaves no lock behind. It is not
a lock across machines; ``/data`` is a local block volume and one instance runs
(``docs/deploy.md`` §6).

A restore that was killed while it was swapping the directory's contents leaves a
journal behind (``store/state_backup.py``). A shared lock is then refused too: a
server must not start on half of one state and half of another.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path

#: The lock file, in the directory it guards. Never backed up or restored.
LOCK_FILENAME = ".state.lock"

#: Where a restore keeps its staging and its journal, in the directory it restores.
RESTORE_DIRNAME = ".restoring"
RESTORE_JOURNAL = "journal.json"


class StateInUse(RuntimeError):
    """The directory is held by another process, or a restore into it was interrupted."""


def interrupted_restore(root: Path) -> bool:
    """Whether a restore into ``root`` stopped while it was swapping the contents."""
    return (root / RESTORE_DIRNAME / RESTORE_JOURNAL).is_file()


@contextmanager
def _one(root: Path, *, exclusive: bool, purpose: str) -> Iterator[None]:
    fd = os.open(root / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            if exclusive:
                raise StateInUse(
                    f"{root} is in use: a server, a build or a maintenance command is "
                    f"writing it, so {purpose} would not be one point in time. Stop it "
                    "first — stopping the server drains its builds as any shutdown "
                    "does — and run this again."
                ) from None
            raise StateInUse(
                f"{root} is being backed up or restored; {purpose} would change it "
                "midway. Run this again when that has finished."
            ) from None
        if not exclusive and interrupted_restore(root):
            raise StateInUse(
                f"a restore into {root} was interrupted while it swapped the contents, "
                "so the directory holds part of two states. Run `kpubdata-builder "
                f"restore --recover --output-dir {root}` to put the earlier state back, "
                "or run the restore again."
            )
        yield
    finally:
        os.close(fd)


@contextmanager
def state_lock(roots: Sequence[Path], *, exclusive: bool, purpose: str) -> Iterator[None]:
    """Hold every root of ``roots``, shared or exclusive, or raise without waiting.

    A root that does not exist has nothing in it to protect and is skipped; ``roots``
    may repeat one.

    Raises:
        StateInUse: A root is held the other way, or holds an interrupted restore and
            the lock asked for is shared.
    """
    seen: set[Path] = set()
    with ExitStack() as stack:
        for root in roots:
            resolved = root.resolve()
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            stack.enter_context(_one(resolved, exclusive=exclusive, purpose=purpose))
        yield


__all__ = [
    "LOCK_FILENAME",
    "RESTORE_DIRNAME",
    "RESTORE_JOURNAL",
    "StateInUse",
    "interrupted_restore",
    "state_lock",
]
