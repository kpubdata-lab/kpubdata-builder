# Air Quality Dataset — Setup & Publish Guide

> **Publishing is stopped (owner decision, 2026-09-30, #759).** The source is KOGL
> Type 3 (attribution, no derivatives). `scheduled-air-quality.yml` stays disabled and
> must not be re-enabled. The card licence is `license: other` /
> `license_name: kogl-type-3` in `scripts/configs/air_quality.yaml`; correcting the card
> already on Hugging Face is the owner's step (see "Correcting the published card").

## Prerequisites

### 1. API Key Registration

The air quality dataset uses the **한국환경공단_대기오염정보 조회서비스** API.
You must register for this specific service on data.go.kr:

1. Go to [대기오염정보 조회서비스](https://www.data.go.kr/data/15073861/openapi.do)
2. Click **활용신청** (Apply for usage)
3. Wait for approval (usually instant for open APIs)
4. Copy the approved API key

### 2. Environment Variables

```bash
# Set the API key (same key used by kpubdata)
export KPUBDATA_DATAGO_API_KEY="your-approved-api-key"

# Set HuggingFace token for publishing
export HF_TOKEN="hf_your_token_here"
```

## Publish Steps

### Dry Run (validate without uploading)

```bash
python scripts/publish_to_hf.py scripts/configs/air_quality.yaml --dry-run
```

### Actual Publish

```bash
python scripts/publish_to_hf.py scripts/configs/air_quality.yaml
```

## Config Reference

- Config file: `scripts/configs/air_quality.yaml`
- HF repo: `kpubdata/air-quality`
- Stations: 종로구, 강남구, 서초구, 마포구, 영등포구
- Metrics: PM10, PM2.5, O3, NO2, CO, SO2, KHAI

## Troubleshooting

### 401 Unauthorized
Your API key is not registered for this service. Apply at the URL above.

### Empty Results
Some stations may return empty data for certain time periods.
This is normal — the pipeline handles it gracefully.

## Scheduled Updates

`.github/workflows/scheduled-air-quality.yml` is **disabled** (#759) and stays disabled.

## Correcting the published card

Owner only. This re-uploads `README.md` alone and leaves the published data files
untouched:

```bash
python scripts/publish_to_hf.py scripts/configs/air_quality.yaml --local-only
huggingface-cli upload kpubdata/air-quality ./staging/air-quality/README.md README.md \
  --repo-type dataset --commit-message "fix(card): licence is KOGL Type 3 (#759)"
```

`--local-only` still fetches from the API to render the card, so it needs
`KPUBDATA_DATAGO_API_KEY`.
