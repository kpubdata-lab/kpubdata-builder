"""Verify shared path safety utility (stages/_path_safety) (#46/#47 review)."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from kpubdata_builder.errors import PathTraversalError
from kpubdata_builder.stages._path_safety import (
    _strip_windows_extended_prefix,
    ensure_within,
    safe_output_path,
    validate_path_segment,
)

from .conftest import requires_symlinks


class TestStripWindowsExtendedPrefix:
    """Windows ``\\\\?\\`` extended path prefix removal (#506).

    In multi-source parallel build (#247), ``Path.resolve()`` attaches to only one of root/target
    (depending on handle open success)
    ``ensure_within`` occasionally raised false-positive traversal alarms. Being pure string
    function, test platform-independently.
    """

    def test_strips_extended_prefix(self) -> None:
        result = _strip_windows_extended_prefix(Path("\\\\?\\C:\\a\\b"))
        assert str(result) == "C:\\a\\b"

    def test_strips_unc_extended_prefix(self) -> None:
        result = _strip_windows_extended_prefix(Path("\\\\?\\UNC\\server\\share"))
        # Bare UNC share root is treated by pathlib as anchor with trailing backslash
        # appended when stringified (same convention as drive root "C:\\") — this anchor
        # normalization happens only in Windows pathlib, so to validate equivalently in POSIX CI
        # we compare using PureWindowsPath.
        assert str(PureWindowsPath(str(result))) == "\\\\server\\share\\"

    def test_noop_without_prefix(self) -> None:
        plain = Path("C:\\a\\b")
        assert _strip_windows_extended_prefix(plain) == plain

    def test_noop_for_posix_path(self) -> None:
        plain = Path("/a/b")
        assert _strip_windows_extended_prefix(plain) == plain


class TestValidatePathSegment:
    @pytest.mark.parametrize("value", ["run1", "datago.apt_trade", "a_b-c.1"])
    def test_accepts_safe_segments(self, value: str) -> None:
        validate_path_segment(value, field_name="seg")  # No exception.

    @pytest.mark.parametrize("value", ["", "../escape", " leading", "trailing ", "a/b"])
    def test_rejects_unsafe_segments(self, value: str) -> None:
        with pytest.raises(ValueError, match="seg"):
            validate_path_segment(value, field_name="seg")


class TestEnsureWithin:
    def test_allows_target_inside_root(self, tmp_path: Path) -> None:
        target = tmp_path / "run1" / "bronze"
        ensure_within(tmp_path, target, label="bronze directory")  # No exception.

    def test_rejects_sibling_with_shared_prefix(self, tmp_path: Path) -> None:
        # /tmp/root2 shares /tmp/root prefix but is not contained—false-positive case.
        root = tmp_path / "root"
        root.mkdir()
        sibling = tmp_path / "root2"
        sibling.mkdir()

        with pytest.raises(ValueError, match="escapes output_root"):
            ensure_within(root, sibling, label="dir")

    def test_rejects_parent_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()

        with pytest.raises(ValueError, match="escapes output_root"):
            ensure_within(root, root / ".." / "outside", label="dir")

    def test_tolerates_inconsistent_extended_prefix_between_root_and_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recognize path as identical even with extended prefix on one of root/target (#506)."""
        root = tmp_path / "root"
        root.mkdir()
        target = root / "run1" / "silver" / "sales"
        target.mkdir(parents=True)
        real_resolve = Path.resolve

        def fake_resolve(self: Path, strict: bool = False) -> Path:
            resolved = real_resolve(self, strict=strict)
            if self == target:
                return Path("\\\\?\\" + str(resolved))
            return resolved

        monkeypatch.setattr(Path, "resolve", fake_resolve)

        ensure_within(root, target, label="silver directory")  # No exception.

    @requires_symlinks
    def test_rejects_escape_via_existing_symlink(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "linked"
        link.symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="escapes output_root"):
            ensure_within(root, link / "child", label="dir")


class TestSafeOutputPath:
    def test_allows_simple_relative_path(self, tmp_path: Path) -> None:
        result = safe_output_path(tmp_path, "train.parquet")
        assert result == tmp_path / "train.parquet"

    def test_allows_nested_relative_path(self, tmp_path: Path) -> None:
        result = safe_output_path(tmp_path, "data/train.parquet")
        assert result == tmp_path / "data" / "train.parquet"

    @pytest.mark.parametrize(
        "evil",
        ["../escape.parquet", "../../etc/passwd", "data/../../etc/passwd", "a/b/../../../x"],
    )
    def test_rejects_parent_traversal(self, tmp_path: Path, evil: str) -> None:
        with pytest.raises(PathTraversalError, match="escapes base directory"):
            _ = safe_output_path(tmp_path, evil)

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        # Absolute path ignores base when combined with base_dir / "/etc/passwd" and escapes as-is.
        with pytest.raises(PathTraversalError, match="escapes base directory"):
            _ = safe_output_path(tmp_path, "/etc/passwd")

    def test_is_export_error_subclass(self, tmp_path: Path) -> None:
        # Inherit from ExportError to be caught by existing except ExportError paths.
        from kpubdata_builder.errors import ExportError

        with pytest.raises(ExportError):
            _ = safe_output_path(tmp_path, "../oops")
