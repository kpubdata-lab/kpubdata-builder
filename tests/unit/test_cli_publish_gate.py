"""Check if CLI publish follows same policy as service publish (#491 follow-up).

Two issues existed. CLI called only ``validate_spec(spec)`` skipping
publication-only rules (license declaration), and used ``rglob("*")`` to
upload **all** files under artifacts_dir — passing run root as artifacts_dir
would publish bronze originals and BuildSpec snapshot together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.cli import _is_non_publishable


class TestOnlyDatasetArtifactsArePublished:
    def _paths(self, root: Path) -> tuple[list[str], list[str]]:
        published: list[str] = []
        skipped: list[str] = []
        for relative in (
            "gold/datago.x/table.parquet",
            "gold/datago.x/hf/README.md",
            "out/data.jsonl",
            "bronze/datago.x/raw.jsonl",
            "silver/datago.x/table.parquet",
            "manifest.json",
            "buildspec.yaml",
        ):
            target = skipped if _is_non_publishable(root / relative, root) else published
            target.append(relative)
        return published, skipped

    def test_gold_and_exports_are_published(self, tmp_path: Path) -> None:
        published, _ = self._paths(tmp_path)

        assert published == [
            "gold/datago.x/table.parquet",
            "gold/datago.x/hf/README.md",
            "out/data.jsonl",
        ]

    def test_source_layers_and_workspace_files_are_not(self, tmp_path: Path) -> None:
        """bronze original is not publish target and size not comparable to gold."""
        _, skipped = self._paths(tmp_path)

        assert skipped == [
            "bronze/datago.x/raw.jsonl",
            "silver/datago.x/table.parquet",
            "manifest.json",
            "buildspec.yaml",
        ]

    def test_a_directory_named_like_a_layer_deeper_down_is_kept(self, tmp_path: Path) -> None:
        """Only look at top-level directories — don't block 'bronze' name inside gold."""
        assert not _is_non_publishable(tmp_path / "gold" / "bronze" / "x.parquet", tmp_path)


class TestTheLicenseGateApplies:
    """spec blocked by HTTP publish could be uploaded via CLI."""

    _NO_LICENSE = (
        "dataset_id: dataset.sample\n"
        "title: Sample\n"
        "description: D\n"
        "sources:\n"
        "  - provider: datago\n"
        "    dataset: air_quality\n"
        "exports:\n"
        "  - kind: jsonl\n"
        "    output_path: out/data.jsonl\n"
    )

    def test_publishing_without_a_license_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from kpubdata_builder.cli import main

        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text(self._NO_LICENSE, encoding="utf-8")
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "data.jsonl").write_text("{}\n", encoding="utf-8")

        exit_code = main(
            [
                "publish",
                str(spec_path),
                "--target",
                "local",
                "--destination",
                str(tmp_path / "dest"),
                "--artifacts-dir",
                str(artifacts),
            ]
        )

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "license is required when publish=true" in captured.err
