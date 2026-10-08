# syntax=docker/dockerfile:1

# KPubData Builder — serve 배포 이미지 (#320, ADR 0006).
#
# uv sync --no-sources: [tool.uv.sources]의 editable ../kpubdata 오버라이드를 무시하고
# pyproject의 PyPI 핀(kpubdata>=0.8.0,<0.9, #213)대로 kpubdata를 설치한다. 진입점은
# kpubdata-builder serve이며, 환경변수로 설정을 주입한다 (docker-entrypoint.sh).
#
# ADR 0006 결정: 컨테이너는 fail-closed로 동작한다. 인증 수단(KPUBDATA_BUILDER_API_KEY 또는
# OIDC_ISSUER, #1122)이 없으면 기동하지 않는다 (docker-entrypoint.sh에서 강제). 베이스는 pragmatic한 python-slim
# (ADR-0006 미해결 질문: distroless 대안은 후속).

# Debian 12(bookworm)로 고정한다 (#581). `python:3.12-slim` 이 최근 Debian 13(trixie)로
# 이동하면서 아직 mirror 에 배포되지 않은 OS 패키지 CVE(perl/gzip/pcre2 등)가 Trivy 스캔에
# HIGH/CRITICAL 로 잡혔다 — 성숙한 stable 인 bookworm 은 해당 보안 패치가 이미 반영돼 있다.
FROM python:3.12-slim-bookworm

# 베이스 이미지에 포함된 Debian 패키지의 보안 패치를 적용한다.
#
# OS_PATCH_DATE 는 이 레이어의 캐시 키다 (#1006). RUN 문자열이 같으면 buildx 캐시
# (docker.yml 의 type=gha)가 패치 이전 레이어를 계속 재사용해, 수정 버전이 mirror 에 있어도
# Trivy 가 같은 CVE 를 잡는다. 새 OS 패치로 스캔이 실패하면 이 날짜를 올린다.
ARG OS_PATCH_DATE=2026-10-06
RUN echo "os patches as of ${OS_PATCH_DATE}" \
    && apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# uv 바이너리를 Astral 공식 이미지에서 복사한다 (pip 설치 불필요).
# 0.11.8 = 이 저장소의 uv.lock을 생성한 uv 버전.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /bin/

# 가상환경을 고정 경로에 두고, 컴파일된 바이트코드와 함께 이미지 레이어로 캐싱.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:${PATH}

WORKDIR /app

# 비루트로 실행한다. 예전에는 root 로 돌았다 — 컨테이너 탈출이나 임의 파일 쓰기가
# 가능한 결함이 생기면 그 권한이 그대로 공격자의 권한이 된다. 이 서비스는 /data
# 쓰기 말고는 특권이 필요 없다.
#
# uid/gid 를 고정한다. 볼륨은 컨테이너보다 오래 사는데, 재빌드마다 uid 가 바뀌면
# 기존 /data 를 읽지 못한다.
#
# The user exists before anything is installed, and installs as itself, so /app is
# builder's from the start. A `chown -R /app` after the install copied the whole
# virtual environment into one more layer (135 MB) and took most of a rebuild.
#
# /data: 빌드 산출물(아티팩트·매니페스트) 영속 볼륨의 기본 위치.
RUN groupadd --system --gid 10001 builder \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home builder \
    && mkdir -p /data \
    && chown builder:builder /app /data

# --no-sources: editable ../kpubdata 무시, PyPI 핀 사용 (#213).
# --locked: uv.lock 이 pyproject 와 어긋나면 고치지 않고 빌드를 멈춘다(CI 의
# `uv lock --check --no-sources` 와 같은 해상도).
# dev extra(mypy/pytest/ruff)는 배포 이미지에서 제외한다.
#
# EXTRAS: 배포 이미지에 포함할 optional extra 그룹(#373).
# 기본값 publish — HuggingFace/Kaggle publish 타깃이 런타임 ImportError로 실패하지 않도록.
# exporter(parquet/huggingface layout)는 duckdb/표준 라이브러리만 쓰므로 extras 없이 동작하지만,
# publisher(huggingface_hub/kaggle)는 publish extra가 필요하다.
# 여러 extra는 공백으로(예: --build-arg EXTRAS="publish parquet"), 빈 값(--build-arg EXTRAS=)이면 extra 없음.
# CUBRID 상태 백엔드(ADR 0016)로 배포하려면 cubrid extra를 포함한다:
#   --build-arg EXTRAS="publish cubrid"
# sqlalchemy-cubrid[pycubrid]는 순수 파이썬이라 python:3.12-slim에서 C 툴체인 없이 설치된다.
#
# auth: OIDC Bearer 검증에 쓰는 pyjwt (#992). 기본 이미지에 없으면 OIDC_ISSUER 를 설정한
# 배포가 기동 시점에 "pyjwt 가 없다"로 거부된다 — Studio 는 Bearer 만 보내므로, Studio 가
# 붙는 배포는 이 extra 없이는 성립하지 않는다. OIDC 를 켜지 않은 배포에는 영향이 없다.
ARG EXTRAS="publish auth"

# Dependencies first, from the manifests alone, so that a change to src/ reuses this
# layer. It used to come after `COPY src/`, and every source change reinstalled every
# dependency. The cache mount keeps uv's download cache between local builds and out
# of the image.
COPY --chown=builder:builder pyproject.toml uv.lock ./
USER builder
RUN --mount=type=cache,target=/tmp/uv-cache,uid=10001,gid=10001 \
    _flags=""; for _e in ${EXTRAS}; do _flags="$_flags --extra $_e"; done; \
    UV_CACHE_DIR=/tmp/uv-cache uv sync --locked --no-sources --no-install-project $_flags

# Then the project itself.
COPY --chown=builder:builder src/ ./src/
COPY --chown=builder:builder README.md LICENSE ./
RUN --mount=type=cache,target=/tmp/uv-cache,uid=10001,gid=10001 \
    _flags=""; for _e in ${EXTRAS}; do _flags="$_flags --extra $_e"; done; \
    UV_CACHE_DIR=/tmp/uv-cache uv sync --locked --no-sources $_flags

# Nothing runs uv after this.
USER root
RUN rm -rf /bin/uv /bin/uvx
VOLUME /data

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# KPUBDATA_BUILDER_PORT(기본 8000)가 이 포트를 가리킨다.
EXPOSE 8000

# 무인증 /healthz로 liveness probe (#372). python-slim에 curl/wget이 없으므로
# 표준 라이브러리 urllib를 사용한다. 포트는 KPUBDATA_BUILDER_PORT를 따른다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.environ.get('KPUBDATA_BUILDER_PORT','8000')+'/healthz',timeout=3)"

USER builder

ENTRYPOINT ["docker-entrypoint.sh"]
