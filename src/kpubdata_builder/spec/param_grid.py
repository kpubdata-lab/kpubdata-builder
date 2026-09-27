"""``param_grid`` expansion — iterate over parameter combinations (#613)."""

from __future__ import annotations

from itertools import product

from .models import JsonValue

__all__ = ["expand_param_grid"]


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
