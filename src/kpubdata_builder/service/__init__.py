"""Builder HTTP service façade package (#36).

Provides validate/preview/build/artifacts endpoints so external UIs like Studio
can call Builder. Separates logic (app) from stdlib HTTP transport (http).

Key components:
    - BuilderService / ServiceResponse / dispatch: transport-agnostic service logic
    - serve / make_handler: stdlib http.server adapter
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import API_CONTRACT_VERSION, BuilderService, FileResponse, ServiceResponse, dispatch
    from .http import make_handler, serve
    from .jobs import AsyncBuildExecutor, BuildJobSnapshot, BuildJobStatus

__all__ = [
    "API_CONTRACT_VERSION",
    "BuilderService",
    "FileResponse",
    "ServiceResponse",
    "AsyncBuildExecutor",
    "BuildJobSnapshot",
    "BuildJobStatus",
    "dispatch",
    "make_handler",
    "serve",
]


def __getattr__(name: str) -> Any:
    """Lazily load facade exports to prevent circular dependencies in query/jobs."""
    if name in {
        "API_CONTRACT_VERSION",
        "BuilderService",
        "FileResponse",
        "ServiceResponse",
        "dispatch",
    }:
        from . import app

        return getattr(app, name)
    if name in {"make_handler", "serve"}:
        from . import http

        return getattr(http, name)
    if name in {"AsyncBuildExecutor", "BuildJobSnapshot", "BuildJobStatus"}:
        from . import jobs

        return getattr(jobs, name)
    raise AttributeError(name)
