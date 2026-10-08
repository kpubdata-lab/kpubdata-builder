"""Opening an older release's build index does not leave it empty (#1096).

The index is derived, so a change of schema makes its table again. It used to be made
empty and stamped with this release's version: ``serve`` rebuilt it from the manifests
first, but anything else that opened an older index emptied it for good — its version
then said it was current, so nothing rebuilt it. It is now filled from the manifests in
the transaction that makes the table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from kpubdata_builder.store import build_index
from kpubdata_builder.store.build_index import SCHEMA_VERSION, SqliteBuildIndex

_INDEX = "_builds.sqlite"


def _run(root: Path, run_id: str, **manifest: object) -> None:
    """A finished run on disk: the manifest the index is derived from."""
    (root / run_id).mkdir()
    document = {
        "run_id": run_id,
        "started_at": "2026-10-01T10:00:00Z",
        "finished_at": "2026-10-01T10:05:00Z",
        **manifest,
    }
    (root / run_id / "manifest.json").write_text(json.dumps(document), encoding="utf-8")


def _older_index(root: Path, *, version: int = SCHEMA_VERSION - 1) -> None:
    """An index as an earlier release left it: its own columns, its own version, one row.

    In WAL mode, as every release has made it. A file still in rollback mode would have
    two openers race to change the journal mode, which SQLite refuses at once rather
    than waiting — a race of first creation, not of opening an older index.
    """
    with closing(sqlite3.connect(root / _INDEX)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        # No owner_id column: what version 4 had.
        conn.execute(
            "CREATE TABLE builds (run_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "started_at TEXT, finished_at TEXT, spec_digest TEXT, error TEXT, "
            "created_by TEXT, dataset_id TEXT)"
        )
        conn.execute(
            "INSERT INTO builds (run_id, status, finished_at) VALUES "
            "('from-the-old-index', 'ok', '2026-09-01T00:00:00Z')"
        )


def _stored(root: Path) -> tuple[int | None, set[str], set[str]]:
    """The version, the columns of ``builds`` and its run ids, read without the class."""
    with closing(sqlite3.connect(root / _INDEX)) as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(builds)")}
        runs = {str(row[0]) for row in conn.execute("SELECT run_id FROM builds")}
    return version, columns, runs


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every scan of the manifests, by the directory scanned."""
    seen: list[Path] = []
    real = build_index._iter_manifest_entries

    def counted(output_root: Path):  # type: ignore[no-untyped-def]
        seen.append(output_root)
        return real(output_root)

    monkeypatch.setattr(build_index, "_iter_manifest_entries", counted)
    return seen


def test_an_older_index_is_filled_from_the_manifests_when_it_is_opened(tmp_path: Path) -> None:
    _run(tmp_path, "mine", owner_id="issuer#alice", created_by="alice")
    _run(tmp_path, "failed", errors=["boom"], owner_id="issuer#bob")
    _older_index(tmp_path)

    index = SqliteBuildIndex(tmp_path)
    try:
        builds = {entry.run_id: entry for entry in index.list_builds()}
    finally:
        index.close()

    # Every run with a manifest is there, with what the manifest says of it.
    assert set(builds) == {"mine", "failed"}
    assert (builds["mine"].status, builds["mine"].owner_id, builds["mine"].created_by) == (
        "ok",
        "issuer#alice",
        "alice",
    )
    assert (builds["failed"].status, builds["failed"].owner_id) == ("failed", "issuer#bob")
    version, columns, runs = _stored(tmp_path)
    assert version == SCHEMA_VERSION
    assert "owner_id" in columns
    # A row of the old index that no manifest stands behind is not carried over.
    assert runs == {"mine", "failed"}


def test_it_is_filled_whoever_opens_it_and_stays_filled(tmp_path: Path) -> None:
    """The case that lost the runs: opened once outside ``serve``, then served."""
    _run(tmp_path, "earlier")
    _older_index(tmp_path)

    SqliteBuildIndex(tmp_path).close()

    # The version now says the index is this release's, so ``serve`` rebuilds nothing…
    assert build_index.bring_index_up_to_date(tmp_path) is None
    # …and the run is in it all the same.
    index = SqliteBuildIndex(tmp_path)
    try:
        assert [entry.run_id for entry in index.list_builds()] == ["earlier"]
    finally:
        index.close()


def test_a_failure_while_it_is_filled_leaves_the_older_index_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "earlier")
    _older_index(tmp_path)

    def refuse(entry: object) -> tuple[str | None, ...]:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(build_index, "_row_of", refuse)
    with pytest.raises(RuntimeError, match="the disk went away"):
        SqliteBuildIndex(tmp_path)
    monkeypatch.undo()

    # Not an empty table under this release's version: the old table, the old version.
    version, columns, runs = _stored(tmp_path)
    assert version == SCHEMA_VERSION - 1
    assert "owner_id" not in columns
    assert runs == {"from-the-old-index"}

    # And the next opening does the whole thing.
    index = SqliteBuildIndex(tmp_path)
    try:
        assert [entry.run_id for entry in index.list_builds()] == ["earlier"]
    finally:
        index.close()
    assert _stored(tmp_path)[0] == SCHEMA_VERSION


def test_an_index_that_is_this_releases_is_not_scanned_for(
    tmp_path: Path, scans: list[Path]
) -> None:
    index = SqliteBuildIndex(tmp_path)
    index.insert_or_replace("kept", "ok", None, "2026-10-01T10:05:00Z")
    index.close()
    _run(tmp_path, "on-disk-only")
    scans.clear()

    reopened = SqliteBuildIndex(tmp_path)
    try:
        assert [entry.run_id for entry in reopened.list_builds()] == ["kept"]
    finally:
        reopened.close()
    assert scans == []


def test_an_index_that_was_not_there_is_made_empty_without_a_scan(
    tmp_path: Path, scans: list[Path]
) -> None:
    """Nothing it could have lost; ``serve`` and ``rebuild-index`` fill it from the manifests."""
    _run(tmp_path, "on-disk-only")

    index = SqliteBuildIndex(tmp_path)
    try:
        assert index.list_builds() == []
    finally:
        index.close()
    assert scans == []
    assert _stored(tmp_path)[0] == SCHEMA_VERSION


def test_a_file_with_no_version_in_it_is_filled_too(tmp_path: Path) -> None:
    """``serve`` leaves such a file alone as not its own; opening it must not empty it."""
    _run(tmp_path, "earlier")
    with closing(sqlite3.connect(tmp_path / _INDEX)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE builds (run_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.execute("INSERT INTO builds VALUES ('from-somewhere', 'ok')")
    assert build_index.bring_index_up_to_date(tmp_path) is None

    index = SqliteBuildIndex(tmp_path)
    try:
        assert [entry.run_id for entry in index.list_builds()] == ["earlier"]
    finally:
        index.close()
    assert _stored(tmp_path) == (SCHEMA_VERSION, set(build_index._BUILDS_COLUMNS), {"earlier"})


def test_a_rebuild_scans_the_manifests_once(tmp_path: Path, scans: list[Path]) -> None:
    """The new file a rebuild writes into is not filled a second time by being opened."""
    _run(tmp_path, "earlier")
    _older_index(tmp_path)

    assert build_index.rebuild_index(tmp_path) == 1

    assert scans == [tmp_path]
    assert _stored(tmp_path) == (SCHEMA_VERSION, set(build_index._BUILDS_COLUMNS), {"earlier"})


def test_a_newer_index_is_still_refused_and_left_alone(tmp_path: Path, scans: list[Path]) -> None:
    _run(tmp_path, "earlier")
    _older_index(tmp_path, version=SCHEMA_VERSION + 1)

    with pytest.raises(build_index.UnsupportedSchemaVersionError):
        SqliteBuildIndex(tmp_path)

    assert scans == []
    assert _stored(tmp_path)[2] == {"from-the-old-index"}


def test_two_openers_of_an_older_index_both_end_with_the_whole_of_it(tmp_path: Path) -> None:
    for number in range(20):
        _run(tmp_path, f"run-{number:02d}")
    _older_index(tmp_path)
    together = threading.Barrier(2)
    seen: list[set[str]] = []
    failures: list[BaseException] = []

    def open_it() -> None:
        try:
            together.wait(timeout=10)
            index = SqliteBuildIndex(tmp_path)
            try:
                seen.append({entry.run_id for entry in index.list_builds(limit=None)})
            finally:
                index.close(checkpoint=False)
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below
            failures.append(error)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    expected = {f"run-{number:02d}" for number in range(20)}
    assert seen == [expected, expected]
    assert _stored(tmp_path)[0] == SCHEMA_VERSION
