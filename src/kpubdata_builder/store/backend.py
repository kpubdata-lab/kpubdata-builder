"""Storage backend selection (ADR 0016).

Default is sqlite/local FS (no external deps, deterministic per AGENTS.md). Environment
variable ``KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid`` switches to CUBRID (SQLAlchemy).

Key rules:
    - ``sqlalchemy`` import must happen only *inside* cubrid branch of this module.
      Default (sqlite) path doesn't import SQLAlchemy, so no optional deps
      needed for service to work.
    - CUBRID components (BuildIndex/Credential/ArtifactStore) are **process-global singleton
      Engine** is shared. Engine manages connection pool; multithreaded (#334 async
      jobs etc) borrow short connection per operation, not reusing raw connection per thread
      (``with engine.begin()``).
    - ``pool_pre_ping=True`` auto-detects stale connections (CUBRID restart/idle
      timeout guard).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sqlalchemy import Engine

_BACKEND_ENV = "KPUBDATA_BUILDER_STORAGE_BACKEND"
_CUBRID_URL_ENV = "KPUBDATA_BUILDER_CUBRID_URL"

# Driver chosen by ADR 0016. sqlalchemy-cubrid registers dialect in four ways:
# (`cubrid`, `cubrid.cubrid`, `cubrid.cubriddb`, `cubrid.pycubrid`); first three
# use legacy C-extension (CUBRID-Python, import name `CUBRIDdb`), pure Python
# Only one driver: `pycubrid`. `[cubrid]` extra installs pycubrid only.
_CUBRID_DRIVER = "pycubrid"
_CUBRID_DIALECT = "cubrid"
_CANONICAL_SCHEME = f"{_CUBRID_DIALECT}+{_CUBRID_DRIVER}"
# Async dialect. Engine is sync, can't use here.
_ASYNC_DRIVER = "aiopycubrid"

_logger = logging.getLogger(__name__)

StorageBackend = Literal["sqlite", "cubrid"]

_engine_lock = threading.Lock()
_engine: Engine | None = None


def storage_backend() -> StorageBackend:
    """Selected state backend. Defaults to ``sqlite`` if unset/empty."""
    raw = os.environ.get(_BACKEND_ENV, "").strip().lower()
    if raw in ("", "sqlite"):
        return "sqlite"
    if raw == "cubrid":
        return "cubrid"
    raise RuntimeError(f"{_BACKEND_ENV} must be 'sqlite' or 'cubrid', got {raw!r}")


def normalize_cubrid_url(url: str) -> str:
    """Force URL to use pycubrid driver (ADR 0016).

    Omitted driver ``cubrid://`` interpreted by SQLAlchemy as default dialect (= C-extension
    based), dies at connect time with ``ImportError: Could not import CUBRIDdb``.
    Silently passes at startup, fails at first query; normalize here to eliminate that path.

    - ``cubrid+pycubrid://`` — pass through as-is.
    - ``cubrid://`` — normalize to ``cubrid+pycubrid://`` (log warning).
    - ``cubrid+cubriddb://`` / ``cubrid+cubrid://`` — reject. C-extension driver
      not installed via ``[cubrid]`` extra, never validated in this backend.
    - ``cubrid+aiopycubrid://`` — reject. Doesn't match sync ``Engine``.
    - Other scheme — reject (prevent typo/different DB URL injection).
    """
    scheme, separator, remainder = url.partition("://")
    if not separator:
        raise RuntimeError(
            f"{_CUBRID_URL_ENV} must be a SQLAlchemy URL like "
            f"{_CANONICAL_SCHEME}://user:pass@host:33000/db?charset=utf8, got {url!r}"
        )
    dialect, _, driver = scheme.partition("+")
    if dialect != _CUBRID_DIALECT:
        raise RuntimeError(
            f"{_CUBRID_URL_ENV} must use the '{_CUBRID_DIALECT}' dialect "
            f"(e.g. {_CANONICAL_SCHEME}://...), got scheme {scheme!r}"
        )
    if driver == _CUBRID_DRIVER:
        return url
    if not driver:
        # URL without driver silently fixed, fact of config change logged.
        _logger.warning(
            "%s omits the driver; using %s (a bare cubrid:// URL resolves to the "
            "legacy CUBRIDdb C-extension, which this build does not install).",
            _CUBRID_URL_ENV,
            _CANONICAL_SCHEME,
        )
        return f"{_CANONICAL_SCHEME}://{remainder}"
    if driver == _ASYNC_DRIVER:
        raise RuntimeError(
            f"{_CUBRID_URL_ENV} uses the async driver {driver!r}, but this backend "
            f"runs on a synchronous SQLAlchemy Engine; use {_CANONICAL_SCHEME}:// instead."
        )
    raise RuntimeError(
        f"{_CUBRID_URL_ENV} uses driver {driver!r}, which is backed by the legacy "
        "CUBRID-Python C-extension. The [cubrid] extra installs the pure-python "
        f"pycubrid driver only (ADR 0016) — use {_CANONICAL_SCHEME}:// instead."
    )


def cubrid_url() -> str:
    """CUBRID SQLAlchemy URL. Fail-closed if cubrid backend unset.

    Return value always normalized to pycubrid driver (``normalize_cubrid_url``).
    """
    url = os.environ.get(_CUBRID_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{_BACKEND_ENV}=cubrid requires {_CUBRID_URL_ENV} "
            f"(SQLAlchemy URL, e.g. {_CANONICAL_SCHEME}://user:pass@host:33000/db?charset=utf8)"
        )
    return normalize_cubrid_url(url)


def get_engine() -> Engine:
    """Process-global singleton SQLAlchemy ``Engine`` (lazy-created, thread-safe).

    ``sqlalchemy`` import only here — sqlite default path doesn't call
    this function, no SQLAlchemy dep.
    """
    global _engine
    with _engine_lock:
        if _engine is None:
            from sqlalchemy import create_engine

            _engine = create_engine(cubrid_url(), pool_pre_ping=True, future=True)
        return _engine


def validate_storage_config() -> None:
    """Called at serve start (fail-fast, ADR 0016).

    - sqlite backend → no-op.
    - cubrid backend → URL unset/driver mismatch, ``sqlalchemy-cubrid``/``pycubrid``
      not installed, **actual connection failure** → reject startup. (Index/manifest
      write failure swallowed at runtime best-effort, but startup config errors must
      surface early.)

    If config correct but server down, startup succeeds, first request fails
    — by then write already swallowed best-effort, status silently
    lost. So actually open connection here once (#587).
    """
    if storage_backend() != "cubrid":
        return
    cubrid_url()
    try:
        import sqlalchemy_cubrid  # noqa: F401
    except ImportError as exc:  # pragma: no cover - extra not installed environment
        raise RuntimeError(
            "KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid but sqlalchemy-cubrid is not "
            "installed; install with: uv sync --extra cubrid."
        ) from exc
    try:
        # Even normalized URL fails at first query if driver missing — catch here.
        import pycubrid  # noqa: F401
    except ImportError as exc:  # pragma: no cover - extra not installed environment
        raise RuntimeError(
            "KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid but the pycubrid driver is not "
            "installed; install with: uv sync --extra cubrid (sqlalchemy-cubrid alone "
            "does not pull a driver — ADR 0016 uses sqlalchemy-cubrid[pycubrid])."
        ) from exc
    try:
        # Return validation connection immediately but keep Engine — it's the pool serve uses next,
        # so first request after startup doesn't need new connection.
        with get_engine().connect():
            pass
    except Exception as exc:
        # Keeping failed Engine means next call reuses same dead pool. Discard so retry
        # re-reads URL from scratch.
        dispose_engine()
        raise RuntimeError(
            f"{_BACKEND_ENV}=cubrid but the CUBRID server at {_CUBRID_URL_ENV} is not "
            f"reachable; refusing to start. Underlying error: {exc}"
        ) from exc


def dispose_engine() -> None:
    """Discard global Engine (process exit/test cleanup)."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None


__all__ = [
    "StorageBackend",
    "cubrid_url",
    "dispose_engine",
    "get_engine",
    "storage_backend",
    "validate_storage_config",
]
