# KPubData Builder — OCI Compute VM 배포 (CUBRID 백엔드)

OCI Compute VM 단일 인스턴스에 **builder + CUBRID** 를 Docker Compose 로 함께 띄우는 최소 구성.
CUBRID 상태 백엔드는 [ADR 0016](../../docs/adrs/0016-cubrid-state-backend.md), 배포 스토리는
[docs/deploy.md](../../docs/deploy.md) 를 따른다. (Azure Bicep 구성은 상위 [`infra/`](../) 참조 — 병존한다.)

## 전제

- **단일 replica** — 상태 백엔드가 단일 인스턴스 전제(ADR 0010/0016).
- **Builder 는 internal ingress** (공개 노출 없음, ADR 0009). Studio 는 같은 VM/VCN 에서 호출.
- **산출물 바이트는 블록 볼륨(`/data`)** 에 둔다 — CUBRID 백엔드여도 필수(쿼리 엔진이 실제
  parquet 경로를 요구, ADR 0016). CUBRID 엔 BuildIndex·credential·manifest 정본이 저장된다.

## 구성 파일

| 파일 | 용도 |
| :--- | :--- |
| `docker-compose.yml` | builder + cubrid 서비스 정의 |
| `.env.example` | 환경변수 템플릿(`cp .env.example .env`) |
| `cloud-init.yaml` | VM 부트스트랩(Docker 설치·블록 볼륨 마운트) |

## 배포 절차

### 1. 이미지 빌드·게시 (CUBRID extra 포함)

```bash
docker build --build-arg EXTRAS="publish cubrid" -t ghcr.io/kpubdata-lab/kpubdata-builder:cubrid .
docker push ghcr.io/kpubdata-lab/kpubdata-builder:cubrid
```

### 2. OCI 리소스

1. **VCN + private subnet** 생성. Builder 는 공개 서브넷/공인 IP 에 두지 않는다.
2. **Compute Instance** 생성(예: VM.Standard.E-계열 또는 A1.Flex). `cloud-init.yaml` 을
   초기화 스크립트로 지정.
3. **Block Volume** 생성 후 인스턴스에 attach(파라볼라/iSCSI). `cloud-init` 이 `/mnt/blockvol`
   에 마운트한다(디바이스 경로는 attach 방식에 맞게 `cloud-init.yaml` 에서 확인·수정).
4. **보안 목록/NSG**: 8000 포트를 공개 인터넷에 열지 않는다. Studio 가 다른 호스트면 해당
   private subnet CIDR 만 8000 인바운드 허용. 같은 VM 이면 `127.0.0.1:8000` 바인딩으로 충분.

### 3. 배포 실행

```bash
# VM 에서
cd /opt/kpubdata-builder
# docker-compose.yml 복사 후:
cp .env.example .env && $EDITOR .env      # API 키·CUBRID URL·CORS·OIDC 설정
docker login ghcr.io                       # private 이미지면
docker compose up -d
```

### 4. 검증

```bash
curl -fsS http://127.0.0.1:8000/healthz            # {"status":"ok"}
# 인증 확인(정키 200 / 무키 401)
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KPUBDATA_BUILDER_API_KEY" \
  http://127.0.0.1:8000/version
```

재시작 후에도 CUBRID(BuildIndex·credential·manifest 정본)와 `/data`(산출물·미러)가 블록 볼륨에
영속하는지 확인한다.

## 운영 주의

- **CUBRID 계정**: 기본 `dba`/빈 비밀번호는 개발용이다. 운영에서는 전용 사용자·비밀번호를 만들고
  `.env` 의 `KPUBDATA_BUILDER_CUBRID_URL` 을 갱신할 것.
- **CUBRID 이미지 버전**: `sqlalchemy-cubrid` 지원 범위(CUBRID 10.2–11.4) 내 태그로 고정한다.
  데이터 볼륨 경로/헬스체크 커맨드는 이미지 버전에 따라 다를 수 있어 배포 전 확인한다.
- **마이그레이션(FS→CUBRID)**: `kpubdata-builder rebuild-index` + FS 폴백/다음 빌드 승격
  (ADR 0016 참조).
