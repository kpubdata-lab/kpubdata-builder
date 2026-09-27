"""Publisher boundary (#28): verify PublishResult contract and LocalPublisher registration
behavior.
"""

from __future__ import annotations

import sys
import types
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


def _install_fake_hf(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, object]]]:
    """Inject huggingface_hub.HfApi as fake and record upload calls."""
    calls: dict[str, list[dict[str, object]]] = {"repos": [], "files": [], "folders": []}

    class FakeHfApi:
        def __init__(self, token: str | None = None) -> None:
            self._token = token

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

        def upload_folder(
            self, *, folder_path: str, repo_id: str, repo_type: str, commit_message: str
        ) -> None:
            calls["folders"].append({"folder_path": folder_path})

    fake_module = types.ModuleType("huggingface_hub")
    fake_module.HfApi = FakeHfApi  # type: ignore[attr-defined]
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

    def test_directory_artifacts_use_upload_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_hf(monkeypatch)
        layout = tmp_path / "dataset"
        layout.mkdir()
        _ = (layout / "x.parquet").write_text("x", encoding="utf-8")

        result = HuggingFacePublisher().publish((layout,), destination="org/ds")

        assert len(calls["folders"]) == 1
        assert calls["files"] == []
        assert result.artifact_count == 1

    def test_unrelated_absolute_paths_fall_back_to_basename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unrelated absolute paths with common root "/" must not leak host paths and
        # must fall back to basename (#205).
        calls = _install_fake_hf(monkeypatch)

        if sys.platform == "win32":
            paths = (Path("C:/tmp/a/f1.parquet"), Path("C:/var/b/f2.parquet"))
        else:
            paths = (Path("/tmp/a/f1.parquet"), Path("/var/b/f2.parquet"))

        result = HuggingFacePublisher().publish(paths, destination="org/ds")

        repo_paths = {c["path_in_repo"] for c in calls["files"]}
        assert repo_paths == {"f1.parquet", "f2.parquet"}
        assert result.artifact_count == 2

    def test_relative_paths_preserve_layout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # commonpath of relative-path artifacts is Path("."), which is parent==self but
        # not absolute, so not a basename fallback candidate. Directory layout must be preserved
        # (#205).
        calls = _install_fake_hf(monkeypatch)

        result = HuggingFacePublisher().publish(
            (Path("a/f1.parquet"), Path("b/f2.parquet")), destination="org/ds"
        )

        repo_paths = {c["path_in_repo"] for c in calls["files"]}
        assert repo_paths == {"a/f1.parquet", "b/f2.parquet"}
        assert result.artifact_count == 2


class _FakeKaggleApi:
    """Kaggle API double: record calls and simulate dataset existence."""

    def __init__(self, existing: tuple[str, ...] = (), raise_on_create: bool = False) -> None:
        self._existing = existing
        self._raise_on_create = raise_on_create
        self.calls: list[str] = []

    def authenticate(self) -> None:
        self.calls.append("authenticate")

    def dataset_list(self, *, mine: bool, search: str) -> list[str]:
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
            def dataset_list(self, *, mine: bool, search: str) -> list[str]:
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
