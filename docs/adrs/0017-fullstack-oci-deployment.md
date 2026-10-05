# ADR 0017 — 풀스택 배포 토폴로지: OCI 단일 VM(Builder) + Cloudflare Pages(Studio)

- 상태: 제안됨(Proposed) — **ADR 0016 수용 이후 범위 축소됨**. 2026-10-05: `app-01` 에 배포하지 않기로 해서 자동 배포 워크플로(`.github/workflows/deploy.yml`)와 `ops/deploy/app-01-rollout.sh` 를 제거했다. 아래 본문의 그 두 산출물은 더 이상 저장소에 없다.
- 관련 이슈: —
- 관련 문서: [ADR 0006 — 서비스 인증 & 배포(Docker)](./0006-service-auth-and-deployment.md), [ADR 0010 — ArtifactStore 상태 백엔드](./0010-artifactstore-state-backend.md), [ADR 0016 — CUBRID 상태 백엔드](./0016-cubrid-state-backend.md), [배포 가이드](../deploy.md), [BOUNDARY.md](../BOUNDARY.md)
- 참고: `our-tax` [ADR-0005 OCI Split-Topology](https://github.com/kpubdata-lab/our-tax/blob/main/docs/adr/0005-oci-split-topology.md)

> ## ⚠️ ADR 0016과의 관계 (2026-09-24 정정)
>
> 이 ADR의 초안은 "Builder는 외부 데이터베이스가 필요 없다"를 전제로 쓰였고,
> 상태 영속화 대안 중 **외부 RDBMS(CUBRID/Postgres)를 명시적으로 기각**했다.
> 그 사이 [ADR 0016](./0016-cubrid-state-backend.md)이 **수용됨** 상태로 머지되어
> CUBRID를 상태 백엔드이자 manifest 정본으로 확정했다. 두 문서가 정면으로
> 충돌한 채 2일 간격으로 main에 들어갔다.
>
> **정정.** 상태 영속화에 관한 한 **ADR 0016이 이긴다.** 이 ADR은 그 결정을
> 뒤집지 않으며, 아래 "상태 영속화" 절의 기각 판정은 **무효**다.
>
> 이 ADR에서 살아 있는 것은 **배포 토폴로지**뿐이다 — 단일 app VM, Cloudflare
> Pages 정적 프론트엔드, Caddy 단일 공개 진입, `/data` 영속 볼륨, SSH 기반 CD.
> 이 토폴로지는 두 백엔드 프로파일 모두에 적용된다.
>
> | 프로파일 | 상태 백엔드 | compose |
> |---|---|---|
> | `sqlite` (기본) | `/data` 매니페스트 + 파생 SQLite 인덱스 | `docker-compose.prod.app.yml` |
> | `cubrid` (ADR 0016) | CUBRID(manifest 정본) + `/data` 아티팩트 | `infra/oci/docker-compose.yml` |
>
> 즉 이 ADR은 **"ADR 0016 위의 SQLite 프로파일 + 공통 배포 토폴로지"**로 읽는다.
> CUBRID 프로파일을 쓰는 배포는 DB 호스팅(동일 VM 컨테이너 vs 별도 `db-01`)을
> 따로 정해야 하며, 그 결정은 아직 어느 ADR에도 없다 — 후속 ADR이 필요하다.

## 결정 (제안됨)

KPubData 풀스택(Builder 백엔드 + Studio 프론트엔드)을 프로덕션에 배포하기 위한 토폴로지를 다음과 같이 정한다.

1. **Studio(프론트엔드)** → **Cloudflare Pages** 정적 배포. `our-tax` 프론트엔드와 동일한 패턴. 브라우저에서 `VITE_BUILDER_API_URL`로 OCI의 Builder 백엔드를 직접 호출한다.
2. **Builder(백엔드)** → **OCI 단일 VM(`app-01`)** 위 Docker 컨테이너. GHCR 이미지 + Caddy 리버스 프록시 + 영속 `/data` 볼륨.
3. **`sqlite` 프로파일에서는 별도 DB 서버(db-01)를 두지 않는다.** (CUBRID
   프로파일은 ADR 0016을 따르며, DB 호스팅 형태는 후속 ADR에서 정한다.) `our-tax`가 CUBRID 전용 `db-01`을 분리한 것과 달리, Builder는 **외부 데이터베이스 서버가 필요 없다**(아래 "배경" 참조). 상태는 `/data` 볼륨 내부의 파일(매니페스트 + 파생 SQLite 인덱스)로 충분하다.
4. **CI/CD**는 GitHub Actions → GHCR 이미지 빌드 → SSH 배포(pull → rollout → health check). `our-tax` `deploy.yml`에서 **db-01 배포 job과 Alembic migration job, CUBRID 관련 secret을 제거**한 단순화된 형태를 사용한다.

> 근거: Builder는 상태를 외부 RDBMS가 아니라 **파일시스템(매니페스트가 source of truth, SQLite는 파생 캐시, 빌드 job 레지스트리는 in-memory)**로 관리한다(ADR 0003, 0010). 따라서 `our-tax`의 2-VM split-topology를 그대로 복제하면 필요 없는 DB VM과 migration 파이프라인을 짊어지게 된다. 최소 운영 부담 원칙에 따라 **단일 app VM + 영속 볼륨**으로 축소한다.

## 배경

`our-tax`는 OCI Always Free 환경에서 CUBRID(`db-01`)와 FastAPI+ETL(`app-01`)을 두 VM으로 분리했다(ADR-0005). 1 GB VM에 CUBRID와 백엔드를 함께 올리면 OOM이 확정적이었기 때문이다. 이 프로젝트는 `our-tax`의 배포 방식을 참조하되, **Builder의 상태 저장 방식이 근본적으로 다르다**는 점을 반영해야 한다.

Builder의 영속성 모델(코드 확인 결과):

- **외부 DB 의존성 없음**: `pyproject.toml`에 SQLAlchemy/psycopg/CUBRID 드라이버/Alembic 등 RDBMS 의존성이 없다.
- **매니페스트가 source of truth**: 모든 빌드는 `manifest.json`을 생성하며, 이것이 결과물의 감사 기록이다.
- **SQLite는 파생 인덱스**: `store/`의 BuildIndex는 매니페스트를 스캔해 만든 **재생성 가능한 파생 캐시**다(ADR 0003, `rebuild_index()`). SQLite 파일은 `/data` 하위에 위치한다.
- **빌드 job 레지스트리는 in-memory**: 비동기 build job 상태는 프로세스 메모리에 있고(ADR 0008), 재기동 시 유실되지만 디스크의 아티팩트/매니페스트는 보존된다.
- **컨테이너는 `/data` 볼륨을 마운트**: `Dockerfile`의 `VOLUME /data`, `KPUBDATA_BUILDER_OUTPUT_DIR` 기본값 `/data`.

즉 Builder에 필요한 것은 **DB 서버가 아니라 영속 파일 볼륨 하나**다.

## 문제

- `our-tax`의 split-topology를 그대로 따르면 불필요한 `db-01` VM, CUBRID 튜닝, 4중 방어 규칙, Alembic migration one-shot job을 유지해야 한다.
- Studio는 브라우저 SPA이므로 백엔드와 배포 수명주기를 분리해야 한다(정적 호스팅 vs 컨테이너).
- Builder는 fail-closed 보안 기본값을 요구한다(ADR 0006): `KPUBDATA_BUILDER_API_KEY` 필수, CORS default-deny, credential master key 안정성. 배포 토폴로지가 이 요구를 깨지 않아야 한다.

## 결정 필요 사항

1. Builder 백엔드 호스팅 형태(단일 VM vs 2-VM vs 관리형).
2. Studio 프론트엔드 호스팅 형태(정적 호스팅 vs 동일 VM 서빙).
3. 상태 영속화 방법(외부 DB vs 파일 볼륨).
4. CI/CD 배포 순서와 secret 표면.

## 검토한 대안

### 백엔드 호스팅

- **A. OCI 단일 VM `app-01` + `/data` 볼륨 (채택)**: Builder 컨테이너 + Caddy만 실행. DB VM 불필요. 최소 운영 부담.
- **B. `our-tax` 방식 2-VM 유지**: DB VM을 백업/스토리지 용도로 유지. Builder가 외부 DB를 쓰지 않으므로 명분이 약하고 운영 부담만 증가. **기각**.
- **C. `our-tax` 기존 `app-01` VM에 합승**: 신규 VM 없이 기존 VM에 Builder 컨테이너 추가. 1 GB RAM 제약과 서비스 격리 문제로 위험. 향후 고려로 유보.

### 프론트엔드 호스팅

- **A. Cloudflare Pages (채택)**: `our-tax` 프론트엔드와 동일. TLS/CDN/WAF를 솔로 운영 부담 없이 확보. `VITE_BUILDER_API_URL`로 백엔드 지정.
- **B. 동일 VM에서 Caddy로 정적 파일 서빙**: 인프라 하나로 통합되지만 CDN/캐싱 이점 상실, VM 부하 증가. **기각**.

### 상태 영속화

- **A. `/data` 파일 볼륨 (채택)**: 매니페스트 + 파생 SQLite. Builder 설계와 정합.
- **B. 외부 RDBMS(CUBRID/Postgres) 도입**: ~~Builder에 없는 의존성을 새로 만드는 것으로, "kpubdata 로직 중복 금지 / 결정적 동작 우선" 원칙과 상충. **기각**.~~
  **이 판정은 무효다.** [ADR 0016](./0016-cubrid-state-backend.md)이 조직 요구사항으로
  CUBRID를 상태 백엔드이자 manifest 정본으로 확정했고(수용됨), 그 결정이 이
  문서의 기각 판정보다 우선한다. 위 머리말 참조. 아래 A는 `sqlite` 프로파일에서만
  기본이다.

> **주의**: `/data`는 **로컬 블록 볼륨**이어야 한다. SQLite 파일을 네트워크 파일시스템(예: Azure Files/NFS)에 두면 파일 락 불안정이 발생한다(ADR 0010 §5, 배포 가이드 §6).

## 핵심 설계 원칙

1. **Public 진입은 Caddy만 노출**. Builder는 internal `8000`, Caddy가 `80/443`을 잡고 리버스 프록시. Cloudflare proxied DNS → `app-01` → Caddy → Builder. TLS는 Cloudflare Full(strict) + Caddy origin 종단(Let's Encrypt 자동 갱신).
2. **fail-closed 유지**(ADR 0006). 컨테이너는 `KPUBDATA_BUILDER_API_KEY` 없이 기동 거부. `KPUBDATA_BUILDER_DEV_MODE`는 프로덕션에서 사용 금지.
3. **CORS default-deny**. `KPUBDATA_BUILDER_ALLOWED_ORIGINS`에 Studio의 Cloudflare Pages 오리진만 명시.
4. **credential master key 안정성**. `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`는 배포·재기동 사이에 동일 값을 재주입한다(교체 시 기존 credential 복호화 불가, ADR 0012).
5. **Secret은 VM-local `.env.app` 파일**. `chmod 600`, git 미커밋. committed compose는 `${VAR}` 참조만.
6. **배포 자동화는 GitHub Actions → GHCR → SSH**. 배포 순서: 이미지 빌드 → SSH pull → 컨테이너 rollout → `/version`(또는 헬스) 체크. **migration job 없음**(외부 DB 없음).

## 배포 산출물 (후속 구현 대상)

이 ADR이 승인되면 다음 산출물을 별도 PR로 추가한다.

- `docker-compose.prod.app.yml` — Builder + Caddy 스택(`our-tax` app 스택에서 migrate/etl/CUBRID 제거, `/data` 볼륨 유지).
- `ops/caddy/Caddyfile` — `app-01` 리버스 프록시 설정.
- `.env.app.example` — `KPUBDATA_BUILDER_API_KEY`, `KPUBDATA_BUILDER_ALLOWED_ORIGINS`, `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` 등 placeholder.
- `.github/workflows/deploy.yml` — GHCR 빌드 + SSH 배포(`our-tax` `deploy.yml`에서 `deploy-db`/`run-etl`/migration/CUBRID secret 제거).
- Studio 저장소: Cloudflare Pages 빌드 설정 + `VITE_USE_REAL_BUILDER=true`, `VITE_BUILDER_API_URL` 지정(별도 PR, studio 저장소).

## 영향

### 긍정적

- 관리 대상 인프라가 VM 1대 + Cloudflare Pages로 최소화된다.
- Builder 설계(파일 기반 상태)와 배포 토폴로지가 정합한다.
- Studio/Builder 배포 수명주기가 분리되어 프론트엔드 배포가 백엔드에 영향을 주지 않는다.
- migration 파이프라인·DB 튜닝·DB 방화벽 4중 방어가 불필요해져 운영 표면이 축소된다.
- `our-tax`의 Caddy + Cloudflare + GHCR + SSH 패턴을 재사용한다.

### 부정적 / 트레이드오프

- 단일 VM은 SPOF다. Builder는 단일 replica(ADR 0008/0010)이므로 재기동 시 in-memory job 상태가 유실된다(디스크 아티팩트는 보존).
- `/data`는 로컬 볼륨이어야 하므로 VM 재생성 시 볼륨 백업/복구 절차가 별도로 필요하다.
- Studio(Cloudflare)와 Builder(OCI)가 서로 다른 오리진이므로 CORS/인증 헤더 구성이 정확해야 한다.

## 미해결 질문

1. `app-01` VM을 신규로 만들지, `our-tax` 기존 VM에 합승할지(메모리 여유 확인 필요).
2. `/data` 볼륨 백업 주기·방식(OCI Block Volume 스냅샷 vs 오브젝트 스토리지 sync).
3. Studio ↔ Builder 인증 방식: 정적 `X-API-Key`(브라우저 노출 위험) vs OIDC Bearer(ADR 0015)를 프로덕션에서 어떤 것으로 강제할지.
4. Cloudflare Pages 프로젝트를 studio 저장소 CI에서 배포할지, 수동 연결할지.
