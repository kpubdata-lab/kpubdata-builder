"""Refresh cadence: an ISO 8601 duration a table is expected to be refreshed within (#781)."""

from __future__ import annotations

import re
from datetime import timedelta

_CADENCE = re.compile(
    r"^P(?:(?P<weeks>\d+)W|(?P<days>\d+)D|T(?P<hours>\d+)H|(?P<days2>\d+)DT(?P<hours2>\d+)H)$"
)


def parse_cadence(value: str) -> timedelta:
    """``P1D`` / ``PT6H`` / ``P1W`` / ``P1DT12H`` → timedelta.

    Raises:
        ValueError: Not one of those forms, or zero. Months and years are refused:
            their length varies, and a staleness verdict must not.
    """
    match = _CADENCE.match(value.strip())
    if match is None:
        raise ValueError(
            f"refresh_cadence {value!r} must be an ISO 8601 duration of weeks, days or "
            "hours, such as P1D, PT6H, P1W or P1DT12H"
        )
    parts = {k: int(v) for k, v in match.groupdict().items() if v is not None}
    cadence = timedelta(
        weeks=parts.get("weeks", 0),
        days=parts.get("days", 0) + parts.get("days2", 0),
        hours=parts.get("hours", 0) + parts.get("hours2", 0),
    )
    if cadence <= timedelta(0):
        raise ValueError(f"refresh_cadence {value!r} must be longer than zero")
    return cadence


__all__ = ["parse_cadence"]
