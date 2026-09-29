"""Remove plaintext credentials from what a response or a manifest carries.

Shared by the build execution path and the preview path. It lives on its own so the
domain services extracted from ``app.py`` (#596, #637) can use it without importing
``app``, which imports them.
"""

from __future__ import annotations

from collections.abc import Iterable


def redact_secret_text(value: str | None, secrets: Iterable[str]) -> str | None:
    """Remove plaintext credentials from response/manifest strings."""
    if value is None:
        return None
    redacted = value
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact_json_secrets(value: object, secrets: Iterable[str]) -> object:
    """Recursively remove credentials from all strings in JSON tree."""
    secret_values = tuple(secrets)
    if isinstance(value, str):
        return redact_secret_text(value, secret_values)
    if isinstance(value, list):
        return [redact_json_secrets(item, secret_values) for item in value]
    if isinstance(value, dict):
        return {key: redact_json_secrets(item, secret_values) for key, item in value.items()}
    return value


__all__ = ["redact_json_secrets", "redact_secret_text"]
