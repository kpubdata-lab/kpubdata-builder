# KPubData Builder — Korea Public Data Builder

**KPubData Builder transforms raw Korean public data into validated, distributable datasets.**

It sits atop [kpubdata](https://github.com/yeongseon/kpubdata) and runs a Medallion pipeline: Bronze (raw) → Silver (typed/normalized) → Gold (exportable). BuildSpec is the declarative contract that ensures reproducibility — the same spec produces the same output.

[**📘 한국어**](./README.md)

## Why it exists

Fetching public data alone is not enough. Real dataset work requires:

- **Spec-driven reproducibility**: the same BuildSpec produces the same output every time
- **Staged transparency**: Bronze (raw) → Silver (normalized) → Gold (export-ready) progression
- **Build-publish separation**: generating files and pushing to external stores are distinct, gated steps
- **Audit trail**: Manifest records spec digest, status, and artifact metadata

## Install

```bash
pip install kpubdata-builder
```

Or Docker:

```bash
docker build -t kpubdata-builder:latest .
docker run --rm -e KPUBDATA_BUILDER_API_KEY="${API_KEY}" kpubdata-builder:latest
```

## Quick start

### Minimal BuildSpec

```yaml
dataset_id: weather-forecast
title: Weather Forecast Dataset
sources:
  - provider: datago
    dataset: village_fcst
    params:
      base_date: "20250401"
      nx: 55
      ny: 127
exports:
  - kind: markdown
    output_path: artifacts/report.md
```

### CLI commands

```bash
# Validate
kpubdata-builder validate specs/weather.yaml

# Preview (schema + samples, no execution)
kpubdata-builder preview specs/weather.yaml --limit 5

# Build
kpubdata-builder build specs/weather.yaml --output-dir ./dist

# Publish results
kpubdata-builder publish specs/weather.yaml \
  --target huggingface \
  --destination my-org/my-dataset \
  --artifacts-dir ./dist/run-001

# HTTP service (for Studio integration)
kpubdata-builder serve --host 0.0.0.0 --port 8000
```

## Medallion Architecture

The pipeline runs through three stages:

- **Bronze**: fetch raw data via kpubdata; preserve source snapshots byte-for-byte
- **Silver**: normalize and validate using Polars; compute statistics
- **Gold**: compose Silver output into split-ready, export-ready packages

Execution artifacts are organized as:

```
build/{run_id}/
├── bronze/       # raw API responses
├── silver/       # typed, normalized tables
└── gold/         # export-ready packages
```

## Key documents

| Document | Purpose |
|---|---|
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec contract and validation rules |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Medallion stage design |
| [docs/deployment.md](./docs/deployment.md) | Deployment and configuration guide |
| [API_CONTRACT.md](./API_CONTRACT.md) | HTTP API contract |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Development setup |

## Supported data

Builder leverages all Providers and Datasets that kpubdata supports. See [kpubdata's SUPPORTED_DATA.md](https://github.com/yeongseon/kpubdata/blob/main/SUPPORTED_DATA.md) for coverage.

## KPubData Product Family

| Package | Role |
|---|---|
| [kpubdata](https://github.com/yeongseon/kpubdata) | Public data access + normalization core |
| **kpubdata-builder** | Raw data → validated, distributable datasets |
| [kpubdata-studio](https://github.com/yeongseon/kpubdata-studio) | Dataset workbench UI |

---

## Contributing

Issues and pull requests welcome in **Korean or English**. See [CONTRIBUTING.md](./CONTRIBUTING.md) for details.
