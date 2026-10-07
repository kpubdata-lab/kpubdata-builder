#!/bin/sh
# KPubData Builder 컨테이너 진입점 (#320, ADR 0006).
#
# 환경변수를 `serve` CLI 플래그로 변환한다. 핵심은 ADR 0006의 fail-closed 정책을
# 컨테이너 경계에서 강제하는 것 — service/app.py의 "키 미설정 = 인증 생략" 동작은
# 로컬 개발 편의 전용이며 컨테이너로 누출되지 않아야 한다. 따라서 인증 수단이 하나도
# 없으면 (명시적 dev 플래그가 없는 한) 기동을 거부한다.
#
# 인증 수단은 둘 중 하나 이상이다 (#1122):
#   - KPUBDATA_BUILDER_API_KEY — 서비스 키(스케줄 워크플로 등). 설정했다면 알려진 예시 값이나
#     32자 미만은 거부한다. 값은 어디에도 출력하지 않는다.
#   - OIDC_ISSUER — 사람 사용자 로그인. 나머지 OIDC 설정(audience, allowlist 또는 관리자,
#     pyjwt)은 `serve` 가 기동 시 검사하고 잘못되면 기동을 거부한다(validate_oidc_config).
#     그래서 OIDC 만 쓰는 배포는 API 키 없이 뜬다.
#
# dev-mode 플래그 이름은 service/app.py:_is_dev_mode()가 읽는
# KPUBDATA_BUILDER_DEV_MODE와 동일해야 한다 (#371). dev-mode 와 OIDC 를 함께 켜면
# `serve` 가 거부한다(validate_dev_mode) — 여기서 dev-mode 로 그 검사를 건너뛰지 않는다.
set -eu

# app.py의 _is_dev_mode()와 동일하게 'true'/'1'을 대소문자 무관으로 받는다.
case "${KPUBDATA_BUILDER_DEV_MODE:-}" in
  [Tt][Rr][Uu][Ee] | 1) _dev_mode=1 ;;
  *) _dev_mode=0 ;;
esac

# service/auth.py:_oidc_issuers() 와 같이 쉼표로 나눈 항목 중 비어 있지 않은 것이 있으면 켜진 것.
case "$(printf '%s' "${OIDC_ISSUER:-}" | tr -d ' \t,')" in
  "") _oidc=0 ;;
  *) _oidc=1 ;;
esac

_api_key="${KPUBDATA_BUILDER_API_KEY:-}"

if [ -n "$_api_key" ]; then
  # 문서·예시 파일에 실린 값(.env.app.example, docs/deployment.md, infra/cubrid/.env.example)은
  # 공개된 값이다. 바꾸지 않고 배포하면 누구나 서비스 키를 안다.
  case "$_api_key" in
    replace-with-strong-random-api-key | change-me-strong-secret | your-secret-key | "<secret>")
      echo "kpubdata-builder: KPUBDATA_BUILDER_API_KEY is an example value from the documentation; refusing to start." >&2
      echo "  generate one: python -c \"import secrets; print(secrets.token_urlsafe(32))\"" >&2
      exit 1
      ;;
  esac
  if [ "${#_api_key}" -lt 32 ]; then
    echo "kpubdata-builder: KPUBDATA_BUILDER_API_KEY is shorter than 32 characters; refusing to start." >&2
    echo "  generate one: python -c \"import secrets; print(secrets.token_urlsafe(32))\"" >&2
    exit 1
  fi
elif [ "$_oidc" != "1" ] && [ "$_dev_mode" != "1" ]; then
  echo "kpubdata-builder: no authentication is configured (fail-closed, ADR 0006)." >&2
  echo "  set OIDC_ISSUER (and OIDC_AUDIENCE, an allowlist or KPUBDATA_BUILDER_ADMIN_SUBJECTS)" >&2
  echo "  for signed-in users, and/or KPUBDATA_BUILDER_API_KEY=<secret> for a service key;" >&2
  echo "  KPUBDATA_BUILDER_DEV_MODE=1 only for unauthenticated local use." >&2
  exit 1
fi
unset _api_key

exec kpubdata-builder serve \
  --host "${KPUBDATA_BUILDER_HOST:-0.0.0.0}" \
  --port "${KPUBDATA_BUILDER_PORT:-8000}" \
  --output-dir "${KPUBDATA_BUILDER_OUTPUT_DIR:-/data}"
