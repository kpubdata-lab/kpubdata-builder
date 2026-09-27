"""PII false positive context judgment assistant (AI-2, #447).

LLM provides opinion on whether PII candidates detected by QG-1(scan_pii) are false positives.
LLM is not a gate — regex holds blocking authority; LLM only offers "appears to be false
positive" opinion. human makes final exception by explicitly listing in allow_columns.

**ABSOLUTELY FORBIDDEN**: allowing just because LLM says "OK". if missed, personal data leaks.

original data values not included in prompt — only column names and kind.
data samples do not leave system, so scrubbing unnecessary (#447).
"""

from __future__ import annotations

from dataclasses import dataclass

from .pii import PiiFinding


@dataclass(frozen=True)
class PiiAdvisoryResult:
    """LLM false positive judgment result.

    attributes:
        likely_false_positives: list of column names suspected as false positives (LLM opinion).
        raw_response: LLM raw response (for UI hints).
    """

    likely_false_positives: tuple[str, ...]
    raw_response: str


def build_pii_advisory_prompt(findings: list[PiiFinding]) -> str:
    """constructs prompt passing PII detection candidates to LLM asking about false
    positives (#447).

    excludes original values — only column names and kind. regex over-detects
    (e.g., department name caught as person name) — LLM reduces false positives with context.
    """
    if not findings:
        return ""

    findings_text = "\n".join(
        f"  - 컬럼 {f.column!r}: 종류={f.kind}, 매칭 수={f.count}"
        for f in findings
        if f.column is not None
    )

    return f"""한국 공공데이터에서 정규식으로 검출된 PII 후보 컬럼 목록이다.
정규식은 과탐지할 수 있다 — 담당부서명이 인명으로 잡히거나, 코드값이
주민번호 패턴과 우연히 일치하는 식이다.

각 컬럼이 실제 PII인지 오탐인지 판정해라. 컬럼명의 의미(한국어 축약어 등)를
근거로 판단한다.

PII 후보:
{findings_text}

출력 형식: JSON 객체
{{
  "likely_false_positives": ["컬럼명1", "컬럼명2"]
}}

주의: 이 판정은 참고용이다. 최종 결정은 사용자가 한다."""


__all__ = ["PiiAdvisoryResult", "build_pii_advisory_prompt"]
