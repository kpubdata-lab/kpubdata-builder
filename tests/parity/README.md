# DuckDB parity baseline

The DuckDB migration (ADR 0021, #864) replaces the tabular engine without changing what users
get. This directory pins what the current Polars engine produces, so every later step can be
checked against it (#865).

- `canonical.py` turns results into an engine-neutral form: column names, Builder-canonical
  dtypes, nullability and typed values. Parquet bytes, timestamps, ids, paths and timings are
  left out, so two engines that agree logically compare equal (R12).
- `scenarios.py` defines the scenarios. Each runs the real path — `BuilderService` builds and the
  warehouse endpoints — on fixed inputs, with no network and no key.
- `tests/golden/duckdb_parity/<scenario>.json` holds each scenario's committed result.
- `test_duckdb_parity_baseline.py` compares. It never writes.

## Scenarios

| Scenario | Pins |
| --- | --- |
| `spec_seoul_apartment_trades` | `specs/seoul-apartment-trades.yaml` on recorded provider rows, plus a seeded ratio split: Bronze, Silver, Gold, split membership, every exporter |
| `spec_seoul_apartment_rent` | `specs/seoul-apartment-rent.yaml` the same way |
| `spec_seoul_bike_rent_month` | `specs/seoul-bike-rent-month.yaml` through a file upload: Korean column names, several header generations, `\N`, zfill |
| `composition_trades_rent` | Two sources joined: the composition statistics and the joined rows |
| `replay_air_station` | The fixture Builder ships, through the real kpubdata client in replay mode |
| `r01`–`r15` | The risk cases of ADR 0021: inference, strict casts, integer sums, intervals, unnamed aggregates, NULL ordering, Parquet logical equality, zfill width, introspection and path access, the SQL validator's dialect |
| `r06_decimal_to_int` | A Parquet upload's Decimal column cast to int: truncation toward zero, through every stage and exporter (#918, #979) |
| `query_workers` | SQL, rows, aggregate, profile and export on one table |

A scenario pins the current answer, including a refusal: `r09_interval` records that the
current engine rejects `INTERVAL` arithmetic, and a later step that makes it work changes that
file on purpose.

## Regenerating

```sh
python scripts/generate_duckdb_parity_baseline.py            # every scenario
python scripts/generate_duckdb_parity_baseline.py r08 query  # names starting so
python scripts/generate_duckdb_parity_baseline.py --check    # exit 1 when a file is stale
```

The generator runs each scenario twice and refuses to write when the two runs differ. Commit
a changed baseline only with the reason in the pull request: that diff is what changed for
users.

## After the Silver cutover (#869)

Silver now runs on DuckDB and this baseline is what it was checked against, unchanged.
DuckDB cannot write Parquet's null logical type, so a column whose values are all null
is stored in `silver/table.parquet` with the physical type INTEGER. The file records
every column's Builder dtype in its key-value metadata (`kpubdata_builder.dtypes`), and
Builder's readers (`tabular/builder_parquet.py`) give that dtype back, so Builder reads
the column as `Null` — the baseline included. A reader outside Builder (pyarrow,
pandas, Polars on its own) sees the physical type and reads the column as `Int32`.

## After the Gold cutover (#870)

Gold runs on DuckDB too, checked against the same baseline, unchanged. `gold/*/table.parquet`
is written the way Silver's is, with the Builder dtypes in its metadata. A composition's
rows are in the left side's order and then the right side's (ADR 0021 D8); the
`composition_trades_rent` rows came out in that order under Polars as well.

## After the split cutover (#871)

Ratio splits are `hash-sort-v2` (ADR 0021 D7), so `spec_seoul_apartment_trades` was
regenerated for its seeded split: only `splits/train/rows` and `splits/test/rows`
changed — which rows went where. The sizes (16 and 4), the rows of the two splits
together, the Gold table and every export are what they were. Key splits are unchanged.

## After the query cutover (#874)

SQL, rows, aggregates, profiles and exports run on DuckDB. The baseline was regenerated for
the differences the plan calls compatibility notes (R8–R15), each intended:

| Scenario | Before (Polars) | Now (DuckDB) |
| :--- | :--- | :--- |
| `r08_integer_sum` | `SUM` of Int64 is `int64` | `int128`; still sent as exact decimal text because the values are beyond 2**53 |
| `r09_interval` | `date + INTERVAL` failed | it works and is a `datetime` (`2024-01-31 + 1 month` is `2024-02-29`) |
| `r10_unnamed_aggregate_column` | `COUNT(*)` named `len`, `uint32`; `SUM(v), MAX(v)` failed | named `count_star()`, `int64`; `sum(v)` (`int128`) and `max(v)` work |
| `r11_null_sorting` | SQL `ORDER BY v DESC` put nulls first | nulls last in both directions, as rows and aggregates always did |
| `r14_introspection_and_path_leakage` | `current_setting(...)` refused for not reading `dataset` | refused first for the function itself |
| `query_workers` | `COUNT(*)` `uint32` | `int64` |

`query_workers`' export orders by `aptNm, dealAmount` instead of `aptNm` alone: SQL leaves
the order of tied rows undefined, and DuckDB's sort does not keep it from one version to the
next (1.2 and 1.5 differ), so the scenario now orders completely.

Two differences no scenario pins: a column that is Null in Builder is `int32` (every value
null) in SQL results, since DuckDB types a projected NULL INTEGER — a page of rows still
reports it Null; and a zoned datetime is sent in UTC.
