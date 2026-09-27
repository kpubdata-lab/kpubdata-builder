"""drift root cause interpretation (AI-3, #448).

requests root cause hypotheses from LLM for drift detected by DRIFT-1(detect_drift).
detection is deterministic; only interpretation from LLM is added.

suggestions are shown as advisory only in alert message — does not affect gate decision.
"""

from __future__ import annotations

from .drift import DriftFinding


def build_drift_advisory_prompt(findings: list[DriftFinding]) -> str:
    """prompt that passes drift detection results to LLM and requests root cause hypotheses (#448).

    distinguishing "upstream schema change with different column names" vs "normal changes with
    new region codes" reduces alert fatigue.
    """
    if not findings:
        return ""

    findings_text = "\n".join(
        f"  - 종류={f.kind}, 컬럼={f.column or '테이블 전체'}, 상세={f.detail}" for f in findings
    )

    return f"""한국 공공데이터 파이프라인에서 드리프트가 감지되었다.
각 드리프트의 원인을 분류하고 설명해라.

드리프트 목록:
{findings_text}

각 드리프트에 대해 원인을 다음 중 하나로 분류해라:
- upstream_schema_change: 상류 API가 스키마를 변경
- data_growth: 정상적인 데이터 증가/감소 (append-only 등)
- api_error: 일시적 API 오류 또는 부분 응답
- unknown: 원인 불명

출력 형식: JSON 배열
[
  {{
    "kind": "column_added",
    "column": "...",
    "cause": "upstream_schema_change",
    "explanation": "..."
  }}
]

주의: 이 해석은 참고용이다. 게이트 판정에 영향을 주지 않는다."""


__all__ = ["build_drift_advisory_prompt"]
