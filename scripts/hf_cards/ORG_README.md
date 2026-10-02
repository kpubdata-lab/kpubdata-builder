# kpubdata — Korean Public Data for Everyone

Making Korean government open data accessible worldwide with a single line of code.

```python
from datasets import load_dataset

ds = load_dataset("kpubdata/seoul-apartment-trades")
df = ds["train"].to_pandas()
```

## Mission

Korean public data ([data.go.kr](https://www.data.go.kr)) is valuable but hard to access:
complex API authentication, XML responses, Korean-only documentation,
and no standard formats like Parquet or HuggingFace Datasets.

We bridge the gap — raw public data, cleaned and published as HuggingFace Datasets.
No feature engineering, no opinions. Just honest, well-documented government data ready to use.

## Principles

- **Source fidelity**: Original Korean text values preserved as-is. English column names for accessibility.
- **Schema honesty**: What's declared in the config is exactly what you get. No phantom columns, no all-null surprises.
- **Global-first documentation**: Dataset cards in English with Korean domain context explained for international users.
- **No feature engineering**: We publish clean raw data. Users add derived features (geocoding, distances, etc.) themselves — just like Kaggle.

## Available Datasets

| Dataset | Records | Period | Source | Description |
|---|---:|---|---|---|
| [seoul-apartment-trades](https://huggingface.co/datasets/kpubdata/seoul-apartment-trades) | ~234k | 2020–2024 | MOLIT via data.go.kr | Apartment sale transactions in Seoul, all 25 districts |

*More datasets coming — air quality, weather, transit, and more.*

## How It Works

```
[data.go.kr API] → [kpubdata SDK] → [kpubdata-builder pipeline] → [HuggingFace Dataset]
```

1. **[kpubdata](https://github.com/kpubdata-lab/kpubdata)** — Python SDK that handles API auth, pagination, and response parsing for Korean public data portals
2. **[kpubdata-builder](https://github.com/kpubdata-lab/kpubdata-builder)** — Pipeline that fetches, transforms, validates, and publishes datasets to HuggingFace

## Contributing

We welcome contributions! If there's a Korean public dataset you'd like to see on HuggingFace:

1. Check if the source API is available on [data.go.kr](https://www.data.go.kr)
2. Open an issue on [kpubdata-builder](https://github.com/kpubdata-lab/kpubdata-builder/issues)
3. Or submit a PR with a new dataset config (see [publishing standards](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/docs/hf-publishing-standards.md))

## License

Datasets are published under licenses compatible with their original government data licenses.
Most Korean public data uses 공공누리 (Korea Open Government License), mapped to CC-BY-4.0.

See individual dataset cards for specific licensing details.
