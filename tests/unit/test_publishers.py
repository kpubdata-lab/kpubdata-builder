"""Publisher boundary (#28): verify PublishResult contract and LocalPublisher registration
behavior.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from kpubdata_builder.errors import PublishError
from kpubdata_builder.publishers import (
    PUBLISHER_REGISTRY,
    BasePublisher,
    KagglePublisher,
    LocalPublisher,
    PublishResult,
)
from kpubdata_builder.publishers import huggingface as huggingface_module
from kpubdata_builder.publishers.huggingface import HuggingFacePublisher


def _make_artifacts(tmp_path: Path) -> tuple[Path, ...]:
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.md"
    _ = a.write_text("{}\n", encoding="utf-8")
    _ = b.write_text("# title\n", encoding="utf-8")
    return (a, b)


class TestLocalPublisher:
    def test_is_a_base_publisher_named_local(self) -> None:
        publisher = LocalPublisher()
        assert isinstance(publisher, BasePublisher)
        assert publisher.name == "local"

    def test_copies_artifacts_into_destination_and_reports(self, tmp_path: Path) -> None:
        artifacts = _make_artifacts(tmp_path)
        destination = tmp_path / "registry" / "dataset-v1"

        result = LocalPublisher().publish(artifacts, destination=str(destination))

        assert isinstance(result, PublishResult)
        assert result.publisher == "local"
        assert result.status == "ok"
        assert result.artifact_count == 2
        assert (destination / "a.jsonl").exists()
        assert (destination / "b.md").exists()
        assert result.reference == str(destination)

    def test_registered_in_publisher_registry(self) -> None:
        assert "local" in PUBLISHER_REGISTRY
        assert isinstance(PUBLISHER_REGISTRY["local"], LocalPublisher)


def test_publish_result_is_immutable() -> None:
    result = PublishResult(publisher="local", reference="/tmp/x", artifact_count=1)
    with pytest.raises(AttributeError):
        result.publisher = "other"  # type: ignore[misc]


class TestLocalPublisherFailurePolicy:
    def test_rejects_duplicate_basenames(self, tmp_path: Path) -> None:
        # Two same-named files in different directories are rejected in flat copy because one
        # overwrites the other.
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        f1 = d1 / "data.jsonl"
        f2 = d2 / "data.jsonl"
        _ = f1.write_text("{}\n", encoding="utf-8")
        _ = f2.write_text("{}\n", encoding="utf-8")

        with pytest.raises(PublishError, match="duplicate artifact basename"):
            LocalPublisher().publish((f1, f2), destination=str(tmp_path / "registry"))

    def test_rejects_directory_artifacts(self, tmp_path: Path) -> None:
        # Directories are explicitly rejected because shutil.copy2 cannot handle them.
        dir_artifact = tmp_path / "hf_layout"
        dir_artifact.mkdir()

        with pytest.raises(PublishError, match="directory artifacts"):
            LocalPublisher().publish((dir_artifact,), destination=str(tmp_path / "registry"))

    def test_wraps_copy_failures_in_publish_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # shutil.copy2 raising OSError must be wrapped in PublishError and propagated.
        artifact = tmp_path / "data.jsonl"
        _ = artifact.write_text("{}\n", encoding="utf-8")

        def _boom(src: object, dst: object) -> None:
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr("kpubdata_builder.publishers.local.shutil.copy2", _boom)

        with pytest.raises(PublishError, match="failed to copy"):
            LocalPublisher().publish((artifact,), destination=str(tmp_path / "registry"))


class RepositoryNotFoundError(Exception):
    """Stand-in named like huggingface_hub's own; matched by class name."""


class GatedRepoError(RepositoryNotFoundError):
    """Stand-in: huggingface_hub's GatedRepoError subclasses RepositoryNotFoundError."""


_MISSING = object()


class FakeHub:
    """A dataset repo on a fake Hub: its files, its history, and every write call.

    ``create_commit`` is atomic, as the Hub's is: it applies every operation or none.
    ``upload_file`` and ``upload_folder`` each make their own commit, so a run of them
    that dies midway leaves the earlier ones applied. ``fail_on_transfer`` makes the
    Nth file transfer raise, wherever it happens; ``lose_answer`` lets a commit land
    and then raises as a timed-out request would; ``before_commit`` runs just before a
    commit is applied, standing in for another writer.
    """

    def __init__(self) -> None:
        self.remote: dict[str, bytes] = {".gitattributes": b"*.parquet lfs\n"}
        self.history: list[types.SimpleNamespace] = [
            types.SimpleNamespace(commit_id="c0", title="initial commit", message="")
        ]
        self.calls: dict[str, list[dict[str, object]]] = {
            "repos": [],
            "files": [],
            "folders": [],
            "info": [],
            "commits": [],
        }
        self.transfers = 0
        self.fail_on_transfer: int | None = None
        self.lose_answer = False
        self.before_commit: list[Callable[[], None]] = []

    @property
    def head(self) -> str:
        return str(self.history[0].commit_id)

    def _transfer(self, source: str) -> bytes:
        self.transfers += 1
        if self.fail_on_transfer is not None and self.transfers >= self.fail_on_transfer:
            raise ConnectionError("connection reset during upload")
        return Path(source).read_bytes()

    def _commit(self, title: str, message: str, changes: dict[str, bytes | None]) -> None:
        for path, content in changes.items():
            if content is None:
                self.remote.pop(path, None)
            else:
                self.remote[path] = content
        commit_id = f"c{len(self.history)}"
        self.history.insert(
            0, types.SimpleNamespace(commit_id=commit_id, title=title, message=message)
        )

    def other_writer(self, path: str, content: bytes) -> None:
        self._commit("someone else", "", {path: content})


def _install_fake_hf(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: object = _MISSING,
    hub: FakeHub | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Inject huggingface_hub.HfApi as fake and record upload calls.

    ``existing`` is what ``repo_info`` reports: ``_MISSING`` (the default) raises
    RepositoryNotFoundError, an exception instance is raised, and anything else is
    the repo's ``private`` attribute. ``hub`` holds the fake repo's state; a fresh
    one is used when it is not given.
    """
    state = hub if hub is not None else FakeHub()
    calls = state.calls

    class CommitOperationAdd:
        def __init__(self, *, path_in_repo: str, path_or_fileobj: str) -> None:
            self.path_in_repo = path_in_repo
            self.path_or_fileobj = path_or_fileobj

    class CommitOperationDelete:
        def __init__(self, *, path_in_repo: str) -> None:
            self.path_in_repo = path_in_repo

    class FakeHfApi:
        def __init__(self, token: str | None = None) -> None:
            self._token = token

        def repo_info(self, *, repo_id: str, repo_type: str) -> object:
            calls["info"].append({"repo_id": repo_id, "repo_type": repo_type})
            if existing is _MISSING:
                raise RepositoryNotFoundError(repo_id)
            if isinstance(existing, BaseException):
                raise existing
            return types.SimpleNamespace(private=existing)

        def create_repo(
            self,
            *,
            repo_id: str,
            repo_type: str,
            private: bool,
            exist_ok: bool,
        ) -> None:
            calls["repos"].append(
                {
                    "repo_id": repo_id,
                    "repo_type": repo_type,
                    "private": private,
                    "exist_ok": exist_ok,
                }
            )

        def list_repo_commits(self, *, repo_id: str, repo_type: str) -> list[object]:
            assert repo_type == "dataset"
            return list(state.history)

        def list_repo_files(self, *, repo_id: str, repo_type: str, revision: str) -> list[str]:
            assert revision == state.head
            return sorted(state.remote)

        def create_commit(
            self,
            *,
            repo_id: str,
            repo_type: str,
            operations: list[object],
            commit_message: str,
            commit_description: str,
            parent_commit: str,
        ) -> None:
            calls["commits"].append(
                {
                    "operations": operations,
                    "commit_message": commit_message,
                    "commit_description": commit_description,
                    "parent_commit": parent_commit,
                }
            )
            changes: dict[str, bytes | None] = {}
            for op in operations:
                if isinstance(op, CommitOperationAdd):
                    calls["files"].append(
                        {"path_in_repo": op.path_in_repo, "src": op.path_or_fileobj}
                    )
                    changes[op.path_in_repo] = state._transfer(op.path_or_fileobj)
                else:
                    assert isinstance(op, CommitOperationDelete)
                    changes[op.path_in_repo] = None
            for hook in state.before_commit:
                hook()
            if parent_commit != state.head:
                raise RuntimeError("412 Precondition Failed: the parent commit moved")
            state._commit(commit_message, commit_description, changes)
            if state.lose_answer:
                raise TimeoutError("read timed out")

        def upload_file(
            self,
            *,
            path_or_fileobj: str,
            path_in_repo: str,
            repo_id: str,
            repo_type: str,
            commit_message: str,
        ) -> None:
            calls["files"].append({"path_in_repo": path_in_repo, "src": path_or_fileobj})
            content = state._transfer(path_or_fileobj)
            state._commit(commit_message, "", {path_in_repo: content})

        def upload_folder(
            self, *, folder_path: str, repo_id: str, repo_type: str, commit_message: str
        ) -> None:
            calls["folders"].append({"folder_path": folder_path})
            root = Path(folder_path)
            changes: dict[str, bytes | None] = {
                f.relative_to(root).as_posix(): state._transfer(str(f))
                for f in sorted(root.rglob("*"))
                if f.is_file()
            }
            state._commit(commit_message, "", changes)

    fake_module = types.ModuleType("huggingface_hub")
    fake_module.HfApi = FakeHfApi  # type: ignore[attr-defined]
    fake_module.CommitOperationAdd = CommitOperationAdd  # type: ignore[attr-defined]
    fake_module.CommitOperationDelete = CommitOperationDelete  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_module)
    monkeypatch.setenv("HF_TOKEN", "test-token")
    return calls


class TestHuggingFacePublisher:
    def test_requires_hf_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_hf(monkeypatch)
        monkeypatch.delenv("HF_TOKEN", raising=False)
        artifact = tmp_path / "data.parquet"
        _ = artifact.write_text("x", encoding="utf-8")

        with pytest.raises(RuntimeError, match="HF_TOKEN"):
            HuggingFacePublisher().publish((artifact,), destination="org/ds")

    def test_preserves_directory_layout_in_repo_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nested directory shards must not be flattened to bare filenames but preserve relative
        # paths (#170).
        calls = _install_fake_hf(monkeypatch)
        (tmp_path / "data").mkdir()
        readme = tmp_path / "README.md"
        shard = tmp_path / "data" / "train.parquet"
        _ = readme.write_text("# t\n", encoding="utf-8")
        _ = shard.write_text("x", encoding="utf-8")

        result = HuggingFacePublisher().publish((readme, shard), destination="org/ds")

        repo_paths = {c["path_in_repo"] for c in calls["files"]}
        assert repo_paths == {"README.md", "data/train.parquet"}
        assert result.artifact_count == 2

    @pytest.mark.parametrize("private", [True, False])
    def test_create_repo_uses_requested_new_repo_visibility(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        private: bool,
    ) -> None:
        calls = _install_fake_hf(monkeypatch)
        artifact = tmp_path / "data.parquet"
        artifact.write_text("x", encoding="utf-8")

        HuggingFacePublisher().publish((artifact,), destination="org/ds", private=private)

        assert calls["repos"] == [
            {
                "repo_id": "org/ds",
                "repo_type": "dataset",
                "private": private,
                "exist_ok": True,
            }
        ]

    def test_create_repo_defaults_to_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_hf(monkeypatch)
        artifact = tmp_path / "data.parquet"
        artifact.write_text("x", encoding="utf-8")

        HuggingFacePublisher().publish((artifact,), destination="org/ds")

        assert calls["repos"][0]["private"] is True

    def test_same_basename_different_dirs_do_not_collide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even same-named files are separated into different repo paths when directory structure is
        # preserved
        # so silent overwrites do not occur (#170).
        calls = _install_fake_hf(monkeypatch)
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        f1 = d1 / "data.parquet"
        f2 = d2 / "data.parquet"
        _ = f1.write_text("x", encoding="utf-8")
        _ = f2.write_text("y", encoding="utf-8")

        result = HuggingFacePublisher().publish((f1, f2), destination="org/ds")

        repo_paths = {c["path_in_repo"] for c in calls["files"]}
        assert repo_paths == {"a/data.parquet", "b/data.parquet"}
        assert result.artifact_count == 2

    def test_directory_artifacts_land_at_the_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A directory artifact keeps the layout upload_folder gave it: its files at the
        # repo root, nothing under .git or .cache/huggingface (#1330).
        hub = FakeHub()
        calls = _install_fake_hf(monkeypatch, hub=hub)
        layout = tmp_path / "dataset"
        (layout / "data").mkdir(parents=True)
        (layout / ".git").mkdir()
        (layout / ".cache" / "huggingface").mkdir(parents=True)
        _ = (layout / "README.md").write_text("# t\n", encoding="utf-8")
        _ = (layout / "data" / "x.parquet").write_text("x", encoding="utf-8")
        _ = (layout / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        _ = (layout / ".cache" / "huggingface" / "lock").write_text("l", encoding="utf-8")

        result = HuggingFacePublisher().publish((layout,), destination="org/ds")

        assert len(calls["commits"]) == 1
        assert calls["folders"] == []
        assert {c["path_in_repo"] for c in calls["files"]} == {"README.md", "data/x.parquet"}
        assert result.artifact_count == 1

    def test_unrelated_absolute_paths_fall_back_to_basename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unrelated absolute paths with common root "/" must not leak host paths and
        # must fall back to basename (#205). These paths cannot be created under a
        # test's tmp_path, so the files are never read: only their target paths matter.
        hub = FakeHub()
        calls = _install_fake_hf(monkeypatch, hub=hub)
        monkeypatch.setattr(hub, "_transfer", lambda source: b"")
        monkeypatch.setattr(huggingface_module, "_file_sha256", lambda path: path.as_posix())

        if sys.platform == "win32":
            paths = (Path("C:/tmp/a/f1.parquet"), Path("C:/var/b/f2.parquet"))
        else:
            paths = (Path("/tmp/a/f1.parquet"), Path("/var/b/f2.parquet"))

        result = HuggingFacePublisher().publish(paths, destination="org/ds")

        repo_paths = {c["path_in_repo"] for c in calls["files"]}
        assert repo_paths == {"f1.parquet", "f2.parquet"}
        assert result.artifact_count == 2

    def test_relative_paths_preserve_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # commonpath of relative-path artifacts is Path("."), which is parent==self but
        # not absolute, so not a basename fallback candidate. Directory layout must be preserved
        # (#205).
        calls = _install_fake_hf(monkeypatch)
        monkeypatch.chdir(tmp_path)
        for name in ("a/f1.parquet", "b/f2.parquet"):
            Path(name).parent.mkdir()
            _ = Path(name).write_text("x", encoding="utf-8")

        result = HuggingFacePublisher().publish(
            (Path("a/f1.parquet"), Path("b/f2.parquet")), destination="org/ds"
        )

        repo_paths = {c["path_in_repo"] for c in calls["files"]}
        assert repo_paths == {"a/f1.parquet", "b/f2.parquet"}
        assert result.artifact_count == 2


class TestHuggingFaceAtomicCommit:
    """One completed artifact set is one Hub commit: all of it or none of it (#1330)."""

    @staticmethod
    def _artifact_set(tmp_path: Path, version: str) -> tuple[Path, ...]:
        (tmp_path / "data").mkdir(exist_ok=True)
        readme = tmp_path / "README.md"
        train = tmp_path / "data" / "train.parquet"
        test = tmp_path / "data" / "test.parquet"
        _ = readme.write_text(f"# card {version}\n", encoding="utf-8")
        _ = train.write_text(f"train {version}", encoding="utf-8")
        _ = test.write_text(f"test {version}", encoding="utf-8")
        return (readme, train, test)

    @staticmethod
    def _published_v1(hub: FakeHub) -> dict[str, bytes]:
        hub.remote.update(
            {
                "README.md": b"# card v1\n",
                "data/train.parquet": b"train v1",
                "data/test.parquet": b"test v1",
            }
        )
        hub.other_writer("README.md", b"# card v1\n")
        return dict(hub.remote)

    def test_the_whole_set_is_one_commit_on_the_current_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        calls = _install_fake_hf(monkeypatch, hub=hub)
        parent = hub.head

        result = HuggingFacePublisher().publish(
            self._artifact_set(tmp_path, "v2"), destination="org/ds"
        )

        assert len(calls["commits"]) == 1
        commit = calls["commits"][0]
        assert commit["parent_commit"] == parent
        assert {c["path_in_repo"] for c in calls["files"]} == {
            "README.md",
            "data/train.parquet",
            "data/test.parquet",
        }
        assert hub.remote["data/train.parquet"] == b"train v2"
        assert result.artifact_count == 3

    def test_a_failure_midway_leaves_the_previous_revision_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        before = self._published_v1(hub)
        head = hub.head
        _install_fake_hf(monkeypatch, hub=hub)
        hub.fail_on_transfer = 2

        with pytest.raises(ConnectionError):
            HuggingFacePublisher().publish(self._artifact_set(tmp_path, "v2"), destination="org/ds")

        # Readers still see v1 exactly: no v2 card over v1 shards, no new revision.
        assert hub.remote == before
        assert hub.head == head

    def test_a_retry_after_a_lost_answer_does_not_commit_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        calls = _install_fake_hf(monkeypatch, hub=hub)
        artifacts = self._artifact_set(tmp_path, "v2")
        hub.lose_answer = True

        first = HuggingFacePublisher().publish(artifacts, destination="org/ds")
        hub.lose_answer = False
        second = HuggingFacePublisher().publish(artifacts, destination="org/ds")

        # The commit landed although its answer was lost; the head says so, so the
        # first call reports success and the retry makes no second commit.
        assert first.status == "ok" and second.status == "ok"
        assert len(calls["commits"]) == 1
        assert len(hub.history) == 2

    def test_a_changed_set_is_committed_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        calls = _install_fake_hf(monkeypatch, hub=hub)

        HuggingFacePublisher().publish(self._artifact_set(tmp_path, "v2"), destination="org/ds")
        HuggingFacePublisher().publish(self._artifact_set(tmp_path, "v3"), destination="org/ds")

        assert len(calls["commits"]) == 2
        assert hub.remote["README.md"] == b"# card v3\n"

    def test_a_failure_without_the_set_on_the_head_is_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        _install_fake_hf(monkeypatch, hub=hub)
        hub.fail_on_transfer = 1

        with pytest.raises(ConnectionError):
            HuggingFacePublisher().publish(self._artifact_set(tmp_path, "v2"), destination="org/ds")

    def test_a_commit_made_in_between_is_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        _install_fake_hf(monkeypatch, hub=hub)
        hub.before_commit.append(lambda: hub.other_writer("README.md", b"# theirs\n"))

        with pytest.raises(RuntimeError, match="parent commit moved"):
            HuggingFacePublisher().publish(self._artifact_set(tmp_path, "v2"), destination="org/ds")

        assert hub.remote["README.md"] == b"# theirs\n"
        assert "data/train.parquet" not in hub.remote

    def test_nothing_is_deleted_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        hub.remote["data/old.parquet"] = b"old"
        calls = _install_fake_hf(monkeypatch, hub=hub)

        HuggingFacePublisher().publish(self._artifact_set(tmp_path, "v2"), destination="org/ds")

        assert hub.remote["data/old.parquet"] == b"old"
        assert all(hasattr(op, "path_or_fileobj") for op in calls["commits"][0]["operations"])

    def test_stale_files_are_deleted_in_the_same_commit_when_asked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        hub.remote.update({"data/old.parquet": b"old", "NOTES.txt": b"keep"})
        calls = _install_fake_hf(monkeypatch, hub=hub)

        HuggingFacePublisher().publish(
            self._artifact_set(tmp_path, "v2"),
            destination="org/ds",
            delete_patterns=("data/*", "README.md"),
        )

        assert len(calls["commits"]) == 1
        deleted = [
            op.path_in_repo
            for op in calls["commits"][0]["operations"]
            if not hasattr(op, "path_or_fileobj")
        ]
        assert deleted == ["data/old.parquet"]
        assert set(hub.remote) == {
            ".gitattributes",
            "NOTES.txt",
            "README.md",
            "data/test.parquet",
            "data/train.parquet",
        }

    def test_the_token_is_not_in_the_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hub = FakeHub()
        calls = _install_fake_hf(monkeypatch, hub=hub)

        HuggingFacePublisher().publish(
            self._artifact_set(tmp_path, "v2"),
            destination="org/ds",
            credentials={"HF_TOKEN": "hf_secret_value"},
        )

        assert "hf_secret_value" not in repr(calls["commits"])
        assert "hf_secret_value" not in repr(hub.history)


class TestHuggingFacePrivateTarget:
    """A private publish never lands in an existing public repo (#901)."""

    @staticmethod
    def _artifact(tmp_path: Path) -> Path:
        artifact = tmp_path / "data.parquet"
        artifact.write_text("x", encoding="utf-8")
        return artifact

    @staticmethod
    def _nothing_written(calls: dict[str, list[dict[str, object]]]) -> bool:
        return calls["repos"] == [] and calls["files"] == [] and calls["folders"] == []

    def test_existing_public_repo_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_hf(monkeypatch, existing=False)

        with pytest.raises(PublishError, match="already exists and is public"):
            HuggingFacePublisher().publish(
                (self._artifact(tmp_path),), destination="org/ds", private=True
            )

        assert calls["info"] == [{"repo_id": "org/ds", "repo_type": "dataset"}]
        assert self._nothing_written(calls)

    def test_default_publish_is_private_and_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_hf(monkeypatch, existing=False)

        with pytest.raises(PublishError, match="is public"):
            HuggingFacePublisher().publish((self._artifact(tmp_path),), destination="org/ds")

        assert self._nothing_written(calls)

    @pytest.mark.parametrize(
        "failure",
        [
            ConnectionError("network down"),
            PermissionError("401 unauthorized"),
            GatedRepoError("gated"),
        ],
        ids=["network", "auth", "gated"],
    )
    def test_lookup_failure_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        calls = _install_fake_hf(monkeypatch, existing=failure)

        with pytest.raises(PublishError, match="could not look up"):
            HuggingFacePublisher().publish(
                (self._artifact(tmp_path),), destination="org/ds", private=True
            )

        assert self._nothing_written(calls)

    @pytest.mark.parametrize("reported", [None, "yes"], ids=["missing", "not-a-bool"])
    def test_unreported_visibility_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported: object
    ) -> None:
        calls = _install_fake_hf(monkeypatch, existing=reported)

        with pytest.raises(PublishError, match="could not be determined"):
            HuggingFacePublisher().publish(
                (self._artifact(tmp_path),), destination="org/ds", private=True
            )

        assert self._nothing_written(calls)

    def test_existing_private_repo_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_hf(monkeypatch, existing=True)

        result = HuggingFacePublisher().publish(
            (self._artifact(tmp_path),), destination="org/ds", private=True
        )

        assert result.artifact_count == 1
        assert [c["path_in_repo"] for c in calls["files"]] == ["data.parquet"]

    def test_missing_repo_is_created_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_hf(monkeypatch)

        HuggingFacePublisher().publish(
            (self._artifact(tmp_path),), destination="org/ds", private=True
        )

        assert calls["info"] == [{"repo_id": "org/ds", "repo_type": "dataset"}]
        assert calls["repos"][0]["private"] is True
        assert len(calls["files"]) == 1

    def test_public_publish_does_not_look_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A public publish exposes nothing by landing in a private repo.
        calls = _install_fake_hf(monkeypatch, existing=ConnectionError("unused"))

        HuggingFacePublisher().publish(
            (self._artifact(tmp_path),), destination="org/ds", private=False
        )

        assert calls["info"] == []
        assert len(calls["files"]) == 1


class _KaggleDataset:
    """Stand-in for a ``dataset_list`` entry: ``str()`` is the ref, as in the SDK."""

    def __init__(self, ref: str, **visibility: object) -> None:
        self.ref = ref
        for attribute, value in visibility.items():
            setattr(self, attribute, value)

    def __str__(self) -> str:
        return self.ref


class _FakeKaggleApi:
    """Kaggle API double: record calls and simulate dataset existence.

    ``existing`` entries given as plain refs are reported private (kaggle 1.7's
    ``is_private``); pass ``_KaggleDataset`` entries to control visibility.
    """

    def __init__(
        self, existing: tuple[str | _KaggleDataset, ...] = (), raise_on_create: bool = False
    ) -> None:
        self._existing = tuple(
            _KaggleDataset(d, is_private=True) if isinstance(d, str) else d for d in existing
        )
        self._raise_on_create = raise_on_create
        self.calls: list[str] = []

    def authenticate(self) -> None:
        self.calls.append("authenticate")

    def dataset_list(self, *, mine: bool, search: str) -> list[_KaggleDataset]:
        del mine, search
        return list(self._existing)

    def dataset_create_version(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        if self._raise_on_create:
            raise RuntimeError("kaggle boom")
        self.calls.append("dataset_create_version")

    def dataset_create_new(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        if self._raise_on_create:
            raise RuntimeError("kaggle boom")
        self.calls.append("dataset_create_new")


def _make_kaggle_dir(tmp_path: Path, dataset_id: str) -> Path:
    """Create Kaggle upload directory with dataset-metadata.json and data files."""
    import json

    artifact_dir = tmp_path / "dataset"
    artifact_dir.mkdir()
    _ = (artifact_dir / "data.csv").write_text("id\n1\n", encoding="utf-8")
    _ = (artifact_dir / "dataset-metadata.json").write_text(
        json.dumps({"id": dataset_id, "title": "x", "resources": []}),
        encoding="utf-8",
    )
    return artifact_dir


def _inject_fake_kaggle(monkeypatch: pytest.MonkeyPatch, api: _FakeKaggleApi) -> None:
    """Inject `kaggle.api.kaggle_api_extended.KaggleApi` as fake module."""
    extended = types.ModuleType("kaggle.api.kaggle_api_extended")
    extended.KaggleApi = lambda: api  # type: ignore[attr-defined]
    api_pkg = types.ModuleType("kaggle.api")
    kaggle_pkg = types.ModuleType("kaggle")
    monkeypatch.setitem(sys.modules, "kaggle", kaggle_pkg)
    monkeypatch.setitem(sys.modules, "kaggle.api", api_pkg)
    monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", extended)


class TestKagglePublisher:
    def test_registered_in_publisher_registry(self) -> None:
        assert "kaggle" in PUBLISHER_REGISTRY
        assert isinstance(PUBLISHER_REGISTRY["kaggle"], KagglePublisher)

    def test_publish_new_dataset_calls_create_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeKaggleApi(existing=())
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/new")

        result = KagglePublisher().publish((artifact_dir,), destination="kpub/new")

        assert "dataset_create_new" in api.calls
        assert "dataset_create_version" not in api.calls
        assert result.artifact_count == 1

    def test_publish_existing_dataset_calls_create_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeKaggleApi(existing=("kpub/existing",))
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/existing")

        result = KagglePublisher().publish((artifact_dir,), destination="kpub/existing")

        assert "dataset_create_version" in api.calls
        assert "dataset_create_new" not in api.calls
        assert result.artifact_count == 1

    def test_rejects_non_directory_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeKaggleApi()
        _inject_fake_kaggle(monkeypatch, api)
        artifact = tmp_path / "data.csv"
        _ = artifact.write_text("id\n1\n", encoding="utf-8")

        with pytest.raises(PublishError, match="expects a directory"):
            KagglePublisher().publish((artifact,), destination="kpub/x")

    def test_missing_kaggle_package_raises_runtime_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only kaggle import is blocked with ImportError. sys.modules monkeypatch is spurious in
        # environments
        # where kaggle is installed, so __import__ is intercepted directly (#181).
        import builtins

        real_import = builtins.__import__

        def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "kaggle" or name.startswith("kaggle."):
                raise ImportError("No module named 'kaggle'")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", _blocked_import)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/x")

        with pytest.raises(RuntimeError, match="kaggle is required"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/x")

    def test_rejects_metadata_id_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If dataset-metadata.json id differs from destination, prevent incorrect target upload by
        # raising PublishError (#177).
        api = _FakeKaggleApi(existing=())
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/declared")

        with pytest.raises(PublishError, match="does not match destination"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/different")
        assert "dataset_create_new" not in api.calls

    def test_rejects_missing_metadata_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Directory without dataset-metadata.json is rejected (#181 happy path regression).
        api = _FakeKaggleApi(existing=())
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = tmp_path / "dataset"
        artifact_dir.mkdir()

        with pytest.raises(PublishError, match="requires dataset-metadata.json"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/x")

    def test_authentication_failure_wrapped_in_publish_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # authenticate() exception must be converted to PublishError instead of raw traceback
        # (#178).
        class _AuthFailApi(_FakeKaggleApi):
            def authenticate(self) -> None:
                raise OSError("kaggle.json not found")

        api = _AuthFailApi()
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/x")

        with pytest.raises(PublishError, match="Kaggle authentication failed"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/x")

    def test_dataset_list_failure_wrapped_in_publish_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # dataset_list failure is swallowed and propagated without creating new (public) dataset
        # (#177).
        class _ListFailApi(_FakeKaggleApi):
            def dataset_list(self, *, mine: bool, search: str) -> list[_KaggleDataset]:
                del mine, search
                raise ConnectionError("network down")

        api = _ListFailApi()
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/x")

        with pytest.raises(PublishError, match="Failed to query existing Kaggle"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/x")
        assert "dataset_create_new" not in api.calls

    def test_new_dataset_defaults_to_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If public argument is not given, new dataset must be created private (#177).
        class _RecordingApi(_FakeKaggleApi):
            def __init__(self) -> None:
                super().__init__(existing=())
                self.public_arg: bool | None = None

            def dataset_create_new(self, *args: object, **kwargs: object) -> None:
                self.public_arg = bool(kwargs.get("public"))
                self.calls.append("dataset_create_new")

        api = _RecordingApi()
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/new")

        KagglePublisher().publish((artifact_dir,), destination="kpub/new")
        assert api.public_arg is False

        KagglePublisher().publish((artifact_dir,), destination="kpub/new", public=True)
        assert api.public_arg is True

    def test_wraps_api_failure_in_publish_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeKaggleApi(existing=(), raise_on_create=True)
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/new")

        with pytest.raises(PublishError, match="Failed to publish Kaggle dataset"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/new")


class TestKagglePrivateTarget:
    """A private publish never becomes a new version of a public dataset (#901)."""

    @staticmethod
    def _uploaded(api: _FakeKaggleApi) -> bool:
        return "dataset_create_version" in api.calls or "dataset_create_new" in api.calls

    @pytest.mark.parametrize(
        "dataset",
        [
            _KaggleDataset("kpub/existing", is_private=False),
            _KaggleDataset("kpub/existing", isPrivate=False),
        ],
        ids=["kaggle-1.7", "kaggle-1.6"],
    )
    def test_existing_public_dataset_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dataset: _KaggleDataset
    ) -> None:
        api = _FakeKaggleApi(existing=(dataset,))
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/existing")

        with pytest.raises(PublishError, match="already exists and is public"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/existing")

        assert not self._uploaded(api)

    @pytest.mark.parametrize(
        "dataset",
        [
            _KaggleDataset("kpub/existing"),
            _KaggleDataset("kpub/existing", is_private=None),
            _KaggleDataset("kpub/existing", isPrivate="false"),
        ],
        ids=["absent", "none", "not-a-bool"],
    )
    def test_unknown_visibility_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dataset: _KaggleDataset
    ) -> None:
        api = _FakeKaggleApi(existing=(dataset,))
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/existing")

        with pytest.raises(PublishError, match="could not be determined"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/existing")

        assert not self._uploaded(api)

    def test_lookup_failure_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _ListFailApi(_FakeKaggleApi):
            def dataset_list(self, *, mine: bool, search: str) -> list[_KaggleDataset]:
                del mine, search
                raise PermissionError("403 forbidden")

        api = _ListFailApi()
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/existing")

        with pytest.raises(PublishError, match="Failed to query existing Kaggle"):
            KagglePublisher().publish((artifact_dir,), destination="kpub/existing")

        assert not self._uploaded(api)

    @pytest.mark.parametrize("attribute", ["is_private", "isPrivate"])
    def test_existing_private_dataset_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attribute: str
    ) -> None:
        dataset = _KaggleDataset("kpub/existing", **{attribute: True})
        api = _FakeKaggleApi(existing=(dataset,))
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/existing")

        KagglePublisher().publish((artifact_dir,), destination="kpub/existing")

        assert "dataset_create_version" in api.calls

    def test_missing_dataset_is_created_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_args: list[object] = []

        class _RecordingApi(_FakeKaggleApi):
            def dataset_create_new(self, *args: object, **kwargs: object) -> None:
                del args
                public_args.append(kwargs.get("public"))
                self.calls.append("dataset_create_new")

        # A public dataset with another ref is not the target.
        api = _RecordingApi(existing=(_KaggleDataset("kpub/other", is_private=False),))
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/new")

        KagglePublisher().publish((artifact_dir,), destination="kpub/new")

        assert public_args == [False]

    def test_public_publish_to_existing_public_dataset_is_not_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeKaggleApi(existing=(_KaggleDataset("kpub/existing"),))
        _inject_fake_kaggle(monkeypatch, api)
        artifact_dir = _make_kaggle_dir(tmp_path, "kpub/existing")

        KagglePublisher().publish((artifact_dir,), destination="kpub/existing", public=True)

        assert "dataset_create_version" in api.calls


# ------------------------------------------------------------------ destination visibility (#688)


class _HubRepo:
    def __init__(self, private: bool) -> None:
        self.private = private


def _inject_fake_hub(monkeypatch: pytest.MonkeyPatch, repos: dict[str, bool]) -> list[object]:
    tokens: list[object] = []

    class RepositoryNotFoundError(Exception):
        pass

    class HfApi:
        def __init__(self, token: object = None) -> None:
            tokens.append(token)

        def repo_info(self, repo_id: str, *, repo_type: str) -> _HubRepo:
            assert repo_type == "dataset"
            if repo_id not in repos:
                raise RepositoryNotFoundError(repo_id)
            return _HubRepo(repos[repo_id])

    hub = types.ModuleType("huggingface_hub")
    hub.HfApi = HfApi  # type: ignore[attr-defined]
    utils = types.ModuleType("huggingface_hub.utils")
    utils.RepositoryNotFoundError = RepositoryNotFoundError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", utils)
    return tokens


@pytest.mark.parametrize(
    ("destination", "visibility"),
    [("kpub/open", "public"), ("kpub/closed", "private"), ("kpub/new", "absent")],
)
def test_huggingface_reads_the_repo_visibility(
    monkeypatch: pytest.MonkeyPatch, destination: str, visibility: str
) -> None:
    tokens = _inject_fake_hub(monkeypatch, {"kpub/open": False, "kpub/closed": True})

    result = HuggingFacePublisher().destination_visibility(
        destination, credentials={"HF_TOKEN": "hf_caller"}
    )

    assert result == visibility
    # Read as the caller whose publish it is, not the server account.
    assert tokens == ["hf_caller"]


@pytest.mark.parametrize(
    ("destination", "visibility"),
    [("kpub/open", "public"), ("kpub/closed", "private"), ("kpub/new", "absent")],
)
def test_kaggle_reads_the_dataset_visibility(
    monkeypatch: pytest.MonkeyPatch, destination: str, visibility: str
) -> None:
    api = _FakeKaggleApi(
        existing=(
            _KaggleDataset("kpub/open", isPrivate=False),
            _KaggleDataset("kpub/closed", is_private=True),
        )
    )
    _inject_fake_kaggle(monkeypatch, api)

    assert KagglePublisher().destination_visibility(destination, credentials={}) == visibility


def test_kaggle_that_does_not_say_is_not_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative: a dataset listed without its visibility raises — never guessed private."""
    _inject_fake_kaggle(monkeypatch, _FakeKaggleApi(existing=(_KaggleDataset("kpub/x"),)))

    with pytest.raises(PublishError):
        KagglePublisher().destination_visibility("kpub/x", credentials={})


def test_a_local_destination_is_private() -> None:
    assert LocalPublisher().destination_visibility("anything") == "private"
