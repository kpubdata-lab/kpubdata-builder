"""``param_grid`` expansion — iterate over parameter combinations (#613)."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sized
from itertools import product

from .models import JsonValue

__all__ = [
    "DEFAULT_PARAM_GRID_MAX_COMBINATIONS",
    "PARAM_GRID_MAX_COMBINATIONS_ENV",
    "count_param_grid_combinations",
    "expand_param_grid",
    "param_grid_max_combinations",
]

PARAM_GRID_MAX_COMBINATIONS_ENV = "KPUBDATA_BUILDER_PARAM_GRID_MAX_COMBINATIONS"
#: Chosen without a measurement (#1340): several times the largest grid this repository
#: declares (1,500 combinations), and far below what a few long axes multiply to.
DEFAULT_PARAM_GRID_MAX_COMBINATIONS = 10_000


def param_grid_max_combinations() -> int | None:
    """Combinations one source's ``param_grid`` may expand to, or None for no limit.

    An unset, malformed or negative value is the default; the start-up check reports a
    malformed one (``startup_settings``). ``0`` turns the limit off.
    """
    raw = os.environ.get(PARAM_GRID_MAX_COMBINATIONS_ENV, "").strip()
    value = DEFAULT_PARAM_GRID_MAX_COMBINATIONS
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            value = parsed
    return value or None


def count_param_grid_combinations(param_grid: Mapping[str, Sized]) -> int:
    """How many calls :func:`expand_param_grid` would return, without expanding.

    The product of the axis lengths: one multiplication per axis, whatever the product
    comes to. An empty grid is one call; a grid with an empty axis is none.
    """
    return math.prod(len(values) for values in param_grid.values())


def expand_param_grid(
    params: dict[str, JsonValue],
    param_grid: dict[str, tuple[JsonValue, ...]],
) -> tuple[dict[str, JsonValue], ...]:
    """Expand common ``params`` and ``param_grid`` into list of call combinations.

    Order contract:

    1. **Keys sorted alphabetically.** Don't depend on YAML key declaration order —
       ``canonical_spec_mapping()`` sorts keys when writing snapshots, so relying
       on declaration order makes same-digest specs call in different orders.
    2. **Last key changes fastest** (nested order like ``itertools.product``).
    3. Within each key, **preserve declared value order exactly.** Don't sort values
       — lists like ``["202001", ..., "202012"]`` have intentional human order with
       no reason to reverse.

    Common ``params`` merged into every combination. If keys overlap, ``param_grid``
    wins — validator already rejects such declarations, but direct library calls
    define behavior anyway.

    Args:
        params: Parameters common to all combinations.
        param_grid: Per-key value lists. Empty returns just (params,).

    Returns:
        Tuple of call combinations. Length is 1 if param_grid is empty.
    """
    if not param_grid:
        return (dict(params),)

    keys = sorted(param_grid)
    value_lists = [param_grid[key] for key in keys]
    return tuple(
        {**params, **dict(zip(keys, combination, strict=True))}
        for combination in product(*value_lists)
    )
