"""Replay mode as Builder's own setting, with fixtures Builder ships (#837).

Replay is implemented by kpubdata's transport and switched on by kpubdata's variables
(``KPUBDATA_MODE=replay``, ``KPUBDATA_REPLAY_DIR``). A client of Builder should not
have to know those names or where kpubdata keeps its test fixtures, so Builder exposes
``serve --replay`` / ``--replay-dir`` and ``KPUBDATA_BUILDER_REPLAY_DIR`` and
translates them here.

kpubdata's spec executor asks for a provider key before the request reaches the
transport, so replay also needs *a* key. For every provider that has fixtures and no
key configured, a placeholder is set. It is not a credential: a request replay does
not recognise falls through to the live API with it and fails authentication, which
is the right outcome for a development mode.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Fixtures packaged with Builder.
BUNDLED_FIXTURES = Path(__file__).parent / "replay_fixtures"
#: Builder's own setting for the fixture directory.
REPLAY_DIR_ENV = "KPUBDATA_BUILDER_REPLAY_DIR"
#: Stands in for a provider key the replayed requests never send anywhere.
PLACEHOLDER_KEY = "replay-placeholder"


def fixture_providers(fixture_dir: Path) -> list[str]:
    """Providers with at least one recorded response under ``fixture_dir``."""
    return sorted(
        child.name
        for child in fixture_dir.iterdir()
        if child.is_dir() and any(child.rglob("*.meta.json"))
    )


def enable_replay(fixture_dir: Path) -> list[str]:
    """Switch kpubdata to replay from ``fixture_dir`` for this process.

    Returns:
        The providers that were given a placeholder key because none was set.

    Raises:
        ValueError: ``fixture_dir`` is not a directory or holds no recording — a replay
            mode that replays nothing would send every request to the live API.
    """
    if not fixture_dir.is_dir():
        raise ValueError(f"no such replay fixture directory: {fixture_dir}")
    providers = fixture_providers(fixture_dir)
    if not providers:
        raise ValueError(f"no recorded responses (*.meta.json) under {fixture_dir}")
    os.environ["KPUBDATA_MODE"] = "replay"
    os.environ["KPUBDATA_REPLAY_DIR"] = str(fixture_dir.resolve())
    placeholders: list[str] = []
    for provider in providers:
        variable = f"KPUBDATA_{provider.upper()}_API_KEY"
        if not os.environ.get(variable):
            os.environ[variable] = PLACEHOLDER_KEY
            placeholders.append(provider)
    return placeholders


def export_fixtures(destination: Path) -> list[Path]:
    """Copy the bundled fixtures into ``destination``; never overwrite a file.

    Raises:
        FileExistsError: A file with the same path already exists — nothing is copied.
    """
    sources = sorted(p for p in BUNDLED_FIXTURES.rglob("*") if p.is_file())
    targets = [destination / p.relative_to(BUNDLED_FIXTURES) for p in sources]
    existing = [t for t in targets if t.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite {existing[0]}")
    for source, target in zip(sources, targets, strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return targets


__all__ = [
    "BUNDLED_FIXTURES",
    "PLACEHOLDER_KEY",
    "REPLAY_DIR_ENV",
    "enable_replay",
    "export_fixtures",
    "fixture_providers",
]
