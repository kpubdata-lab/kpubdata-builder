"""exporter registry and plugin registration API (#13, #325).

This module maintains kind string → exporter factory/instance mapping and allows
third-party exporters to register via three approaches.

Approach A (factory registration, ADR 0004 recommended):
    register_exporter_factory("csv", CsvExporter)

Approach B (instance registration, legacy compatible):
    register_exporter_instance(CsvExporter())

Approach C (entry points auto-discovery):
    if external package declares entry point in pyproject.toml, explicitly call
    load_entry_point_exporters() to discover and register (auto-loading is avoided
    to not execute arbitrary third-party code at import).

        [project.entry-points."kpubdata_builder.exporters"]
        csv = "my_package:CsvExporter"
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points

from .base import BaseExporter

EXPORTER_ENTRY_POINT_GROUP = "kpubdata_builder.exporters"

# kind -> (factory function, instance cache)
# factory must return a new instance on each invocation.
ExporterFactory = Callable[[], BaseExporter]
_EXPORTER_FACTORIES: dict[str, ExporterFactory] = {}

# instance registry for legacy compatibility (pre-ADR 0004 style)
EXPORTER_REGISTRY: dict[str, BaseExporter] = {}


def register_exporter_factory(
    kind: str, factory: ExporterFactory, *, override: bool = False
) -> None:
    """registers exporter factory with kind string to registry (#325).

    Factory must return new exporter instance on each call. This approach follows
    ADR 0004 recommendation; duplicate registration is rejected by default.

    Args:
        kind: exporter identifier (e.g., "csv", "parquet").
        factory: callable returning BaseExporter instance without args.
        override: whether to overwrite if same kind already exists.

    Raises:
        ValueError: if same kind already registered and override is False.

    Example:
        >>> register_exporter_factory("csv", CsvExporter)
        >>> exporter = get_exporter("csv")
    """
    if kind in _EXPORTER_FACTORIES and not override:
        raise ValueError(f"exporter kind {kind!r} is already registered")
    _EXPORTER_FACTORIES[kind] = factory


def register_exporter_instance(exporter: BaseExporter, *, override: bool = False) -> None:
    """registers exporter instance by its name to registry.

    .. deprecated::
        ADR 0004 recommends using register_exporter_factory. This function is maintained
        for backward compatibility.

    Args:
        exporter: BaseExporter instance to register.
        override: whether to overwrite if same name already exists.

    Raises:
        ValueError: if same name already registered and override is False.
    """
    name = exporter.name
    if name in EXPORTER_REGISTRY and not override:
        raise ValueError(f"exporter {name!r} is already registered")
    EXPORTER_REGISTRY[name] = exporter


# alias for backward compatibility
register_exporter = register_exporter_instance


def registered_exporter_kinds() -> frozenset[str]:
    """Every kind ``get_exporter`` can serve: both registries together (#1192).

    The answer to "is this kind supported" for anything that does not go on to build
    the exporter — spec validation, above all. It read the legacy instance registry
    alone, so an exporter registered the way ADR 0004 and ``AGENTS.md`` say to, by
    factory, was found by ``get_exporter`` and refused by validation.
    """
    return frozenset(_EXPORTER_FACTORIES) | frozenset(EXPORTER_REGISTRY)


def get_exporter(name: str) -> BaseExporter:
    """looks up registered exporter by kind name."""
    # factory registry takes precedence (ADR 0004)
    if name in _EXPORTER_FACTORIES:
        return _EXPORTER_FACTORIES[name]()
    # legacy instance registry fallback
    if name in EXPORTER_REGISTRY:
        return EXPORTER_REGISTRY[name]
    raise KeyError(
        f"unknown exporter kind: {name!r}; registered: {sorted(registered_exporter_kinds())}"
    )


def load_entry_point_exporters(*, override: bool = False) -> list[str]:
    """discovers and registers external exporter plugins from entry point group.

    Each entry point must point to BaseExporter instance or class creatable without args.
    If class, register as factory; if instance, register to instance registry.

    Args:
        override: whether to overwrite existing registration.

    Returns:
        list[str]: list of registered exporter names (sorted by name).
    """
    registered: list[str] = []
    for entry_point in entry_points(group=EXPORTER_ENTRY_POINT_GROUP):
        loaded = entry_point.load()
        if isinstance(loaded, type):
            # class: register as factory (ADR 0004 recommendation)
            if not issubclass(loaded, BaseExporter):
                raise TypeError(
                    f"entry point {entry_point.name!r} did not resolve to a BaseExporter subclass"
                )
            register_exporter_factory(entry_point.name, loaded, override=override)
            registered.append(entry_point.name)
        else:
            # instance: register using legacy method
            exporter = loaded
            if not isinstance(exporter, BaseExporter):
                raise TypeError(
                    f"entry point {entry_point.name!r} did not resolve to a BaseExporter"
                )
            register_exporter_instance(exporter, override=override)
            registered.append(exporter.name)
    return sorted(registered)


def clear_exporter_registry() -> None:
    """initializes all exporter registrations (#325, #326).

    Primarily used in tests. Clears both factory and instance registries to prevent
    exporter registration leaks between tests.
    """
    _EXPORTER_FACTORIES.clear()
    EXPORTER_REGISTRY.clear()


__all__ = [
    "EXPORTER_ENTRY_POINT_GROUP",
    "EXPORTER_REGISTRY",
    "clear_exporter_registry",
    "get_exporter",
    "load_entry_point_exporters",
    "register_exporter",
    "register_exporter_factory",
    "register_exporter_instance",
    "registered_exporter_kinds",
]
