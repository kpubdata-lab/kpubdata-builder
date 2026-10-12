<!--
PR 제목은 Conventional Commits 형식이어야 합니다 — `PR title` 체크가 검사합니다(POLICY 2.1.3).
type: feat fix docs test perf refactor ci build chore style revert — 규칙 정본: kpubdata POLICY 2.1.3
이슈 번호는 제목이 아니라 본문에 적습니다 (Closes #123). squash 병합이 PR 번호를 붙입니다.
예) docs: add feature process flow diagrams
-->

Closes #<이슈 번호>
<!-- 이슈 없는 PR(의존성 갱신 등)은 이 줄을 지우고, 참조만 남길 땐 Refs #N. -->


## 안정화 범위 확인

<!-- 규칙 정본: kpubdata/docs/governance/POLICY.md 1.3절.
기존 사용자 여정·보안·정확성·복구를 안정화하는 작업만 허용됩니다.
새 기능/Provider/외부 연동/API/CLI/화면/예약 자동화/저장소·운영 책임은 기본 보류입니다.
"feat" 제목만 제한되는 것은 아닙니다. "fix"도 실질적 범위 확장이면 보류합니다. -->

Existing flow or risk:
<!-- 어떤 기존 기능/실제 위험을 안정화하는지, 수정 전 재현·근거를 구체적으로 기입 -->

- [ ] 기존 기능의 오류·회귀·보안·데이터 안전 또는 릴리스 안정성을 개선한다.
- [ ] 새로운 사용자 흐름·Provider·공개 API·외부 전송·상시 운영 책임을 만들지 않는다.
- [ ] 기존 테스트를 약화하지 않았으며, 필요한 소비자·통합 테스트까지 확인했다.

Scope exception: NO
<!-- scope gate가 감지한 범위 확장이 "기존 사용자 여정의 필수 안전 수정"일 때만
NO를 YES로 바꾸고 위 Existing flow or risk에 구체적인 위험·최소 수정·대안을 적습니다.
"Refs #N" / "Closes #N"과 review:R3 및 작성자가 아닌 사람의 승인이 필요합니다.
예외 선언은 그 자체로 승인이 아닙니다. -->

## 문제
<!-- 무엇이 잘못됐거나 무엇이 필요한지 — 대부분의 PR은 한두 문장이면 충분하다. -->

## 변경 내용
<!-- 무엇을 어떻게 바꿨는지 -->

## 검증
<!-- 돌린 게이트·테스트와 결과. ruff·mypy·pytest·docs strict 는 CI가 같은 걸 다시 돌리므로, 여기에는 로컬에서만 볼 수 있는 것(실측·재현·골든 비교)을 적는다. -->
