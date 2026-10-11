"""The settings a process is running with, in one object (#1108).

Each setting is read where it is used, so there was no one place that could say what a
running Builder had been given: an operator compared ``.env``, the compose file and the
defaults in the deployment guide by eye. :func:`effective_settings` reads every setting
of the catalog once and returns an :class:`EffectiveSettings` — one frozen object, one
entry per setting, each with where its value came from and the value as the Python type
the catalog's ``kind`` names. ``kpubdata-builder settings`` prints it, and ``serve``
prints the entries that are not at their default when it starts.

What it is not: the readers still parse their own settings, with their own ranges, and
``startup_settings.check_settings`` is still what refuses a value. This object reports;
it decides nothing. Where a reader falls back to its default on a value it cannot read,
the entry says the default is in use, by the same predicate the start-up check warns
with.

A setting marked ``secret`` in the catalog is never read into the object at all — the
entry records that it is set, and nothing else — so no way of printing the object can
show one.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from .. import settings_catalog
from ..settings_catalog import Group, Kind, Setting
from ..settings_env import EarlierNameUse
from . import startup_settings

#: Where a setting's value came from: the variable of its own name, a variable of a
#: name it used to have, a command-line flag that takes the variable's place, or
#: nowhere — the reader's default is in use.
Source = Literal["environment", "earlier name", "flag", "default"]

Value = str | int | float | bool | None

#: What is shown in place of a secret's value.
REDACTED = "<redacted>"


@dataclass(frozen=True)
class EffectiveSetting:
    """One setting as the process has it."""

    name: str
    group: Group
    kind: Kind
    secret: bool
    source: Source
    #: The value in use, typed by ``kind``. None when the default is in use (the
    #: default itself is the reader's; ``default`` describes it), and always None for
    #: a secret. A flag is always True or False: on or off is what is in use.
    value: Value
    #: The default as the deployment guide words it.
    default: str
    #: Anything an operator should know that the value does not say.
    note: str = ""

    @property
    def is_set(self) -> bool:
        """Whether something other than the default is in use."""
        return self.source != "default"

    def line(self) -> str:
        """The entry as one line. A secret's value is not in it."""
        if self.secret:
            shown = REDACTED if self.is_set else "(not set)"
        elif self.kind == "flag":
            shown = "on" if self.value else "off"
        elif self.value is None:
            shown = f"(default: {self.default.replace('`', '')})"
        else:
            shown = str(self.value)
        text = f"{self.name} = {shown}"
        if self.is_set:
            text += f"  [{self.source}]"
        if self.note:
            text += f"  # {self.note}"
        return text

    def as_dict(self) -> dict[str, Value]:
        """The entry for JSON. ``inf`` is a value some settings take and JSON has no
        number for, so a number that is not finite is given as its text."""
        value = self.value
        if isinstance(value, float) and not math.isfinite(value):
            value = str(value)
        return {
            "name": self.name,
            "group": self.group,
            "kind": self.kind,
            "secret": self.secret,
            "source": self.source,
            "set": self.is_set,
            "value": value,
            "note": self.note,
        }


@dataclass(frozen=True)
class EffectiveSettings:
    """Every setting of the catalog as the process has it, in catalog order."""

    entries: tuple[EffectiveSetting, ...]

    def __getitem__(self, name: str) -> EffectiveSetting:
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def lines(self, *, only_set: bool = False) -> list[str]:
        """One line per setting.

        With ``only_set``, only those an operator has something to read about: a value
        other than the default, or a note — a value that was ignored, a switch the
        deployment turns on by itself.
        """
        return [
            entry.line() for entry in self.entries if entry.is_set or entry.note or not only_set
        ]

    def as_list(self) -> list[dict[str, Value]]:
        return [entry.as_dict() for entry in self.entries]


def _typed(kind: Kind, raw: str) -> Value:
    """``raw`` as the type ``kind`` names, or None when it cannot be read as one."""
    try:
        if kind == "integer":
            return int(raw)
        if kind == "number":
            return float(raw)
    except ValueError:
        return None
    return raw


def _flag(setting: Setting, raw: str) -> tuple[bool, str]:
    """Whether a flag is on, and why when that is not what its variable says."""
    strips, on_words = startup_settings.FLAGS[setting.name]
    written = (raw.strip() if strips else raw).lower()
    if startup_settings.forced_on(setting.name):
        return True, "on whatever the variable says: this deployment serves more than one user"
    return written in on_words, ""


def _entry(
    setting: Setting,
    flags: Mapping[str, str | int | float],
    earlier: frozenset[str],
) -> EffectiveSetting:
    def entry(source: Source, value: Value, note: str = "") -> EffectiveSetting:
        return EffectiveSetting(
            name=setting.name,
            group=setting.group,
            kind=setting.kind,
            secret=setting.secret,
            source=source,
            value=value,
            default=setting.default,
            note=note,
        )

    written: Source = "earlier name" if setting.name in earlier else "environment"
    if setting.secret:
        # Asked only whether it is there: the value is never read into this object.
        return entry(written if os.environ.get(setting.name) else "default", None)
    if setting.name in flags:
        return entry("flag", flags[setting.name])
    raw = os.environ.get(setting.name, "")
    if setting.kind == "flag":
        on, note = _flag(setting, raw)
        return entry(written if raw.strip() else "default", on, note)
    raw = raw.strip()
    if not raw:
        return entry("default", None)
    falls_back = startup_settings.FALLS_BACK.get(setting.name)
    if falls_back is not None and not falls_back[0](raw):
        return entry("default", None, f"the value written is ignored: it must be {falls_back[1]}")
    value = _typed(setting.kind, raw)
    if value is None:
        return entry(written, None, f"the value written cannot be read as {setting.kind}")
    return entry(written, value)


def effective_settings(
    *,
    flags: Mapping[str, str | int | float] | None = None,
    earlier: Iterable[EarlierNameUse] = (),
) -> EffectiveSettings:
    """Every setting of the catalog as this process has it.

    Args:
        flags: Settings a command-line flag took the place of, with the flag's value.
            The variable of such a setting is never read.
        earlier: What ``settings_env.apply_earlier_names`` reported, so that a value
            that arrived under an earlier name says so.
    """
    from_earlier = frozenset(use.current for use in earlier if use.used)
    return EffectiveSettings(
        tuple(_entry(setting, flags or {}, from_earlier) for setting in settings_catalog.SETTINGS)
    )


__all__ = [
    "REDACTED",
    "EffectiveSetting",
    "EffectiveSettings",
    "Source",
    "Value",
    "effective_settings",
]
