# 사용자 키가 남는 지점 — 전수 조사 (BYOK-01)

기준: 2026-09-27, `kpubdata-builder` main + `kpubdata` main + `kpubdata-studio` main

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

## 조사 결과 — 16개 지점

| # | 지점 | 판정 | 근거 |
|---|---|---|---|
| 1 | Builder provider credential API | **남는다** | `routes/providers.py:45` `PUT /providers/{p}/credential` → 저장소에 씀 |
| 2 | DB credential table | **남는다** | `credentials/store.py:76` `provider_credentials(owner_id, provider, ciphertext, updated_at)` |
| 3 | encryption master key | **남는다(운영자 보유)** | `service/app.py:93` `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` |
| 4 | HF token | **남는다** | `publish_credentials.py:39` `publish-huggingface-hf-token` slot, 동일 저장소 |
| 5 | environment fallback (조회) | **남는다, 차단 불가** | `service/providers.py:108` — 아래 참조 |
| 5b | environment fallback (게시) | 남지만 **차단 가능** | `publish_credentials.py:117` `REQUIRE_OWN_PUBLISH_CREDENTIAL` |
| 6 | queue payload | 남지 않는다 | `service/jobs.py` 에 credential·token·secret 참조 0건 |
| 7 | response cache | **남는다(간접)** | 아래 참조 |
| 8 | manifest | 남지 않는다 | `spec/serializer.py:18` 이 명시 키를 `<redacted>` 로 치환. 디스크 확인함 |
| 9 | logs | **확인 못 했다** | logging redaction 필터가 **없다** (`logging.Filter` 구현 0건) |
| 10 | temp files | 남지 않는다 | `publishers/kaggle.py:55` — `KAGGLE_CONFIG_DIR` 을 빈 임시 디렉터리로 돌린다 |
| 11 | backup | **확인 못 했다** | 백업 절차가 정의되어 있지 않다. SQLite 파일을 복사하면 ciphertext 가 따라간다 |
| 12 | SQLite WAL | 남지 않는다 | credential store 는 WAL 을 쓰지 않는다. WAL 은 `store/build_index.py:134` 뿐이고 거기엔 credential 이 없다 |
| 13 | browser storage | **부분적으로 남는다** | 아래 참조 |
| 14 | reverse proxy logs | **확인 못 했다** | 배포가 정하는 영역. 권고가 문서에 없다 |
| 15 | APM / traces | 해당 없음 | APM 연동이 없다 |
| 16 | crash dump | **확인 못 했다** | Python 기본 traceback 에 지역변수는 실리지 않지만, `faulthandler`·코어덤프 설정은 배포가 정한다 |

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

## 7. response cache 가 인증을 대체한다

`kpubdata` 의 transport cache 키는 method·url·params·headers 에서 만들고,
credential 값은 **지문으로 치환된다**(`transport/cache.py:228`, `#263`). 즉 캐시
파일 안에 키 원문은 없다.

문제는 다른 데 있다. 캐시 키에 credential 지문이 들어간다면 사용자마다 캐시가
갈리지만, 그렇지 않은 파라미터 조합이면 **A 가 자기 키로 받은 응답을 B 가 같은
질의로 받는다.** 키는 새지 않지만 **데이터가 샌다** — 캐시가 authorization 을
대체하는 상태다. 이것이 #684 다.

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
