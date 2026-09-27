"""Verify atomic directory replacement utility atomic_replace_dir behavior (#180)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.stages._atomic import atomic_replace_dir


def _make_dir(parent: Path, name: str, content: str) -> Path:
    d = parent / name
    d.mkdir()
    _ = (d / "data.txt").write_text(content, encoding="utf-8")
    return d


def test_replaces_into_empty_destination(tmp_path: Path) -> None:
    tmp_dir = _make_dir(tmp_path, ".tmp_new", "new")
    final_dir = tmp_path / "final"

    atomic_replace_dir(tmp_dir, final_dir)

    assert (final_dir / "data.txt").read_text(encoding="utf-8") == "new"
    assert not tmp_dir.exists()


def test_swaps_over_existing_destination(tmp_path: Path) -> None:
    final_dir = _make_dir(tmp_path, "final", "old")
    tmp_dir = _make_dir(tmp_path, ".tmp_new", "new")

    atomic_replace_dir(tmp_dir, final_dir)

    assert (final_dir / "data.txt").read_text(encoding="utf-8") == "new"
    assert not tmp_dir.exists()
    # Backup (.old) must not remain.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".old")]
    assert leftovers == []


def test_restores_existing_data_when_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If new batch (rename) fails, existing data must not be lost and must be recoverable.
    final_dir = _make_dir(tmp_path, "final", "old")
    tmp_dir = _make_dir(tmp_path, ".tmp_new", "new")

    original_rename = Path.rename
    state = {"calls": 0}

    def flaky_rename(self: Path, target: Path) -> Path:
        # First rename (existing→backup) passes, second rename (tmp→final) fails.
        state["calls"] += 1
        if state["calls"] == 2:
            raise OSError("disk full")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(OSError, match="disk full"):
        atomic_replace_dir(tmp_dir, final_dir)

    monkeypatch.undo()
    # Existing data must be recovered in place.
    assert final_dir.exists()
    assert (final_dir / "data.txt").read_text(encoding="utf-8") == "old"


def test_restores_backup_even_when_final_dir_partially_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even if final_dir exists after tmp→final rename fails,
    # backup must be recovered (#224).
    # In real OS, final_dir doesn't appear after rename failure, but
    # verify defensive code is correct.
    final_dir = _make_dir(tmp_path, "final", "old")
    tmp_dir = _make_dir(tmp_path, ".tmp_new", "new")

    original_rename = Path.rename
    state = {"calls": 0}

    def flaky_rename(self: Path, target: Path) -> Path:
        state["calls"] += 1
        if state["calls"] == 2:
            # Before simulating rename failure, create final_dir as empty directory
            # to recreate "partial batch" situation.
            target.mkdir(exist_ok=True)
            raise OSError("partial rename failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(OSError, match="partial rename failure"):
        atomic_replace_dir(tmp_dir, final_dir)

    monkeypatch.undo()
    # final_dir must contain original data recovered from backup.
    assert final_dir.exists()
    assert (final_dir / "data.txt").read_text(encoding="utf-8") == "old"
