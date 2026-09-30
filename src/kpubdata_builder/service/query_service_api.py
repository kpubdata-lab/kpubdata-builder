"""Query domain service (#596, third segment after providers/uploads).

One ``POST /query`` endpoint, but **error classification is the core** — permission,
artifact absence, context error, unsafe SQL, congestion, timeout, execution failure
each map to different status codes and ``code`` values. Keeping this classification
in one place rather than scattered in a class makes it easier to review "what failure
maps to what response".

Same rule as the two prior segments: **takes only self-dependencies**, wire contract
(status codes, ``code`` values, body keys) unchanged.

Named ``query_service_api`` because ``kpubdata_builder.query.service`` already has
the execution engine's ``QueryService``. This module is the **HTTP domain service**
that uses that engine; the name is separated to avoid import confusion.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from kpubdata_builder.query.engine import QueryExecutionError, QueryTimeoutError
from kpubdata_builder.query.models import QueryRequest, QueryStage
from kpubdata_builder.query.resolver import (
    QueryArtifactUnavailableError,
    QueryContextError,
    resolve_query_context,
)
from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql
from kpubdata_builder.query.service import QueryBusyError, QueryService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.column_semantics import describe_columns, spec_semantics
from kpubdata_builder.service.datasets import read_snapshot_spec
from kpubdata_builder.service.ownership import hides_foreign_runs
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular.semantics import ColumnSemantics

_ALLOWED_FIELDS = {"dataset_id", "run_id", "stage", "source", "sql", "limit"}


def query_request_from_body(body: Mapping[str, JsonValue] | None) -> QueryRequest:
    """Validate and convert request body to ``QueryRequest``.

    Rejects unknown fields — silently ignoring typos would let users not notice
    they sent a query different from their intent.
    """
    if body is None:
        raise ValueError("request body is required")
    if not set(body).issubset(_ALLOWED_FIELDS):
        raise ValueError("request contains unknown fields")
    dataset_id = body.get("dataset_id")
    run_id = body.get("run_id")
    stage = body.get("stage")
    sql = body.get("sql")
    source = body.get("source")
    limit = body.get("limit", 100)
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ValueError("dataset_id must be a non-empty string")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if stage not in ("silver", "gold"):
        raise ValueError("stage must be silver or gold")
    if not isinstance(sql, str) or not sql:
        raise ValueError("sql must be a non-empty string")
    if source is not None and (not isinstance(source, str) or not source):
        raise ValueError("source must be a non-empty string when provided")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    return QueryRequest(
        dataset_id=dataset_id,
        run_id=run_id,
        stage=cast(QueryStage, stage),
        source=source,
        sql=sql,
        limit=limit,
    )


class QueryApiService:
    """Read-only SQL execution against server-resolved stage tables."""

    def __init__(self, *, output_root: Path, engine: QueryService) -> None:
        self._output_root = output_root
        self._engine = engine

    def query(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """Execute one validated SQL query against server-resolved stage tables."""
        try:
            request = query_request_from_body(body)
            context = resolve_query_context(self._output_root, request, principal)
        except PermissionError:
            if hides_foreign_runs():
                # The answer a missing run gets (#796).
                return ServiceResponse(400, {"error": "run not found", "code": "invalid_context"})
            return ServiceResponse(403, {"error": "forbidden", "code": "forbidden"})
        except QueryArtifactUnavailableError:
            return ServiceResponse(
                404, {"error": "query artifact unavailable", "code": "artifact_unavailable"}
            )
        except QueryContextError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_context"})
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})
        # What the run's kpubdata sources declare about the columns (#702): a code column
        # is reported as an identifier. Metadata only; the rows are sent as they are.
        semantics = spec_semantics(
            read_snapshot_spec(self._output_root, context.run_id), context.source
        )
        return execute_query(
            self._engine, context.table_path, request.sql, limit=request.limit, semantics=semantics
        )


def execute_query(
    engine: QueryService,
    table_path: Path,
    sql: str,
    *,
    limit: int,
    semantics: Mapping[str, ColumnSemantics] | None = None,
) -> ServiceResponse:
    """Validate and run ``sql`` against one table file, and classify what went wrong.

    Shared by ``POST /query`` and ``POST /warehouse/query`` (#797), so the same failure
    maps to the same status code and ``code`` whichever way the table was found.
    ``semantics`` describes the table's columns by name (#702); a result column that
    keeps a described name gets its hints, and a text code column is an identifier.
    """
    try:
        validated = validate_read_only_sql(sql)
        result = engine.execute(table_path, validated.canonical_sql, limit=limit)
    except UnsafeQueryError as exc:
        return ServiceResponse(400, {"error": str(exc), "code": "unsafe_query"})
    except QueryBusyError:
        return ServiceResponse(429, {"error": "query is busy", "code": "query_busy"})
    except QueryTimeoutError:
        return ServiceResponse(504, {"error": "query timed out", "code": "query_timeout"})
    except QueryExecutionError:
        return ServiceResponse(
            400, {"error": "query execution failed", "code": "query_execution_failed"}
        )
    return ServiceResponse(
        200,
        {
            "columns": list(result.columns),
            "column_meta": cast(list[JsonValue], describe_columns(result.column_meta, semantics)),
            "rows": list(result.rows),
            "truncated": result.truncated,
            "execution_ms": result.execution_ms,
            "startup_ms": result.startup_ms,
            "engine_execution_ms": result.engine_execution_ms,
        },
    )


__all__ = ["QueryApiService", "execute_query", "query_request_from_body"]
