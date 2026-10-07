"""Test-only helpers.

The Polars modules here were Builder's engine until the DuckDB migration (ADR 0021,
#876). Builder no longer imports Polars; the tests keep them as an oracle — the frames
the DuckDB stages are compared against — and to build test tables from frames.
"""
