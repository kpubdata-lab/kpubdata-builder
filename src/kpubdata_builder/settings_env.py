"""Read a setting written under a name it used to have (#1108).

A setting is read where it is used, by the one name the catalog gives it. Renaming one
would turn every deployment that wrote the old name into one running on the default,
with nothing saying so. So a renamed setting keeps its earlier names in the catalog
(``Setting.earlier_names``), and :func:`apply_earlier_names` — which the
``kpubdata-builder`` command runs before anything reads a setting — copies a value
written under an earlier name to the name the readers know. No reader changes, and a
child process started later inherits the value under the new name.

The rules, which ``settings_catalog.NAMING_POLICY`` states for operators:

- The name the setting has now wins. An earlier name is used only when the current one
  is unset or empty; an empty value is "not set", which is how the readers and the
  production compose already treat it.
- Of several earlier names, the first listed (the most recent) that has a value is used.
- Every earlier name that has a value is reported, used or not, so that a deployment
  hears about it on each start until its ``.env`` is corrected.
- A message names the two variables and never a value: the setting may be a secret.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass

from . import settings_catalog
from .settings_catalog import Setting


@dataclass(frozen=True)
class EarlierNameUse:
    """An earlier name that had a value when the process started."""

    #: The name the value was written under.
    earlier: str
    #: The setting's name now.
    current: str
    #: The release the current name appeared in.
    renamed_in: str
    #: True when the value was taken; False when another name's value was used instead.
    used: bool

    def warning(self) -> str:
        """One line for the operator. It holds no value."""
        renamed = f"{self.earlier} was renamed to {self.current} in {self.renamed_in}"
        if self.used:
            return (
                f"{renamed}; its value is in use, but the old name will stop being read — "
                f"write {self.current} instead"
            )
        return (
            f"{renamed}; it is ignored because a name that takes precedence is set — "
            f"remove {self.earlier}"
        )


def apply_earlier_names(
    environ: MutableMapping[str, str] | None = None,
    settings: Iterable[Setting] | None = None,
) -> list[EarlierNameUse]:
    """Copy each value written under an earlier name to the setting's current name.

    Args:
        environ: The environment to read and write; the process's own when None.
        settings: The settings to look through; the catalog's unless a test says
            otherwise.

    Returns:
        One entry per earlier name that had a value, in catalog order.
    """
    env = os.environ if environ is None else environ
    uses: list[EarlierNameUse] = []
    for setting in settings_catalog.SETTINGS if settings is None else settings:
        taken = bool(env.get(setting.name))
        for earlier in setting.earlier_names:
            value = env.get(earlier.name)
            if not value:
                continue
            if not taken:
                env[setting.name] = value
            uses.append(
                EarlierNameUse(
                    earlier=earlier.name,
                    current=setting.name,
                    renamed_in=earlier.renamed_in,
                    used=not taken,
                )
            )
            taken = True
    return uses


__all__ = ["EarlierNameUse", "apply_earlier_names"]
