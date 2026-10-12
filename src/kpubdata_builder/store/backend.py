"""Builder keeps its state in SQLite and local files only (ADR 0022).

The CUBRID state backend (ADR 0016) was removed. Its two settings are in the catalog's
list of removed settings (``settings_catalog.REMOVED_SETTINGS``), and a deployment that
still sets one is refused here rather than started on SQLite: a leftover setting means
the operator believes the state is in CUBRID, and starting anyway would write it
somewhere else without a word. There is no fallback and no warning-only start.

Every command calls :func:`refuse_removed_storage_settings` before it does anything
(``cli.main``), and so does every factory that opens state, for code that uses Builder
without the command line.
"""

from __future__ import annotations

import os

from ..settings_catalog import REMOVED_SETTINGS

#: The decision that removed the backend, as the refusal names it.
ADR = "ADR 0022 (docs/adrs/0022-sqlite-only-state-and-cubrid-retirement.md)"
#: The last release with the CUBRID backend, where ADR 0022's reconciliation is run.
LAST_RELEASE_WITH_CUBRID = "v0.4.0"


class RemovedStorageSettingError(RuntimeError):
    """A removed storage setting is still set. The message is one line and never holds a
    setting's value: a CUBRID URL carries the database password (#1108)."""

    def __init__(self, names: list[str]) -> None:
        self.names = tuple(names)
        every = ", ".join(removed.name for removed in REMOVED_SETTINGS)
        super().__init__(
            f"{' and '.join(names)} {'is' if len(names) == 1 else 'are'} set, but the "
            f"CUBRID state backend was removed and state is kept in SQLite only ({ADR}); "
            "refusing to start. If this deployment's CUBRID data was never reconciled, "
            f"run ADR 0022 section 3 with {LAST_RELEASE_WITH_CUBRID} first; once it is, "
            f"unset {every} (the value is not shown)."
        )


def removed_storage_settings() -> list[str]:
    """The removed settings this process's environment still sets, in catalog order."""
    found: list[str] = []
    for removed in REMOVED_SETTINGS:
        value = os.environ.get(removed.name, "").strip().lower()
        if value and value not in removed.harmless:
            found.append(removed.name)
    return found


def refuse_removed_storage_settings() -> None:
    """Raise :class:`RemovedStorageSettingError` when a removed setting is still set."""
    found = removed_storage_settings()
    if found:
        raise RemovedStorageSettingError(found)


def validate_storage_config() -> None:
    """Called when ``serve`` starts: refuse a removed storage setting (ADR 0022)."""
    refuse_removed_storage_settings()


__all__ = [
    "LAST_RELEASE_WITH_CUBRID",
    "RemovedStorageSettingError",
    "refuse_removed_storage_settings",
    "removed_storage_settings",
    "validate_storage_config",
]
