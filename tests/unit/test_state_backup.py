"""One backup holds all of the state, and a restore puts it back whole or not at all (#1095).

The state is made the way Builder makes it — a real build into a real catalog, and every
store opened and written through its own class — then backed up, broken in the ways a
backup breaks, and restored. A crash during a restore is a real one: the restore runs in
another process, which is killed in the middle of the swap.
"""

from __future__ import annotations

import ast
import base64
import fnmatch
import gc
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

import kpubdata_builder.cli as cli_module
from kpubdata_builder.cli import main
from kpubdata_builder.credentials.crypto import AesGcmCredentialCipher
from kpubdata_builder.credentials.store import SQLiteCredentialRepository
from kpubdata_builder.events.store import BuildEventStore
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.service.analyses_api import AnalysisStore
from kpubdata_builder.service.exports_api import ExportStore
from kpubdata_builder.service.provider_tests import ProviderTestLog
from kpubdata_builder.service.publish import PublishReceiptStore
from kpubdata_builder.service.revisions import RevisionStore
from kpubdata_builder.service.user_ledger import UserLedger
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.store import rebuild_index, state_backup
from kpubdata_builder.store.build_index import SqliteBuildIndex
from kpubdata_builder.store.inventory import OUTPUT_PATHS, STORES
from kpubdata_builder.store.state_lock import (
    LOCK_FILENAME,
    RESTORE_DIRNAME,
    StateInUse,
    interrupted_restore,
    state_lock,
)
from kpubdata_builder.uploads.store import SQLiteUploadRepository
from kpubdata_builder.warehouse import TableCatalog

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "kpubdata_builder"
_SECRET = "provider-key-that-must-stay-encrypted"
_MASTER_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
_BIG_UPLOAD = b"a,b\n" + b"1,2\n" * 64


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Dataset:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def list(self, **_params: object) -> _Result:
        self._client.started.set()
        if self._client.gate is not None:
            assert self._client.gate.wait(timeout=30)
        return _Result([{"id": "1", "value": self._client.value}])


class _Client:
    def __init__(self, value: str, gate: threading.Event | None = None) -> None:
        self.value = value
        self.gate = gate
        self.started = threading.Event()

    def dataset(self, _key: str) -> _Dataset:
        return _Dataset(self)


def _spec() -> BuildSpec:
    return BuildSpec(
        dataset_id="backup.table",
        title="Backup",
        description="d",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def _cipher() -> AesGcmCredentialCipher:
    return AesGcmCredentialCipher.from_base64(_MASTER_KEY)


def _populate(output: Path) -> str:
    """Every kind of state Builder keeps, written by the code that keeps it. Returns an
    upload id kept as a file."""
    run_build(
        _spec(),
        client=_Client("first"),
        output_root=output,
        run_id="run-1",
        catalog=TableCatalog(output / "warehouse"),
    )
    events = BuildEventStore(output)
    events.record_submission(
        "run-1", owner_id="owner-1", created_by="alice", submitted_at=datetime.now(timezone.utc)
    )
    events.close()
    SQLiteCredentialRepository(output / ".service" / "provider-credentials.sqlite3", _cipher()).put(
        "owner-1", "datago", _SECRET
    )
    uploads = SQLiteUploadRepository(
        output / ".service" / "uploads.sqlite3", spill_threshold_bytes=32
    )
    uploads.put("owner-1", content=b"x\n1\n", format="csv", encoding="utf-8", original_filename="s")
    big = uploads.put(
        "owner-1", content=_BIG_UPLOAD, format="csv", encoding="utf-8", original_filename="b"
    )
    assert PublishReceiptStore(output).audit_entries(owner_key="owner-1", run_id="run-1") == []
    ProviderTestLog(output / ".service" / "provider_tests.sqlite3")
    RevisionStore(output / ".service" / "revisions.sqlite3")
    UserLedger(output / ".service" / "users.sqlite3")
    AnalysisStore(output / ".service" / "analyses.sqlite3")
    exports = ExportStore(output / ".service" / "exports")
    export_id, _ = exports.create()
    exports.save({"export_id": export_id, "state": "done"})
    rebuild_index(output)
    return big.upload_id


def _tree(root: Path) -> dict[str, str]:
    """Every file under ``root`` by its SHA-256, the lock and a restore's leftovers aside.

    Collects first. From Python 3.11 a ``sqlite3`` connection that was dropped without
    ``close()`` is closed by the cyclic collector, not at its last reference, and until
    then its ``-wal`` and ``-shm`` lie beside the database. A tree hashed with one still
    open differs from the same state a moment later, at a moment no test chooses.
    """
    gc.collect()
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and path.name != LOCK_FILENAME and not rel.startswith(RESTORE_DIRNAME):
            found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


@pytest.fixture
def state(tmp_path: Path) -> tuple[Path, Path, str]:
    output = tmp_path / "data"
    upload_id = _populate(output)
    archive = tmp_path / "backups" / "state.tar"
    state_backup.backup(output, archive, warehouse=output / "warehouse")
    return output, archive, upload_id


# -- the inventory -----------------------------------------------------------------------


def test_every_store_has_the_version_this_release_reads() -> None:
    assert set(state_backup.supported_versions()) == {store.path for store in STORES}


def test_every_path_the_code_writes_is_in_the_inventory(tmp_path: Path) -> None:
    """A new file or directory under the output directory fails here until it is listed."""
    output = tmp_path / "data"
    _populate(output)

    scan = state_backup.scan_output(output, "warehouse")

    assert scan.problems == []
    listed = {store.path for store in STORES if store.root == "output"}
    assert listed <= set(scan.stores)
    assert any(path.startswith(".service/uploads.sqlite3.blobs/") for path in scan.files)
    assert any(path.startswith(".service/exports/") for path in scan.files)
    assert "run-1/manifest.json" in scan.files


def _service_children(tree: ast.AST) -> set[str]:
    """Every name the code joins under ``.service``: ``x / ".service" / "name"``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
            and isinstance(node.left, ast.BinOp)
            and isinstance(node.left.right, ast.Constant)
            and node.left.right.value == ".service"
        ):
            names.add(node.right.value)
    return names


def _service_name_is_listed(name: str) -> bool:
    rel = f".service/{name}"
    return rel in {store.path for store in STORES} or any(
        fnmatch.fnmatchcase(rel, entry.pattern) for entry in OUTPUT_PATHS
    )


def test_every_name_the_code_puts_under_service_is_listed() -> None:
    names: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        names |= _service_children(ast.parse(path.read_text(encoding="utf-8")))

    assert names, "the scan found nothing; it no longer reads the code"
    assert sorted(name for name in names if not _service_name_is_listed(name)) == []


def test_the_service_check_sees_a_name_nobody_listed() -> None:
    tree = ast.parse('root / ".service" / "sessions.sqlite3"\nroot / ".service" / "exports"\n')

    assert [
        name for name in sorted(_service_children(tree)) if not _service_name_is_listed(name)
    ] == ["sessions.sqlite3"]


def test_the_deployment_guide_lists_every_path() -> None:
    document = (_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    start = document.index("### 6.2 ")
    section = document[start : document.index("\n## ", start)]

    assert [entry.pattern for entry in OUTPUT_PATHS if f"`{entry.pattern}`" not in section] == []


def test_a_path_nobody_listed_refuses_the_backup(tmp_path: Path) -> None:
    output = tmp_path / "data"
    _populate(output)
    (output / ".service" / "sessions.sqlite3").write_bytes(b"")
    (output / "notes.txt").write_text("x", encoding="utf-8")
    archive = tmp_path / "state.tar"

    with pytest.raises(state_backup.StateBackupRefused) as refused:
        state_backup.backup(output, archive, warehouse=output / "warehouse")

    assert [problem.split(" ")[0] for problem in refused.value.problems] == [
        ".service/sessions.sqlite3",
        "notes.txt",
    ]
    assert not archive.exists()


# -- backup ------------------------------------------------------------------------------


def test_the_archive_lists_every_member_with_its_checksum_and_version(
    state: tuple[Path, Path, str],
) -> None:
    _, archive, _ = state

    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        member = tar.extractfile("backup.json")
        assert member is not None
        manifest = json.loads(member.read())

    entries = {entry["path"]: entry for entry in manifest["entries"]}
    assert set(entries) | {"backup.json"} == names
    supported = state_backup.supported_versions()
    for store in STORES:
        name = f"{'output' if store.root == 'output' else 'warehouse'}/{store.path}"
        assert entries[name]["schema_version"] == supported[store.path], name
    assert manifest["warehouse"] == "warehouse"
    assert (
        "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY"
        in manifest["not_included"]["credential_master_key"]
    )


def test_provider_keys_stay_encrypted_and_the_master_key_is_not_in_the_archive(
    state: tuple[Path, Path, str],
) -> None:
    _, archive, _ = state
    data = archive.read_bytes()

    assert _SECRET.encode() not in data
    assert _MASTER_KEY.encode() not in data
    assert bytes(range(32)) not in data


def test_the_archive_cannot_go_into_what_it_backs_up(tmp_path: Path) -> None:
    output = tmp_path / "data"
    _populate(output)

    with pytest.raises(state_backup.StateBackupRefused, match="inside"):
        state_backup.backup(
            output, output / ".backup" / "state.tar", warehouse=output / "warehouse"
        )
    with pytest.raises(state_backup.StateBackupRefused, match="inside"):
        state_backup.backup(output, tmp_path / "a.tar", work_dir=output / "tmp")

    assert not (output / ".backup").exists()
    assert not (output / "tmp").exists()


def test_a_warehouse_outside_the_output_directory_is_refused(tmp_path: Path) -> None:
    output = tmp_path / "data"
    output.mkdir()

    with pytest.raises(state_backup.StateBackupRefused, match="not a directory of the output"):
        state_backup.backup(output, tmp_path / "a.tar", warehouse=tmp_path / "elsewhere")


def test_a_damaged_store_refuses_the_backup(tmp_path: Path) -> None:
    output = tmp_path / "data"
    _populate(output)
    path = output / ".service" / "users.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    data = bytearray(path.read_bytes())
    data[page_size : page_size + 64] = b"\xff" * 64
    path.write_bytes(bytes(data))

    with pytest.raises(state_backup.StateBackupRefused) as refused:
        state_backup.backup(output, tmp_path / "a.tar", warehouse=output / "warehouse")

    assert any(".service/users.sqlite3" in problem for problem in refused.value.problems)
    assert not (tmp_path / "a.tar").exists()
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".a.tar")] == []


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_a_cubrid_setting_left_behind_is_refused(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "data"
    output.mkdir()
    archive = tmp_path / "state.tar"
    archive.write_bytes(b"")
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")

    argv = (
        ["backup", str(tmp_path / "new.tar"), "--output-dir", str(output)]
        if command == "backup"
        else ["restore", str(archive), "--output-dir", str(output)]
    )

    assert main(argv) == 1
    assert "cubrid" in capsys.readouterr().err.lower()
    assert not (tmp_path / "new.tar").exists()


# -- verification ------------------------------------------------------------------------


def test_a_whole_archive_verifies(state: tuple[Path, Path, str]) -> None:
    _, archive, _ = state

    assert state_backup.verify(archive) == []
    assert [path.name for path in archive.parent.iterdir()] == [archive.name]


def _member_offset(archive: Path, name: str) -> int:
    with tarfile.open(archive) as tar:
        return tar.getmember(name).offset_data


def test_a_changed_byte_is_refused_by_name(state: tuple[Path, Path, str]) -> None:
    _, archive, _ = state
    offset = _member_offset(archive, "output/run-1/manifest.json")
    data = bytearray(archive.read_bytes())
    data[offset] ^= 0xFF
    archive.write_bytes(bytes(data))

    assert state_backup.verify(archive) == [
        "output/run-1/manifest.json does not match its checksum; the archive is damaged"
    ]


def test_a_cut_short_archive_is_refused(state: tuple[Path, Path, str]) -> None:
    _, archive, _ = state
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])

    problems = state_backup.verify(archive)

    assert len(problems) == 1
    assert "damaged or cut short" in problems[0] or "no backup.json" in problems[0]


def test_a_member_the_manifest_does_not_list_is_refused(state: tuple[Path, Path, str]) -> None:
    _, archive, _ = state
    with tarfile.open(archive, "a") as tar:
        info = tarfile.TarInfo("output/run-1/extra.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))

    assert state_backup.verify(archive) == [
        "output/run-1/extra.json is in the archive but not in backup.json"
    ]


def test_a_path_that_climbs_out_is_refused_and_not_written(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("output/../../escaped.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    problems = state_backup.verify(archive)

    assert any("is not a path this archive writes" in problem for problem in problems)
    assert not (tmp_path / "escaped.txt").exists()


def test_an_upload_without_its_file_is_refused(tmp_path: Path) -> None:
    output = tmp_path / "data"
    upload_id = _populate(output)
    (output / ".service" / "uploads.sqlite3.blobs" / f"{upload_id}.bin").unlink()
    archive = tmp_path / "state.tar"
    state_backup.backup(output, archive, warehouse=output / "warehouse")

    assert state_backup.verify(archive) == [
        f"upload {upload_id} names a file the archive does not hold"
    ]


def _newer(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO schema_version (version) VALUES (99)")
        conn.commit()


def test_a_store_a_newer_release_wrote_is_refused(tmp_path: Path) -> None:
    output = tmp_path / "data"
    _populate(output)
    _newer(output / ".service" / "analyses.sqlite3")
    archive = tmp_path / "state.tar"
    state_backup.backup(output, archive, warehouse=output / "warehouse")
    target = tmp_path / "restored"
    target.mkdir()
    (target / "keep.txt").write_text("before", encoding="utf-8")

    problems = state_backup.verify(archive)
    with pytest.raises(state_backup.StateBackupRefused) as refused:
        state_backup.restore(archive, target)

    supported = state_backup.supported_versions()[".service/analyses.sqlite3"]
    expected = (
        f"output/.service/analyses.sqlite3 has schema version 99; this release reads up to "
        f"{supported}. A newer release wrote it: restore it with that release"
    )
    assert problems == [expected]
    assert refused.value.problems == [expected]
    assert sorted(path.name for path in target.iterdir()) == [LOCK_FILENAME, "keep.txt"]


def test_verify_backup_command_says_why(
    state: tuple[Path, Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, archive, _ = state
    assert main(["verify-backup", str(archive)]) == 0
    archive.write_bytes(archive.read_bytes()[:1024])

    assert main(["verify-backup", str(archive)]) == 1
    assert "would not restore" in capsys.readouterr().err


# -- restore -----------------------------------------------------------------------------


def test_a_round_trip_restores_every_store_and_file(state: tuple[Path, Path, str]) -> None:
    output, archive, upload_id = state
    before = _tree(output)
    # The state moves on, then is lost: a second run, a deleted upload, a damaged run.
    run_build(
        _spec(),
        client=_Client("second"),
        output_root=output,
        run_id="run-2",
        catalog=TableCatalog(output / "warehouse"),
    )
    (output / "run-1" / "manifest.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(output / ".service" / "uploads.sqlite3.blobs")

    result = state_backup.restore(archive, output)

    assert result.indexed == 1
    assert result.previous is not None and (result.previous / "run-2").is_dir()
    restored = _tree(output)
    for rel, digest in before.items():
        if rel.endswith((".sqlite", ".sqlite3", "-wal", "-shm")):
            continue
        assert restored.get(rel) == digest, rel
    assert not (output / "run-2").exists()
    assert not (output / RESTORE_DIRNAME).exists()
    secret = SQLiteCredentialRepository(
        output / ".service" / "provider-credentials.sqlite3", _cipher()
    ).get_secret("owner-1", "datago")
    assert secret == _SECRET
    uploads = SQLiteUploadRepository(output / ".service" / "uploads.sqlite3")
    assert uploads.get_content("owner-1", upload_id) == _BIG_UPLOAD
    events = BuildEventStore(output)
    submission = events.submission("run-1")
    events.close()
    assert submission is not None and submission.created_by == "alice"
    catalog = TableCatalog(output / "warehouse")
    (table,) = catalog.list_tables()
    assert table.current_snapshot_id is not None
    assert catalog.get_snapshot(table.current_snapshot_id).run_id == "run-1"
    index = SqliteBuildIndex(output)
    assert [entry.run_id for entry in index.list_builds()] == ["run-1"]
    index.close()


def test_a_restore_into_an_empty_directory(state: tuple[Path, Path, str], tmp_path: Path) -> None:
    _, archive, _ = state
    target = tmp_path / "fresh"

    result = state_backup.restore(archive, target)

    assert result.previous is None
    assert (target / "run-1" / "manifest.json").is_file()
    assert (target / "warehouse" / "_warehouse.sqlite").is_file()


def test_a_damaged_archive_changes_nothing(state: tuple[Path, Path, str]) -> None:
    output, archive, _ = state
    before = _tree(output)
    offset = _member_offset(archive, "output/run-1/manifest.json")
    data = bytearray(archive.read_bytes())
    data[offset] ^= 0xFF
    archive.write_bytes(bytes(data))

    with pytest.raises(state_backup.StateBackupRefused):
        state_backup.restore(archive, output)

    assert _tree(output) == before
    assert not (output / RESTORE_DIRNAME).exists()


_CRASH = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from kpubdata_builder.store import state_backup

    archive, output, where = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

    if where == "unpacking":
        def crash(*_args, **_kwargs):
            os._exit(9)
        state_backup._verify_into = crash
    else:
        swap = state_backup._swap

        def crashing_swap(root, staging, new):
            moves = {"count": 0}
            limit = 1 if where == "moving-out" else len(state_backup._contents(root)) + 1
            rename = os.rename

            def counted(src, dst):
                if moves["count"] == limit:
                    os._exit(9)
                moves["count"] += 1
                rename(src, dst)

            os.rename = counted
            return swap(root, staging, new)

        state_backup._swap = crashing_swap
    state_backup.restore(archive, output)
    """
)


@pytest.mark.parametrize("where", ["unpacking", "moving-out", "moving-in"])
def test_a_restore_killed_midway_leaves_the_earlier_state_recoverable(
    where: str, state: tuple[Path, Path, str], tmp_path: Path
) -> None:
    output, archive, _ = state
    run_build(
        _spec(),
        client=_Client("second"),
        output_root=output,
        run_id="run-2",
        catalog=TableCatalog(output / "warehouse"),
    )
    before = _tree(output)

    killed = subprocess.run(
        [sys.executable, "-c", _CRASH, str(archive), str(output), where],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert killed.returncode == 9, killed.stderr
    if where == "unpacking":
        # Nothing was moved: the earlier state is in place, and a server may start.
        assert _tree(output) == before
        assert not interrupted_restore(output)
    else:
        # Half moved: a server must not start on it.
        assert interrupted_restore(output)
        with (
            pytest.raises(StateInUse, match="interrupted"),
            state_lock([output], exclusive=False, purpose="serve"),
        ):
            pass
    assert state_backup.recover(output) != ""
    assert _tree(output) == before
    assert not (output / RESTORE_DIRNAME).exists()
    assert not interrupted_restore(output)


def test_serve_refuses_a_directory_a_restore_left_half_swapped(
    state: tuple[Path, Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    output, _, _ = state
    (output / RESTORE_DIRNAME).mkdir()
    (output / RESTORE_DIRNAME / "journal.json").write_text(
        json.dumps({"phase": "moving-in", "old": [], "new": []}), encoding="utf-8"
    )

    assert main(["serve", "--output-dir", str(output), "--port", "0"]) == 1
    assert "restore --recover" in capsys.readouterr().err


def test_restore_command_recovers_and_restores(
    state: tuple[Path, Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    output, archive, _ = state

    assert main(["restore", str(archive), "--output-dir", str(output)]) == 0
    printed = capsys.readouterr().out
    assert "rebuilt the build index from the manifests: 1 run(s)" in printed
    assert "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY" in printed
    assert main(["restore", "--recover", "--output-dir", str(output)]) == 0
    assert "no interrupted restore" in capsys.readouterr().out
    assert main(["restore", "--output-dir", str(output)]) == 2


# -- writers and the lock ----------------------------------------------------------------

_SPEC_YAML = """\
dataset_id: backup.table
title: Backup
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


def test_a_running_build_holds_the_backup_off_until_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "data"
    _populate(output)
    spec = tmp_path / "spec.yaml"
    spec.write_text(_SPEC_YAML, encoding="utf-8")
    gate = threading.Event()
    client = _Client("late", gate)
    monkeypatch.setattr(cli_module, "_create_client", lambda **_: client)
    codes: list[int] = []
    argv = ["build", str(spec), "--output-dir", str(output), "--run-id", "late"]
    argv += ["--warehouse", str(output / "warehouse")]
    build = threading.Thread(target=lambda: codes.append(main(argv)))
    build.start()
    archive = tmp_path / "state.tar"
    backup = ["backup", str(archive), "--output-dir", str(output)]
    backup += ["--warehouse", str(output / "warehouse")]
    try:
        assert client.started.wait(timeout=30)

        assert main(backup) == 1
        assert "is in use" in capsys.readouterr().err
        assert not archive.exists()
    finally:
        gate.set()
        build.join(timeout=60)
    assert codes == [0]

    assert main(backup) == 0
    with tarfile.open(archive) as tar:
        assert "output/late/manifest.json" in tar.getnames()
    assert state_backup.verify(archive) == []


def test_a_build_does_not_start_while_a_backup_holds_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "data"
    output.mkdir()
    spec = tmp_path / "spec.yaml"
    spec.write_text(_SPEC_YAML, encoding="utf-8")
    monkeypatch.setattr(cli_module, "_create_client", lambda **_: _Client("x"))

    with state_lock([output], exclusive=True, purpose="the backup"):
        code = main(["build", str(spec), "--output-dir", str(output), "--run-id", "blocked"])

    assert code == 1
    assert "being backed up or restored" in capsys.readouterr().err
    assert not (output / "blocked").exists()


def test_the_lock_is_released_when_its_holder_dies(tmp_path: Path) -> None:
    output = tmp_path / "data"
    output.mkdir()
    script = (
        "import os, sys; from pathlib import Path;"
        "from kpubdata_builder.store.state_lock import state_lock;"
        "cm = state_lock([Path(sys.argv[1])], exclusive=False, purpose='serve');"
        "cm.__enter__(); os._exit(9)"
    )

    killed = subprocess.run([sys.executable, "-c", script, str(output)], check=False, timeout=60)

    assert killed.returncode == 9
    with state_lock([output], exclusive=True, purpose="the backup"):
        pass


def test_backup_leaves_out_what_the_inventory_says_and_reports_it(
    state: tuple[Path, Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output, _, _ = state
    (output / ".readyz-abc").write_bytes(b"")
    archive = tmp_path / "second.tar"

    assert (
        main(
            [
                "backup",
                str(archive),
                "--output-dir",
                str(output),
                "--warehouse",
                str(output / "warehouse"),
            ]
        )
        == 0
    )

    printed = capsys.readouterr().out
    assert "  - .readyz-abc" in printed
    with tarfile.open(archive) as tar:
        assert not any(name.endswith(".readyz-abc") for name in tar.getnames())
        assert not any(name.endswith(LOCK_FILENAME) for name in tar.getnames())


def test_environment_is_not_read_for_the_master_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The key may be set where the backup runs; it is never printed or archived."""
    output = tmp_path / "data"
    _populate(output)
    monkeypatch.setenv("KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY", _MASTER_KEY)
    archive = tmp_path / "state.tar"

    argv = ["backup", str(archive), "--output-dir", str(output)]
    assert main([*argv, "--warehouse", str(output / "warehouse")]) == 0

    captured = capsys.readouterr()
    assert _MASTER_KEY not in captured.out + captured.err
    assert _MASTER_KEY.encode() not in archive.read_bytes()
    assert os.environ["KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY"] == _MASTER_KEY
