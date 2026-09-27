"""Reusable build template rendering (#14).

Define frequently-used build patterns as YAML templates with a ``_template``
metadata block and ``{{ param }}`` placeholders, then generate completed BuildSpec
YAML by substituting only parameters. Uses stdlib regex substitution with no
external dependencies.

Main functions:
    - render_template: template + parameters → completed YAML string
    - load_template: render template then load as BuildSpec
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..errors import SpecLoadError
from .loader import parse_spec
from .models import BuildSpec

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _effective_params(template_meta: dict[str, object], params: dict[str, str]) -> dict[str, str]:
    """Overlay user parameters on declared parameter defaults to produce final values."""
    effective: dict[str, str] = {}
    declared = template_meta.get("parameters", {})
    if isinstance(declared, dict):
        for name, meta in declared.items():
            if isinstance(meta, dict) and "default" in meta:
                effective[str(name)] = str(meta["default"])
    for name, value in params.items():
        effective[name] = str(value)
    return effective


def _render_template_data(path: str | Path, params: dict[str, str]) -> dict[str, object]:
    """Substitute template YAML with parameters and return as in-memory mapping (internal helper).

    Avoids re-parsing rendered YAML to prevent type coercion like "1" → int.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise SpecLoadError(f"Failed to load template from {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise SpecLoadError(f"Failed to render template {path}: top-level YAML must be a mapping")

    data: dict[str, object] = dict(loaded)
    template_meta = data.pop("_template", {})
    meta = template_meta if isinstance(template_meta, dict) else {}
    effective = _effective_params(meta, params)

    # Substitute in the data structure directly to avoid YAML structure corruption.
    missing: list[str] = []

    def _substitute_in_value(value: object) -> object:
        if isinstance(value, str):

            def _replace(match: re.Match[str]) -> str:
                name = match.group(1)
                if name not in effective:
                    missing.append(name)
                    return match.group(0)
                return effective[name]

            return _PLACEHOLDER.sub(_replace, value)
        if isinstance(value, list):
            return [_substitute_in_value(item) for item in value]
        if isinstance(value, dict):
            return {k: _substitute_in_value(v) for k, v in value.items()}
        return value

    substituted = _substitute_in_value(data)

    if missing:
        unique = ", ".join(sorted(set(missing)))
        raise SpecLoadError(f"Missing template parameter(s): {unique}")

    if not isinstance(substituted, dict):
        raise SpecLoadError(
            f"Template substitution produced a non-mapping result for {path}: {type(substituted)}"
        )
    return substituted


def render_template(path: str | Path, params: dict[str, str]) -> str:
    """Render template YAML with parameters and return completed YAML string.

    Args:
        path: Template YAML file path.
        params: Placeholder parameters (override declared defaults).

    Returns:
        str: YAML with ``_template`` block removed and placeholders substituted.

    Raises:
        SpecLoadError: File load failed, top-level is not a mapping, or placeholders
            with no values remain.
    """
    substituted = _render_template_data(path, params)
    # Re-dump so the output is valid YAML regardless of substitution values.
    return yaml.safe_dump(substituted, allow_unicode=True, sort_keys=False)


def load_template(path: str | Path, params: dict[str, str]) -> BuildSpec:
    """Render template then parse as BuildSpec.

    Args:
        path: Template YAML file path.
        params: Placeholder parameters.

    Returns:
        BuildSpec: Rendered and parsed build specification.

    Raises:
        SpecLoadError: Rendering or parsing failed.
    """
    # Pass already-substituted in-memory structure directly to parse_spec to avoid
    # YAML re-serialization/re-parsing and type coercion ("1" → int, etc.) (#225).
    substituted = _render_template_data(path, params)
    return parse_spec(substituted)


__all__ = ["load_template", "render_template"]
