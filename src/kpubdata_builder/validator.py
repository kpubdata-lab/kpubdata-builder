"""Backward compatibility shim — validate_spec moved to spec.validator (#44).

Re-export module to maintain existing ``from kpubdata_builder.validator import validate_spec``
import path. New code uses ``kpubdata_builder.spec.validator`` directly.
"""

from __future__ import annotations

from .spec.validator import validate_spec

__all__ = ["validate_spec"]
