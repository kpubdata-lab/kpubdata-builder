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

Silver now runs on DuckDB and this baseline is what it was checked against. One class of
difference was accepted when regenerating: DuckDB cannot write Parquet's null logical
type, so a column whose values are all null is stored in `silver/table.parquet` as
INTEGER and reads back as `Int32`. Its Builder dtype is still `Null` in `schema.json`,
and every other Silver, Gold, export and query output is unchanged.
