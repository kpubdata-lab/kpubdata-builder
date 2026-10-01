"""Reading a Parquet file Builder wrote with the dtypes Builder gave it (#869, #891 review).

DuckDB has no Parquet form for some Builder dtypes: a column whose values are all null
is written as INTEGER (Parquet's null type is not available to it), a Duration as
microseconds, a zoned datetime as an instant, an Int128 as ``DECIMAL(38,0)`` or text.
So the writer records each column's Builder dtype in the file's key-value metadata
(:data:`KV_KEY`, see ``TableHandle.write_parquet``), and these readers give the columns
those dtypes back — so what a query of ``silver/table.parquet`` reports is what
``schema.json`` says, as it was when Polars wrote the file.

A file without the key (Gold, still written by Polars; anything older) is read as it
is. These readers use Polars, like the stages that call them, and go with it (#876).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import polars as pl

from .builder_kv import KV_KEY as KV_KEY
from .builder_kv import KV_NAMES_KEY as KV_NAMES_KEY

_SIMPLE: dict[str, pl.DataType] = {
    "Null": pl.Null(),
    "Boolean": pl.Boolean(),
    "Int8": pl.Int8(),
    "Int16": pl.Int16(),
    "Int32": pl.Int32(),
    "Int64": pl.Int64(),
    "Int128": pl.Int128(),
    "UInt8": pl.UInt8(),
    "UInt16": pl.UInt16(),
    "UInt32": pl.UInt32(),
    "UInt64": pl.UInt64(),
    "Float32": pl.Float32(),
    "Float64": pl.Float64(),
    "String": pl.String(),
    "Binary": pl.Binary(),
    "Date": pl.Date(),
    "Time": pl.Time(),
}


class _Parser:
    """A recursive-descent reader of Builder dtype strings (``dtypes.canonical_dtype``)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def parse(self) -> pl.DataType:
        dtype = self._dtype()
        if self.pos != len(self.text):
            raise ValueError(f"unexpected text in dtype {self.text!r} at {self.pos}")
        return dtype

    def _name(self) -> str:
        start = self.pos
        while self.pos < len(self.text) and (self.text[self.pos].isalnum()):
            self.pos += 1
        return self.text[start : self.pos]

    def _expect(self, token: str) -> None:
        if not self.text.startswith(token, self.pos):
            raise ValueError(f"expected {token!r} in dtype {self.text!r} at {self.pos}")
        self.pos += len(token)

    def _until(self, stop: str) -> str:
        end = self.text.index(stop, self.pos)
        value, self.pos = self.text[self.pos : end], end
        return value

    def _quoted(self) -> str:
        quote = self.text[self.pos]
        end = self.pos + 1
        while self.text[end] != quote:
            end += 2 if self.text[end] == "\\" else 1
        literal, self.pos = self.text[self.pos : end + 1], end + 1
        return str(ast.literal_eval(literal))

    def _dtype(self) -> pl.DataType:
        name = self._name()
        if name in _SIMPLE:
            return _SIMPLE[name]
        if name == "Decimal":
            self._expect("(precision=")
            precision = int(self._until(","))
            self._expect(", scale=")
            scale = int(self._until(")"))
            self._expect(")")
            return pl.Decimal(precision, scale)
        if name in ("Datetime", "Duration"):
            self._expect("(time_unit=")
            unit = self._quoted()
            zone = None
            if name == "Datetime":
                self._expect(", time_zone=")
                zone = None if self.text.startswith("None", self.pos) else self._quoted()
                if zone is None:
                    self.pos += len("None")
            self._expect(")")
            return pl.Datetime(unit, zone) if name == "Datetime" else pl.Duration(unit)  # type: ignore[arg-type]
        if name == "List":
            self._expect("(")
            inner = self._dtype()
            self._expect(")")
            return pl.List(inner)
        if name == "Struct":
            self._expect("({")
            fields: dict[str, pl.DataType] = {}
            while not self.text.startswith("})", self.pos):
                field = self._quoted()
                self._expect(": ")
                fields[field] = self._dtype()
                if self.text.startswith(", ", self.pos):
                    self.pos += 2
            self._expect("})")
            return pl.Struct(fields)
        raise ValueError(f"unknown dtype {name!r} in {self.text!r}")


def parse_dtype(text: str) -> pl.DataType:
    """The Polars dtype a Builder dtype string names."""
    return _Parser(text).parse()


def builder_dtypes(path: Path | str) -> dict[str, str] | None:
    """The Builder dtypes a file records, or None when it records none."""
    return _mapping(path, KV_KEY)


def _mapping(path: Path | str, key: str) -> dict[str, str] | None:
    raw = pl.read_parquet_metadata(path).get(key)
    if raw is None:
        return None
    decoded = json.loads(raw)
    return {str(k): str(v) for k, v in decoded.items()} if isinstance(decoded, dict) else None


def _lazy_restore(name: str, stored: pl.DataType, wanted: pl.DataType) -> pl.Expr | None:
    column = pl.col(name)
    if stored == wanted:
        return None
    if isinstance(wanted, pl.Datetime) and isinstance(stored, pl.Datetime):
        if wanted.time_zone and stored.time_zone:
            return column.dt.convert_time_zone(wanted.time_zone)
        return None
    if isinstance(wanted, (pl.Null, pl.Duration, pl.Int128)):
        return column.cast(wanted)
    return None


def scan_builder_parquet(path: Path | str) -> pl.LazyFrame:
    """``pl.scan_parquet`` with the recorded Builder dtypes given back.

    Nested types with a Null part (``List(Null)``) and empty structs keep their stored
    form here; :func:`read_builder_parquet` rebuilds those too.
    """
    frame = pl.scan_parquet(path)
    renamed = _mapping(path, KV_NAMES_KEY)
    if renamed:
        frame = frame.rename(renamed)
    dtypes = builder_dtypes(path)
    if not dtypes:
        return frame
    stored = frame.collect_schema()
    restores = [
        expr
        for name, text in dtypes.items()
        if name in stored
        and (expr := _lazy_restore(name, stored[name], parse_dtype(text))) is not None
    ]
    return frame.with_columns(restores) if restores else frame


def read_builder_parquet(path: Path | str) -> pl.DataFrame:
    """The file as a frame with every recorded Builder dtype given back."""
    frame = scan_builder_parquet(path).collect()
    dtypes = builder_dtypes(path)
    if not dtypes:
        return frame
    rebuilt = []
    for name, text in dtypes.items():
        wanted = parse_dtype(text)
        if name in frame.columns and frame.schema[name] != wanted:
            values = frame.get_column(name).to_list()
            if isinstance(wanted, pl.Struct) and not wanted.fields:
                values = [{} if v else None for v in values]
            rebuilt.append(pl.Series(name, values, dtype=wanted))
    return frame.with_columns(rebuilt) if rebuilt else frame


def read_builder_parquet_schema(path: Path | str) -> pl.Schema:
    """``pl.read_parquet_schema`` as :func:`scan_builder_parquet` reads the file."""
    return scan_builder_parquet(path).collect_schema()


__all__ = [
    "KV_KEY",
    "builder_dtypes",
    "parse_dtype",
    "read_builder_parquet",
    "read_builder_parquet_schema",
    "scan_builder_parquet",
]
