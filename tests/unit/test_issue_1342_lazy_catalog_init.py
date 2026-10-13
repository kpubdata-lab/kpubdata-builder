"""The table catalog is opened once, however many threads ask first (#1342).

The service is threaded and opened the catalog on first use without a lock, so two first
warehouse requests could each open one. Nothing here sleeps: a thread is held inside the
constructor by an event, and the test goes on only when the second thread has shown
where it is, either waiting for the lock or inside a second constructor.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import TracebackType

import pytest

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service import app as app_module
from kpubdata_builder.stages.bronze.build import SourceClient
from kpubdata_builder.warehouse import CATALOG_FILENAME, TableCatalog

#: Only a guard against a hang; no assertion depends on how long anything takes.
GUARD_SECONDS = 30.0


def _no_client(**_kwargs: object) -> SourceClient:
    raise AssertionError("no provider is called here")


class _HeldConstructor:
    """Stands in for ``TableCatalog``: holds the first call open until it is released."""

    def __init__(self, second_caller_seen: threading.Event, *, first_call_fails: bool) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self._second_caller_seen = second_caller_seen
        self._first_call_fails = first_call_fails
        self._count_lock = threading.Lock()

    def __call__(self, root: Path) -> TableCatalog:
        with self._count_lock:
            self.calls += 1
            call = self.calls
        if call == 1:
            self.entered.set()
            assert self.release.wait(GUARD_SECONDS)
            if self._first_call_fails:
                raise OSError("the catalog could not be opened")
        elif not self.release.is_set():
            # A second constructor while the first is still open: the race itself.
            self._second_caller_seen.set()
        return TableCatalog(root)


class _WatchedLock:
    """A lock that says when a thread finds it taken."""

    def __init__(self, second_caller_seen: threading.Event) -> None:
        self._lock = threading.Lock()
        self._second_caller_seen = second_caller_seen

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self._second_caller_seen.set()
            self._lock.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lock.release()


def _race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, first_call_fails: bool
) -> tuple[BuilderService, _HeldConstructor, list[object]]:
    """Hold one thread in the constructor until a second has reached the catalog."""
    warehouse = tmp_path / "warehouse"
    service = BuilderService(
        output_root=tmp_path, client_factory=_no_client, warehouse_root=warehouse
    )
    second_caller_seen = threading.Event()
    constructor = _HeldConstructor(second_caller_seen, first_call_fails=first_call_fails)
    monkeypatch.setattr(app_module, "TableCatalog", constructor)
    monkeypatch.setattr(service, "_catalog_lock", _WatchedLock(second_caller_seen), raising=False)
    results: list[object] = [None, None]

    def ask(slot: int) -> None:
        try:
            results[slot] = service._table_catalog()
        except OSError as exc:
            results[slot] = exc

    first = threading.Thread(target=ask, args=(0,))
    second = threading.Thread(target=ask, args=(1,))
    first.start()
    assert constructor.entered.wait(GUARD_SECONDS)
    second.start()
    assert second_caller_seen.wait(GUARD_SECONDS)
    constructor.release.set()
    first.join(GUARD_SECONDS)
    second.join(GUARD_SECONDS)
    assert not first.is_alive()
    assert not second.is_alive()
    return service, constructor, results


def test_concurrent_first_use_opens_one_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, constructor, results = _race(tmp_path, monkeypatch, first_call_fails=False)

    assert constructor.calls == 1
    catalog = results[0]
    assert isinstance(catalog, TableCatalog)
    assert results[1] is catalog
    assert service._table_catalog() is catalog
    # Whole, not merely assigned: it answers a question.
    assert catalog.create_table("ws-1", "sales").logical_name == "sales"
    catalog.close()


def test_failed_open_is_not_published_and_the_waiting_thread_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, constructor, results = _race(tmp_path, monkeypatch, first_call_fails=True)

    assert isinstance(results[0], OSError)
    # The thread that was waiting found nothing published, and opened the catalog itself.
    assert constructor.calls == 2
    catalog = results[1]
    assert isinstance(catalog, TableCatalog)
    assert service._table_catalog() is catalog
    assert constructor.calls == 2
    catalog.close()


def test_failed_open_leaves_nothing_and_the_next_call_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = BuilderService(
        output_root=tmp_path, client_factory=_no_client, warehouse_root=tmp_path / "warehouse"
    )
    attempts: list[Path] = []

    def fails_once(root: Path) -> TableCatalog:
        attempts.append(root)
        if len(attempts) == 1:
            raise OSError("the catalog could not be opened")
        return TableCatalog(root)

    monkeypatch.setattr(app_module, "TableCatalog", fails_once)

    with pytest.raises(OSError, match="could not be opened"):
        service._table_catalog()
    assert service._catalog is None

    catalog = service._table_catalog()

    assert isinstance(catalog, TableCatalog)
    assert service._table_catalog() is catalog
    assert len(attempts) == 2
    catalog.close()


def test_no_warehouse_opens_no_catalog_from_any_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=_no_client)

    def never(root: Path) -> TableCatalog:
        raise AssertionError(f"a catalog was opened under {root}")

    monkeypatch.setattr(app_module, "TableCatalog", never)
    # The build index is there from the start; asking for the catalog adds nothing to it.
    files_before = sorted(tmp_path.rglob("*"))
    callers = 4
    barrier = threading.Barrier(callers)
    results: list[object] = ["unset"] * callers

    def ask(slot: int) -> None:
        barrier.wait(GUARD_SECONDS)
        try:
            results[slot] = service._table_catalog()
        except AssertionError as exc:
            results[slot] = exc

    threads = [threading.Thread(target=ask, args=(slot,)) for slot in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(GUARD_SECONDS)

    assert results == [None] * callers
    assert list(tmp_path.rglob(CATALOG_FILENAME)) == []
    assert sorted(tmp_path.rglob("*")) == files_before
