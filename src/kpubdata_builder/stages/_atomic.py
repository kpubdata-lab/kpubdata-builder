"""directory-level atomic replacement utility (#180).

If existing directory is deleted via ``shutil.rmtree`` then ``rename``, failure between
deletion and rename loses both old and new data. To prevent this, rename existing
directory to ``.old`` backup first, then rename new directory into place, and delete
backup only on success. Both renames are atomic on the same filesystem; if new batch
stage fails, restore backup to original location.

Main functions:
    - atomic_replace_dir: atomically replace tmp directory with final path
"""

from __future__ import annotations

import shutil
from pathlib import Path


def atomic_replace_dir(tmp_dir: Path, final_dir: Path) -> None:
    """`tmp_dir` atomically replaces the `final_dir` location.

    If ``final_dir`` does not exist, simply rename. Otherwise, replace by: rename existing
    directory to unique ``.old`` backup → rename new directory into place → delete backup.
    If new directory rename fails, restore backup to original location and propagate exception.

    Args:
        tmp_dir: temporary directory to move to final location.
        final_dir: final path where output will be placed.
    """
    if not final_dir.exists():
        tmp_dir.rename(final_dir)
        return

    # backup path made unique via tmp_dir name (mkdtemp-based) to prevent previous crash
    # avoids collision with leftover stale backups.
    backup = final_dir.with_name(f"{final_dir.name}.{tmp_dir.name}.old")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)

    final_dir.rename(backup)  # atomic: move existing data to backup
    try:
        tmp_dir.rename(final_dir)  # atomic: move new data into place
    except BaseException:
        # new batch failed -> restore existing data to original location.
        # if partial final_dir remains, remove it then restore backup to original location.
        if final_dir.exists():
            shutil.rmtree(final_dir, ignore_errors=True)
        backup.rename(final_dir)
        raise
    shutil.rmtree(backup, ignore_errors=True)


__all__ = ["atomic_replace_dir"]
