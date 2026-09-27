"""Service route adapters with explicit ordering."""

from __future__ import annotations

from . import (
    admin,
    artifacts,
    builds,
    core,
    datasets,
    events,
    monitoring,
    providers,
    publish,
    quality,
    query,
    stages,
)
from ._types import RouteAdapter

# admin (#679) comes first — "/admin/" prefix does not overlap with other adapter paths,
# and admin routes are never bypassed by general path matching.
#
# Order is fixed by existing app.dispatch condition precedence. events (#496)/publish (#491)
# come right after builds — they all handle "/builds/{run_id}/..." paths, so logically neighbors
# (actual matching uses each adapter's path suffix check to not overlap).
ROUTE_ADAPTERS: tuple[RouteAdapter, ...] = (
    admin.route,
    core.route,
    providers.route,
    query.route,
    datasets.route,
    builds.route,
    events.route,
    publish.route,
    quality.route,
    stages.route,
    artifacts.route,
    monitoring.route,
)

__all__ = ["ROUTE_ADAPTERS", "RouteAdapter"]
