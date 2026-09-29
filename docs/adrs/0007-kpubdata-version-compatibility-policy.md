# ADR 0007 — kpubdata 버전 호환성 정책 및 핀 강화

- 상태: 승인됨(Accepted)
- 관련 이슈: #213, kpubdata#233
- 관련 문서: [CONTRIBUTING.md](../CONTRIBUTING.md) §3-1, pyproject.toml

## 결정 (승인됨)

**권고안을 채택한다.**

1. `pyproject.toml`의 kpubdata 종속성을 `>=0.1.0a0`에서 `>=0.5.0,<0.6`으로 명시적으로 핀한다.
2. CI에 하한(lower-bound) 테스트 잡을 추가하여, 선언된 최소 버전에서 테스트 스위트가 통과하는지 강제한다.
3. 호환성 정책을 문서화하여, 상위 버전(kpubdata 0.6+)에 대한 업그레이드 경로와 검증 절차를 명확히 한다.

## 배경

`kpubdata-builder`는 `kpubdata` 라이브러리의 0.5.x 시리즈 API에 의존한다:

- `Client.dataset(...).list` — 데이터셋 메타데이터 조회
- `RecordBatch` 필드 구조 — 레코드 배치의 표준화된 스키마
- 기타 0.5.x 도입된 계약 메서드

그러나 `pyproject.toml`의 종속성 선언이 `kpubdata >=0.1.0a0`으로 되어 있어, 사실상 모든 버전을 허용하고 있었다. 이는 clean install 시 0.1.x~0.4.x 버전이 설치되면 런타임 에러가 발생하는 상황을 초래했다.

## 문제

1. **하한 미준수**: 선언된 최소 버전(0.1.0a0)이 실제 요구되는 API(0.5.x)보다 낮아, 의존성 해석 시 호환되지 않는 버전이 설치될 수 있다.
2. **CI 검증 부재**: 하한 버전에서 테스트가 수행되지 않아, 실제 호환성이 검증되지 않았다.
3. **상한 미정의**: 0.6+ 버전의 호환성 정책이 확정되지 않아, 의도치 않은 주요 버전이 설치되는 것을 막지 못했다.
4. **문서 누락**: 종속성 핀의 의미와 업그레이드 절차가 문서화되지 않아, 기여자가 의도를 파악하기 어려웠다.

## 결정 필요 사항

1. 하한 버전을 실제 필요한 API 버전(0.5.0)으로 올릴 것인가?
2. 상한 버전을 둘 것인가? 둔다면 어떤 형식(직역, compatible release)인가?
3. CI에서 하한 버전을 테스트할 것인가?
4. 호환성 정책을 어디에 어떻게 문서화할 것인가?

## 검토한 대안

### 대안 A — 하한만 설정 (`>=0.5.0`)
- 장점: 명시적 하한, 유연한 상한
- 단점: 0.6+ breaking change가 자동으로 설치되어 런타임 에러 위험

### 대안 B — Compatible Release (`~=0.5.0`)
- 장점: PEP 440 표준, 명시적 의미(0.5.x만 허용)
- 단점: 문법이 less explicit, CI에서 하한 추출이 어려울 수 있음

### 대안 C — 명시적 하한/상한 (`>=0.5.0,<0.6`)
- 장점: 의도가 가장 명확, CI 스크립트에서 파싱 용이
- 단점: 상한 변경 시 수동 갱신 필요 (하지만 이는 의도된 것: 의식적 업그레이드 강제)

### 대안 D — 하한 테스트 방식

**D-1. 전체 lowest-direct resolution**
```bash
uv pip install --resolution=lowest-direct
```
- 단점: 다른 종속성(예: `pyyaml>=6.0,<7`)이 하한으로 해석되어 빌드 실패

**D-2. kpubdata만 하한으로 고정**
```bash
uv sync --no-sources && uv pip install "kpubdata==$floor" --no-deps
```
- 장점: 다른 종속성은 정상 해석, kpubdata만 하한 테스트
- 단점: 두 단계 필요 (정상 해석 후 kpubdata만 재설치)

## 권고 (제안)

**대안 C(명시적 핀 `>=0.5.0,<0.6`)와 대안 D-2(CI 하한 테스트)를 조합하여 채택한다.**

### 1. pyproject.toml 수정
```toml
dependencies = [
  # Builder depends on the 0.5.x kpubdata API (Client.dataset(...).list, etc.).
  # The old >=0.1.0a0 floor accepted any release and broke on clean installs that
  # resolved an older kpubdata; pin to the 0.5 line until a compat policy lands (#213).
  "kpubdata>=0.5.0,<0.6",
  ...
]
```

### 2. CI 하한 테스트 잡 추가 (`.github/workflows/ci.yml`)
```yaml
min-deps:
  name: Minimum kpubdata floor
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@...
    - name: Install uv
      uses: astral-sh/setup-uv@...
    - name: Install dependencies (normal resolution)
      run: uv sync --extra dev --no-sources
    - name: Pin kpubdata to its declared floor
      run: |
        floor=$(sed -n 's/.*kpubdata>=\([0-9][0-9.]*\).*/\1/p' pyproject.toml | head -1)
        echo "Pinning kpubdata to declared floor: $floor"
        test -n "$floor" || { echo "could not extract kpubdata floor from pyproject.toml"; exit 1; }
        uv pip install "kpubdata==$floor" --no-deps
    - name: Run tests at the floor
      run: uv run --no-sync pytest
```

### 3. 문서화 ([CONTRIBUTING.md](../CONTRIBUTING.md) §3-1 확장)
- 로컬 개발과 CI/배포 환경의 의존성 해석 차이를 명시
- 핀 범위(`>=0.5.0,<0.6`)의 의미와 이슈 #213 링크를 추가
- 하한 테스트 잡의 목적과 절차를 설명

## 영향

- **빌드**: clean install 시 최소 0.5.0이 보장되어 런타임 에러 방지
- **CI**: 매 PR/merge마다 하한 버전에서 테스트가 수행되어 실제 호환성 검증
- **문서**: 기여자가 종속성 정책과 업그레이드 절차를 명확히 이해
- **관련 이슈**: kpubdata#233(cross-repo compatibility matrix)와 연계하여, 상위 버전 업그레이드 시 ADR 갱신 절차 확립 필요

## 미해결 질문

- **kpubdata 0.6+ 호환성**: 0.6이 호환 가능한 breaking change 없는 minor 업그레이드인지, 신규 ADR/테스트로 검증 후 상한을 `<0.7`으로 올릴 것인가?
- **자동 상한 관리**: pyproject.toml에서 상한을 CI/CD가 자동 갱신할 것인가? (현재는 수동 갱신으로 의도적 업그레이드 강제)
- **Cross-repo compatibility matrix**: kpubdata#233의 compatibility matrix가 확정되면, 이 ADR을 어떻게 연동/갱신할 것인가?

## 갱신 — 릴리스 매트릭스 (#832, 2026-09-30)

호환성은 **릴리스된 버전**에 대한 주장이다 (Independence Rule 11·12). 하한 하나만 돌리던
`Minimum kpubdata floor` 잡을 매트릭스로 넓혔다.

- `List supported kpubdata releases` 잡이 `scripts/kpubdata_release_matrix.py` 로 pyproject 의
  `kpubdata` 범위와 PyPI JSON 을 읽어, 범위 안의 릴리스(yanked·pre-release 제외)를 모두 나열한다.
  하한이 릴리스 목록에 없거나 목록이 비면 실패한다 — 조용히 줄어든 매트릭스는 테스트하지 않은
  버전을 숨긴다.
- `kpubdata <version>` 매트릭스 잡이 각 버전을 `--no-deps` 로 설치하고 설치된 버전을 확인한 뒤
  전체 스위트를 돈다. 하한은 이 목록의 첫 항목이다.
- 필수 검사는 여전히 `CI gate` 하나다. 매트릭스 이름은 필수 검사가 아니므로 범위에 릴리스가
  더해지거나 빠져도 branch protection 은 바뀌지 않는다.
- `kpubdata main ↔ Builder` (`cross-repo-contract.yml`) 는 **조기 경보**다. kpubdata `main` 은
  릴리스가 아니므로 그 잡의 통과·실패는 호환성 계약의 근거가 아니다 (Rule 12). 거기서 깨진 것은
  다음 릴리스 전에 고칠 신호이고, 호환성은 이 매트릭스로만 말한다.
