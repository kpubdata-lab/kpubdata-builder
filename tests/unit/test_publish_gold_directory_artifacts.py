"""Publish preparation when Gold artifact is directory (#491 follow-up).

``kind: huggingface`` export creates layout directory (README.md + data/), not single file;
manifest output_path points to that directory. But ``resolve_gold_artifacts`` checked only
with ``is_file()``, so normally completed build was blocked as ``artifact_missing`` —
Studio's "huggingface export → publish" flow entirely impossible.

Workaround makes it worse: uploading gold files individually uploads gold README without
YAML front matter, losing license meta in HF card. Proper card exists only inside HF export layout.
"""

from __future__ import annotations

from pathlib import Path

from kpubdata_builder.service.publish import (
    PublishIssue,
    ResolvedArtifacts,
    resolve_gold_artifacts,
)

_RUN = "run-hf"
_SOURCE = "datago.sample"


def _manifest(*output_paths: Path) -> dict[str, object]:
    return {
        "inputs": [_SOURCE],
        "outputs": [str(path) for path in output_paths],
    }


def _gold_dir(tmp_path: Path) -> Path:
    gold = tmp_path / _RUN / "gold" / _SOURCE
    gold.mkdir(parents=True)
    return gold


class TestDirectoryGoldArtifacts:
    def test_a_huggingface_layout_directory_resolves(self, tmp_path: Path) -> None:
        gold = _gold_dir(tmp_path)
        layout = gold / "hf"
        (layout / "data").mkdir(parents=True)
        (layout / "README.md").write_text("---\nlicense: cc-by-4.0\n---\n", encoding="utf-8")
        (layout / "data" / "train.parquet").write_bytes(b"parquet")

        resolved = resolve_gold_artifacts(tmp_path, _RUN, _manifest(layout))

        assert isinstance(resolved, ResolvedArtifacts), resolved
        assert resolved.paths == (layout,)

    def test_files_and_directories_can_be_mixed(self, tmp_path: Path) -> None:
        gold = _gold_dir(tmp_path)
        layout = gold / "hf"
        layout.mkdir()
        (layout / "README.md").write_text("x", encoding="utf-8")
        single = gold / "data.jsonl"
        single.write_text("{}\n", encoding="utf-8")

        resolved = resolve_gold_artifacts(tmp_path, _RUN, _manifest(layout, single))

        assert isinstance(resolved, ResolvedArtifacts), resolved
        assert set(resolved.paths) == {layout, single}

    def test_a_path_that_exists_as_neither_is_still_missing(self, tmp_path: Path) -> None:
        """Non-existent path must still fail-closed."""
        gold = _gold_dir(tmp_path)
        ghost = gold / "never-written"

        issue = resolve_gold_artifacts(tmp_path, _RUN, _manifest(ghost))

        assert isinstance(issue, PublishIssue)
        assert issue.code == "artifact_missing"
