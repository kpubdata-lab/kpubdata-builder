"""Refusing a state store written by a release this code does not know (#1096).

Each SQLite store records the version of its schema. A store whose version is newer
than the running code was written by a later release: this code does not know what the
later one added or what it relies on, so it must not read it as its own, and must not
recreate it to make the difference go away. It refuses to open, and the server does not
start.

That is what an operator meets after rolling a deployment back. The message says which
store, which versions, and what can be done.
"""

from __future__ import annotations


class UnsupportedSchemaVersionError(RuntimeError):
    """A state store has a schema version this code cannot use."""

    def __init__(self, *, store: str, location: str, found: int, supported: int, remedy: str):
        self.store = store
        self.found = found
        self.supported = supported
        super().__init__(
            f"{store} at {location} has schema version {found}; this release supports "
            f"up to {supported}. It was written by a newer release and is left "
            f"untouched. {remedy}"
        )


__all__ = ["UnsupportedSchemaVersionError"]
