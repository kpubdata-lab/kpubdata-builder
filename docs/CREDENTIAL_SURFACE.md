# 사용자 키가 남는 지점 — 전수 조사 (BYOK-01)

기준: 2026-09-27, `kpubdata-builder` main + `kpubdata` main + `kpubdata-studio` main
(17·18번과 3번 보강: 2026-09-29, builder `fbbdd16` 이후 main)

> **지금의 동작은 [`REQUEST_CREDENTIALS.md`](./REQUEST_CREDENTIALS.md) 에 있다.** 아래는
> 2026-09-27 의 조사 기록이다. 요약의 "세 가지 모두 어긋난다" 는 그때의 판정이고, 다중
> 사용자 배포에서는 #683(요청·작업 수명의 키), #684, #686, #925 로 바뀌었다 — 문서 뒤쪽의
> 날짜가 붙은 절들이 그 경과다.

이 문서는 **조사**다. 고치지 않는다 — 수정은 #682(ADR)·#683(ephemeral context)이
한다. 여기서 하는 일은 "키가 어디에 남는가" 를 빈칸 없이 적는 것이다.

## 요약

제품 원칙(POLICY 1.2)은 세 가지를 말한다.

1. **BYOK** — 모든 데이터 호출은 요청자 본인의 키로
2. **키 비저장** — 요청·작업이 도는 동안 메모리에만
3. **키 풀링 금지** — 운영자 키·다른 사용자 키·공유 캐시로 대신 호출하지 않는다

조사 결과 **세 가지 모두 현재 구현과 어긋난다.**

| | 현재 |
|---|---|
| 키 비저장 | ❌ SQLite 에 AES-GCM 암호화 후 **영속 저장** |
| 키 풀링 금지 | ❌ 데이터 조회 경로가 운영자 키로 폴백하고, **그 폴백에는 차단 스위치가 없다** |
| BYOK | ⚠️ 가능하지만 강제되지 않는다 |

가장 중요한 발견은 두 번째다. publish 경로는 #635 에서 스위치를 얻었지만
**데이터 조회 경로는 얻지 못했다.**

---

## 조사 결과 — 18개 지점

| # | 지점 | 판정 | 근거 |
|---|---|---|---|
| 1 | Builder provider credential API | **남는다** | `routes/providers.py:45` `PUT /providers/{p}/credential` → 저장소에 씀 |
| 2 | DB credential table | **남는다** | `credentials/store.py:76` `provider_credentials(owner_id, provider, ciphertext, updated_at)` |
| 3 | encryption master key | **남는다(운영자 보유)** | `service/app.py:93` `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` |
| 4 | HF token | **남는다** | `publish_credentials.py:39` `publish-huggingface-hf-token` slot, 동일 저장소 |
| 5 | environment fallback (조회) | **남는다, 차단 불가** | `service/providers.py:108` — 아래 참조 |
| 5b | environment fallback (게시) | 단일 사용자에서만 남고 **차단 가능**, 다중 사용자에서는 없다 (#925) | `publish_credentials.py` `REQUIRE_OWN_PUBLISH_CREDENTIAL`, `multi_user_mode()` |
| 6 | queue payload | 남지 않는다 | `service/jobs.py` 에 credential·token·secret 참조 0건 |
| 7 | response cache | 멀티유저 배포에선 꺼짐 (#684, 심층 방어) | 아래 참조 |
| 8 | manifest | 남지 않는다 | `spec/serializer.py:18` 이 명시 키를 `<redacted>` 로 치환. 디스크 확인함 |
| 9 | logs | 남지 않는다 — 게이트가 확인한다 (#686) | 아래 참조. 전에는 **남았다**: httpx 가 요청마다 `?serviceKey=` 가 붙은 URL 을 INFO 로 찍었다 |
| 10 | temp files | 남지 않는다 | `publishers/kaggle.py:55` — `KAGGLE_CONFIG_DIR` 을 빈 임시 디렉터리로 돌린다 |
| 11 | backup | **확인 못 했다** | 백업 절차가 정의되어 있지 않다. SQLite 파일을 복사하면 ciphertext 가 따라간다 |
| 12 | SQLite WAL | 남지 않는다 | credential store 는 WAL 을 쓰지 않는다. WAL 은 `store/build_index.py:134` 뿐이고 거기엔 credential 이 없다 |
| 13 | browser storage | **부분적으로 남는다** | 아래 참조 |
| 14 | reverse proxy logs | **확인 못 했다** | 배포가 정하는 영역. 권고가 문서에 없다 |
| 15 | APM / traces | 해당 없음 | APM 연동이 없다 |
| 16 | crash dump | **확인 못 했다** | Python 기본 traceback 에 지역변수는 실리지 않지만, `faulthandler`·코어덤프 설정은 배포가 정한다 |
| 17 | GitHub Actions 로그·artifact | 사용자 키는 **해당 없음**, 운영자 키는 남지 않는다(마스킹) | 아래 참조 |
| 18 | 설정 객체의 `__repr__` | 남지 않는다 (#686) | 두 dataclass 의 키 필드를 `repr=False` 로 뺐다 |

---

## 5. 데이터 조회 경로에 차단 스위치가 없다

```python
# service/providers.py:101
def resolve(self, owner_id: str | None, provider: str) -> ResolvedCredential:
    if self._repository is not None and owner_id is not None:
        user_value = self._repository.get_secret(owner_id, provider)
        if user_value is not None:
            return ResolvedCredential("user", user_value)
    server_value = KPubDataConfig.from_env().get_provider_key(...)
    if server_value:
        return ResolvedCredential("server", server_value)      # ← 운영자 키
    return ResolvedCredential("none", None)
```

요청자에게 저장된 키가 없으면 **운영자의 provider 키로 호출한다.** publish 쪽의
`REQUIRE_OWN_PUBLISH_CREDENTIAL` 에 해당하는 스위치가 여기에는 없다.

게시보다 조회가 더 잦은 동작이므로, 실질적으로 이 경로가 키 풀링의 주 통로다.
`#635` 가 게시 쪽만 막은 이유는 그 이슈가 게시에서 출발했기 때문이고, 조회 쪽을
검토한 결과가 아니다.

## 7. response cache 가 인증을 대체하지 않게 한다

`kpubdata` ≥0.7 의 transport cache 키는 method·url·params·headers 에서 만들고,
credential 값은 **지문으로 치환되어 키에 들어간다**(`transport/cache.py:224-227`,
kpubdata#263 — 닫힘). 민감 헤더를 실은 GET 은 아예 캐시하지 않는다. 그래서 현재
kpubdata 에서는 한 credential 로 받은 응답이 다른 credential 의 같은 질의에 돌아가지
않는다.

서비스는 **그 보장에 기대지 않는다(심층 방어).** 캐시 키 도출이 바뀌거나 어떤 파라미터
조합에서 지문이 빠지면, A 가 자기 키로 받은 응답을 B 가 같은 질의로 받게 된다 — 키는
새지 않아도 **데이터가 새고**, 캐시가 authorization 을 대체한다. 또 디스크 캐시는 배포
모드 전환을 넘어 살아남는다. #684 는 이 두 가능성을 닫는다.

**조치(#684):** 멀티유저 배포 — `OIDC_ISSUER` 또는 `ENFORCE_OWNERSHIP` 가 켜진
배포 — 에서는 서비스가 만드는 **모든** client 가 `cache=False` 로 생성된다.
개인 키가 없는 요청, catalog 조회도 예외가 아니며 `KPUBDATA_CACHE=1` 보다
우선한다. `cache` 인자를 받지 못하는 client factory 는 신뢰하지 않고 거부한다
(`service/app.py` `_create_client`). 음성 테스트는
`tests/unit/test_shared_response_cache.py` 에 있다. 단일 사용자 배포의 캐시는
그대로 둔다 — 자기 자신과 캐시를 공유하는 것은 노출이 아니다.

**여러 사람이 API 키 하나를 공유하는 배포**(Studio 의 API 키 모드 등)는 위 두 스위치로
멀티유저로 판정되지 않는다. 그런 배포에서는 `KPUBDATA_CACHE` 를 설정하지 않아 캐시를 끈다.

**캐시를 다시 켜려면** 다음을 모두 만족해야 한다.

- 캐시 키가 요청자 credential 지문으로 **분할**된다 — 같은 질의라도 키가 다르면
  다른 항목이다. 키 없는 요청(서버 기본 키)도 하나의 분할로 취급한다.
- **메모리 전용**이다 — 디스크 캐시는 프로세스·배포 모드 전환을 넘어 살아남아,
  단일 사용자 시절에 채운 응답이 멀티유저 배포에서 읽힌다.
- 위 음성 테스트가 수정 없이 통과한다.

## 13. 브라우저에 남는 것과 남지 않는 것

| | |
|---|---|
| provider credential | 남지 않는다 — `pages/ProviderPage.tsx:125` React state 뿐 |
| Kubi(LLM) 키 | **남는다** — `features/assistant/config.ts:12` `localStorage["kpubdata-assist-key"]` |

Kubi 키는 `#256` 에서 provider credential 과 **다른 BYOK 정책**으로 분리된
것이라 의도된 설계다. 다만 `#682` 가 "HF token 에도 같은 원칙을 적용할지" 를
정할 때 이것도 같은 표에 놓고 봐야 한다 — 세 종류의 키에 세 가지 정책이 있는
상태다.

LLM 전송 경로에는 스크러빙이 있다 (`features/assistant/scrub.ts`) — 키 이름
패턴 + Shannon 엔트로피 4.0. 이것은 **다른 문제**(사용자 키가 외부 LLM 사업자로
나가는 것)를 막는 장치이고, 로컬 저장과는 무관하다.

## 3. master key 는 ciphertext 와 같은 호스트에 있다

단일 VM 배포는 `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` 를 VM 의 `.env`(chmod 600)에 둔다
(자동 배포 워크플로 `deploy.yml` 이 그렇게 렌더했고, 2026-10-05 에 제거된 뒤로는 손으로 둔다).
credential SQLite 도 같은 VM 의 볼륨에 있다. 즉 **그 호스트의 파일 두 개를 읽을 수
있는 사람은 모든 사용자의 키를 복호화할 수 있다.** 암호화는 DB 파일만 유출되는
경우를 막고, 호스트 유출은 막지 않는다. 11번(backup)의 "ciphertext 와 master key 를
같은 곳에 백업하지 말 것" 은 호스트 자체에 대해서도 이미 성립하지 않는다.

제거된 `deploy.yml` 은 secret 을 원격 커맨드라인이 아니라 `bash -s` 의 stdin 으로 넘겼다 —
`ps` 로 보이지 않게 하려는 것이었다. 배포를 다시 자동화한다면 같은 방식을 지켜야 한다.

## 17. GitHub Actions 로그·artifact

**사용자 키는 Actions 에 들어가지 않는다.** Actions 가 쓰는 것은 운영자의 secret 이다:
`KPUBDATA_DATAGO_API_KEY`·`HF_TOKEN`(scheduled-*·publish-dataset),
`KPUBDATA_BUILDER_API_KEY`·`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`·SSH 키(deploy).

| 경로 | 판정 | 근거 |
|---|---|---|
| 로그 | 남지 않는다 | GitHub 이 등록된 secret 값을 로그에서 가린다. `set -x` 는 없고, deploy 는 값을 `printf %q` 로 **ssh stdin** 에만 쓴다 |
| artifact | 남지 않는다 | `actions/upload-artifact` 를 쓰는 워크플로가 **0개**다 |
| Docker 빌드 캐시 (`type=gha`) | 남지 않는다 | `docker.yml` 빌드에 secret·build-arg 로 키가 들어가지 않는다 |

**한계:** 마스킹은 등록된 값의 **원문**만 가린다. base64·URL 인코딩처럼 변형된 값은
가리지 않는다. 지금 변형해 출력하는 곳은 없지만, 그것을 확인하는 장치는 없다 —
9번과 같은 공백이고 `#686` 이 메운다.

## 18. 설정 객체의 `__repr__`

repr 은 로그 포맷(`%r`, f-string `!r`), 예외 메시지, 테스트 실패 출력, 디버거로 새어
나간다. 호출 지점을 하나하나 조심하는 것으로는 막을 수 없다 — 객체가 스스로 가려야 한다.

| 객체 | 판정 |
|---|---|
| `kpubdata.config.KPubDataConfig` | 남지 않는다 — `__repr__` 이 provider **이름**만 싣는다 (kpubdata 0.7) |
| `credentials.crypto.AesGcmCredentialCipher` | 남지 않는다 — 기본 object repr, 키 필드를 노출하지 않는다 |
| `credentials.store.SQLiteCredentialRepository` | 남지 않는다 — 경로와 cipher 객체뿐 |
| **`service.providers.ResolvedCredential`** | **남는다** — frozen dataclass 기본 repr 이 `value='<평문 키>'` 를 싣는다 |
| **`service.publish_credentials.PublishCredentialResolution`** | **남는다** — `values` Mapping(HF 토큰 평문)이 repr 에 실린다 |

지금 이 두 객체를 로그에 포맷하는 코드는 없다(`grep` 0건). 그래서 **조건부**다 — 누가
`logger.debug("%r", resolved)` 한 줄을 쓰거나 테스트가 이 객체를 비교하다 실패하면
키가 출력된다. 수정은 `field(repr=False)` 한 줄씩이고, `#686`(canary 게이트)이 이런
경로를 기계적으로 잡는 장치다.

## 9·18 이후 — canary 게이트 (#686)

`tests/unit/test_canary_leak_gate.py` 가 사용자 키 자리에 canary 를 넣고 성공·400·403·429·500·
timeout·redirect·업스트림 echo·취소·재시작을 **실제 kpubdata Client**(HTTP 계층만 mock)로 돌린 뒤,
로그(DEBUG)·작업공간의 모든 파일(SQLite·WAL 포함)·응답 본문·job registry 를 raw·URL·이중 URL·
Base64·JSON 이스케이프 다섯 가지로 검색한다. 처음 돌렸을 때 찾은 것:

| 누출 | 원인 | 조치 |
|---|---|---|
| 로그 | httpx 가 `HTTP Request: GET <url>` 을 INFO 로 기록 — data.go.kr URL 에 키가 쿼리로 실린다 | `kpubdata_builder.logging_redaction` — 모든 로그 레코드를 record factory 에서 재작성. 이름 기반(kpubdata `SENSITIVE_PARAM_KEYS`) + 열린 client 의 키 값 기반(경로 세그먼트 키) |
| bronze·silver·gold·카드·export | 요청을 되돌려주는 업스트림이면 키가 **데이터**에 들어간다 | Bronze 진입 지점에서 요청자 키 값(원문·URL 인코딩)을 `[REDACTED]` 로 치환 |

두 테스트가 **로그에 키가 있어야 한다**고 단언하고 있었다(`test_error_message_redaction.py`) — 이슈가
말한 "누출을 고정하는 테스트"의 사례다. 이제 URL 은 남고 키만 가려진다는 것을 단언한다.

게이트가 보지 않는 곳과 이유: 브라우저 저장소·HAR·스크린샷은 Studio, 프록시 로그·trace·에러 트래커·
GitHub Actions 는 배포의 영역이다(17번).

## 9·11·14·16 — "확인 못 했다" 의 의미

네 항목은 코드를 읽어서 판정할 수 없다. 배포 환경이 정하는 영역이거나
(reverse proxy, 코어덤프, 백업), 검증 장치가 없어서 단정할 수 없다(로그).

- **9. logs** — redaction 필터가 없으므로 "안 샌다" 고 말할 근거가 없다.
  개별 지점에서 조심하고 있을 수는 있지만, **그것을 확인하는 장치가 없다.**
  `#686`(canary key leakage gate)이 정확히 이 공백을 메운다.
- **11. backup** — 절차가 없다. 절차를 만들 때 ciphertext 와 master key 를
  같은 곳에 백업하지 말 것.
- **14. reverse proxy** — `docs/deploy.md` 에 권고가 없다.
- **16. crash dump** — Python 기본 traceback 은 지역변수를 싣지 않지만,
  `faulthandler` 나 OS 코어덤프는 프로세스 메모리를 남긴다.

빈칸으로 두지 않기 위해 적는다 — 이 네 항목은 **모른다** 가 결론이다.

## 부수 발견 — CLI publish 경로에 게이트가 없다

```python
# service/publish.py:726
# resolution 을 주지 않는 호출자(CLI/테스트)는 예전처럼 서버 환경만 본다.
```

HTTP 경로는 `PublishCredentialResolution` 으로 막지만, CLI 는 서버 환경변수를
그대로 쓴다. 단일 사용자 CLI 사용에서는 정상 구성이므로 결함이 아니다. 다만
`#682` 가 "환경변수 fallback 제거" 를 결정하면 **이 경로도 같이 정해야 한다** —
안 그러면 HTTP 로 막은 것을 CLI 로 우회한다.

## 다음

이 조사가 `#682`(키 비저장 ADR)의 입력이다. ADR 이 답해야 하는 것 중 이 조사가
바꾼 것:

1. "영속 저장 금지" 를 택하면 **1·2·3·4번을 전부 되돌려야 한다.** 이미 저장된
   credential 의 처리 경로가 필요하다.
2. **5번(조회 경로 폴백)을 함께 정해야 한다.** 게시만 막는 것은 절반이다.
3. scheduled build 는 키 저장을 전제한다 — 저장을 금지하면 그 기능이 성립하지
   않는다. 포기할지 예외로 둘지가 결정 항목이다.

## 조회 경로의 폴백 — 스위치가 생겼다 (2026-09-28)

이 문서가 기록한 결함 중 하나가 닫혔다.

> `service/providers.py` 의 `CredentialResolver.resolve` 가 사용자 키가 없으면
> 운영자 키로 폴백하고, `REQUIRE_OWN_PUBLISH_CREDENTIAL` 에 해당하는 것이 없다.

`KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL` 이 그것이다. 기본은 **unset** —
단일 사용자 배포가 per-user 설정 없이 그대로 동작한다.

| 상태 | 요청자에게 키 있음 | 없음 |
|---|---|---|
| 기본 (unset) | 자기 키 | **운영자 키** |
| 스위치 켬 | 자기 키 | **거부** (`source="none"`) |

`REQUIRE_OWN_PUBLISH_CREDENTIAL` 과 **같은 형태**로 만들었다. 두 스위치가 다르게
동작하면 하나만 켜 놓고 안전하다고 믿게 된다.

### 이 스위치가 막지 않는 것

`owner_id` 가 아예 없는 요청 — dev mode 의 비인증 호출 — 은 거부하지 않는다.
**찾아볼 owner 가 없으므로 거부할 대상도 없고**, 거부하면 아무것도 보호하지 못한 채
dev mode 만 깨진다. 그 문을 닫는 것은 `ENFORCE_OWNERSHIP`(`#635`)이다.

## #682 의 답 — 확정 (2026-10-01)

위 "다음" 과 13·부수 발견 절이 `#682` 에 넘긴 질문은 ADR 0020 이 답했고, 소유자가
2026-10-01 에 D1 에서 따라 나온 항목을 확인했다. 다중 사용자 모드에서:

- HF 토큰(publish 토큰)도 provider 키와 같은 규칙이다 — 저장·서버 환경변수 폴백 없이
  요청·작업 수명만 (ADR 0020 항목 2)
- 서버 환경변수·운영자 키 폴백은 없다 (항목 3·4). 라이브러리·CLI 는 사용자 자신의
  프로세스이므로 이 규칙 밖이다
- 스케줄 빌드는 단일 사용자 전용이다 (항목 7)
- 이미 저장된 credential 은 읽지 않고, ADR 0020 의 "기존 저장 credential" 절차로 지운다
  (항목 9)

publish 경로도 이를 따른다(#925) — 다중 사용자 모드에서 publish 토큰은 요청의
`X-Publish-Credential` 헤더로만 받고, 저장된 `publish-*` 슬롯은 읽지 않으며, 서버
`HF_TOKEN`·`KAGGLE_*` 로는 `REQUIRE_OWN_PUBLISH_CREDENTIAL` 값과 무관하게 내려가지 않는다.
reconcile 의 원격 확인도 요청의 토큰만 쓴다.

