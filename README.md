# KPubData Builder

**KPubData Builder는 KPubData를 사용해 공공데이터를 재현 가능한 Table Snapshot으로 만드는 빌드·웨어하우스 도구입니다.**

> KPubData 제품군: [KPubData](https://github.com/yeongseon/kpubdata)는 단독으로 쓰는 공공데이터 접근 SDK이고, **KPubData Builder**는 그 공개 API를 쓰는 하위 소비자이며, [KPubData Studio](https://github.com/yeongseon/kpubdata-studio)는 Builder를 위한 시각적 작업공간입니다. 의존 방향은 Studio → Builder → KPubData 한 방향입니다. 저장소·Python 패키지·CLI 이름은 `kpubdata-builder`입니다.

`kpubdata`가 정규화한 데이터를 받아 Medallion Architecture (Bronze → Silver → Gold)를 거쳐 결과물을 만들고, Manifest로 추적 가능하게 기록합니다. BuildSpec이라는 선언형 스펙으로 같은 입력에서 같은 결과를 재현합니다.

[**📘 English**](./README.en.md)

## 왜 필요한가

공공데이터를 가져오는 것만으로는 충분하지 않습니다. 실제 데이터셋 작업에는 다음이 필요합니다.

- **명세 기반 재현성**: 같은 BuildSpec으로 같은 결과를 다시 만들 수 있어야 함
- **단계별 산출물**: Bronze(원본) → Silver(정규화) → Gold(배포 가능) 형태로 투명하게 진행
- **배포 분리**: 파일 생성과 외부 저장소 게시를 구분
- **감사 추적**: Manifest에 spec digest, 상태, artifact 메타데이터 기록

## 설치

```bash
pip install kpubdata-builder
```

또는 Docker:

```bash
docker build -t kpubdata-builder:latest .
docker run --rm -e KPUBDATA_BUILDER_API_KEY="${API_KEY}" kpubdata-builder:latest
```

## 빠른 시작

### 최소 BuildSpec

```yaml
dataset_id: weather-forecast
title: "동네예보 데이터셋"
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

### CLI 명령

```bash
# 검증
kpubdata-builder validate specs/weather.yaml

# 미리보기 (실행 없이 스키마와 샘플만)
kpubdata-builder preview specs/weather.yaml --limit 5

# 빌드 실행
kpubdata-builder build specs/weather.yaml --output-dir ./dist

# 결과 게시
kpubdata-builder publish specs/weather.yaml \
  --target huggingface \
  --destination my-org/my-dataset \
  --artifacts-dir ./dist/run-001

# HTTP 서비스 (Studio 연동용)
kpubdata-builder serve --host 0.0.0.0 --port 8000
```

## Medallion Architecture

Builder의 파이프라인은 세 단계를 거칩니다:

- **Bronze**: `kpubdata`를 통해 원시 데이터를 가져오고 source snapshot 보존
- **Silver**: Bronze를 Polars로 정제·검증·통계 계산
- **Gold**: Silver 결과를 split-ready/export-ready 패키지로 조립

실행 결과는 다음 구조로 정리됩니다:

```
build/{run_id}/
├── bronze/       # 원본 응답
├── silver/       # 정규화된 데이터
└── gold/         # 배포 가능 형태
```

## 주요 문서

| 문서 | 설명 |
|---|---|
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec 계약과 검증 규칙 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Medallion stage 설계 |
| [docs/deployment.md](./docs/deployment.md) | 배포 및 설정 가이드 |
| [API_CONTRACT.md](./API_CONTRACT.md) | HTTP API 계약 |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | 개발 환경 설정 |

## 지원 대상

Builder는 kpubdata가 지원하는 모든 Provider와 Dataset을 활용할 수 있습니다. 현황은 [kpubdata의 SUPPORTED_DATA.md](https://github.com/yeongseon/kpubdata/blob/main/SUPPORTED_DATA.md)를 참고하세요.

## KPubData Product Family

| 패키지 | 역할 |
|---|---|
| [kpubdata](https://github.com/yeongseon/kpubdata) | 공공데이터 접근·정규화 SDK (단독 사용 가능) |
| **kpubdata-builder** (KPubData Builder) | KPubData를 사용해 재현 가능한 데이터셋과 Table Snapshot을 만드는 빌드·웨어하우스 도구 |
| [kpubdata-studio](https://github.com/yeongseon/kpubdata-studio) | Builder를 위한 시각적 작업공간 |

---

## Contributing

이슈와 PR은 **한국어 또는 영어**로 환영합니다. [CONTRIBUTING.md](./CONTRIBUTING.md)를 참고하세요.
