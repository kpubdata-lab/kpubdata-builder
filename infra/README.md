# KPubData Builder — Azure 배포 (Bicep)

Azure Container Apps + Azure Files + Log Analytics 최소 배포 템플릿.

> **OCI + CUBRID 배포**를 찾는다면 [`infra/oci/`](./oci/)를 참조하세요 — Compute VM 단일
> 인스턴스에 builder + CUBRID 를 Docker Compose 로 띄우는 구성입니다(ADR 0016).

## 전제

- **단일 replica** (`minReplicas: 1, maxReplicas: 1`) — ADR 0010(#375) 상태 백엔드가 로컬 FS/SQLite라 replica 확장 불가.
- Builder는 **internal ingress** (공개 노출 없음, ADR 0009).
- API 키는 시크릿으로 주입 (fail-closed).

## 배포

```bash
# 리소스 그룹 생성 (처음 1회)
az group create --name kpubdata-builder-rg --location koreacentral

# 배포
az deployment group create \
  --resource-group kpubdata-builder-rg \
  --template-file infra/main.bicep \
  --parameters \
    imageName=ghcr.io/kpubdata-lab/kpubdata-builder:latest \
    apiKey=$(az keyvault secret show --vault-name <kv> --name builder-api-key --query value -o tsv) \
    allowedOrigins=https://studio.example.com \
    containerCpu=1.0 \
    containerMemory=2Gi \
    builderMaxWorkers=4 \
    queryMaxConcurrency=1 \
    duckdbThreads=1 \
    duckdbMemoryLimit=96MB \
    duckdbMaxTempSize=1GB
```

`main.bicep`의 보수적 기본값은 단일 replica에 `1.0` vCPU/`2Gi`, HTTP worker 4개,
query child 1개, DuckDB 연결마다 thread 1·메모리 `96MB`·spill `1GB`다. 애플리케이션 자체
기본값(HTTP 10, query 2, DuckDB thread 2·`1GB`·`10GB`)보다 작게 명시하여 작은 ACA 인스턴스의
process/thread·메모리 과다 경쟁을 피한다. 비동기 build pool은 `serve`가 HTTP worker와 같은
값(`builderMaxWorkers`)으로 만든다 — 동시 build 하나는 source마다 DuckDB 연결을 하나씩(최대 4)
연다. 2Gi에서 동시 build를 줄이려면 `builderMaxWorkers`를 낮춘다(1 vCPU / 2 GiB 예시는 2). 산정과 튜닝 절차는
[`docs/deploy.md`](../docs/deploy.md#9-리소스-예산과-튜닝)를 따른다.

## 리소스

| 리소스 | 용도 |
| :--- | :--- |
| Storage Account + File Share | `/data` 영속 볼륨 (산출물 + BuildIndex) |
| Log Analytics Workspace | 구조화 로그 수집 (request_id 추적, #379) |
| Container Apps Environment | ACA 실행 환경 |
| Container App | Builder serve 컨테이너 |

## 제약

- Azure Files 위 SQLite는 파일 잠금 불안정 — ADR 0010 이행 전까지 replica 1 고정.
- GHCR 이미지가 필요 (#376 — 토큰 `workflow` 스코프 대기 중).
