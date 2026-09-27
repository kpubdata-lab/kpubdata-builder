"""shared JSON-safe export conversion (#629 follow-up).

Gold tables come from Polars, so ``date``/``datetime``/``Decimal`` and similar Python
objects stay in records as-is. ``json.dumps`` cannot serialize them, raising
``TypeError``. That exception is masked at service boundary, so users see
buil failure without root cause simply because they declared
``casts: {deal_date: date}``.

**Only convert types with single standard representation.** Converting arbitrary objects
via ``str()`` or flattening ``set`` to list silently changes data—such values should
reach ``json.dumps`` and fail with ``TypeError``. Which representation to choose is
contract; this layer should not decide silently.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any

__all__ = ["json_safe"]


def json_safe(value: Any) -> Any:
    """converts only values with lossless standard representation to JSON-compatible values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        # tuple preserves existing json.dumps array serialization behavior.
        return [json_safe(item) for item in value]
    return value
