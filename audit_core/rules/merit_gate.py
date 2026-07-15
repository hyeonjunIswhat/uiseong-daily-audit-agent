"""실익 게이트 — A6 후처리 필터 (마스터 SOP ⑥, 설계서 §7, F9 과잉지적 방어).

실익 없는 지적 3종을 의견서 본문에서 제외하고 말미에
[실익제외: 항목명·사유 한 줄]로만 목록화한다:
  ① 문서에 이미 해소 단서 존재 (예: 규격서에 "동등 이상 증빙 제시" 기재)
  ② 과소계상 방향 지적 (예산이 줄어드는 방향 — 조치 실익 없음)
  ③ 상위기관 확정공문이 존재하는 사업추진 타당성 (공문 번호 인용 후 제외)

원칙:
  - 보수적: 신호가 명확할 때만 제외. 애매하면 본문 유지(제외 누락은 무해,
    과잉 제외는 지적 누락 = F8이므로 위험 방향이 다르다).
  - 검증 가능: 제외 내역과 사유를 반드시 남긴다 — 담당자가 게이트 판단
    자체를 검증할 수 있게(설계서 A6 후처리 필터의 존재 이유).
  - 결정론: LLM 미관여, 동일 입력 → 동일 결과.
"""

import re
from dataclasses import dataclass
from typing import Protocol


class _FindingLike(Protocol):
    """merit gate가 보는 최소 형상 — synthesizer.Finding과 구조 호환."""

    item_id: str
    question: str
    evidence: str


@dataclass(frozen=True)
class MeritExclusion:
    item_id: str
    rule: str      # 해소단서 | 과소계상 | 확정공문
    reason: str    # 한 줄 사유(공문번호 등 검증 좌표 포함)


# ① 해소 단서 — 문서 측 신호와 지적 측 주제가 모두 맞아야 제외
_RESOLVED_DOC_RE = re.compile(r"동등\s*(?:규격\s*)?이상")
_SPEC_TOPIC_RE = re.compile(r"특정\s*(?:규격|상표|업체|제품)|상표\s*(?:지정|제한)|규격\s*(?:지정|제한)")

# ② 과소계상 방향 — 지적 문장 자체가 '적게 잡았다'는 방향일 때
_UNDER_RE = re.compile(r"과소\s*(?:계상|산정|반영|책정)|(?:적게|낮게)\s*(?:계상|산정|책정)")

# ③ 상위기관 확정공문 — 문서번호 패턴이 '확정·승인' 문맥의 같은 줄에 있어야 인정
_DOC_NO_RE = re.compile(r"[가-힣A-Za-z]{2,15}\s*제?\s*\d{4}\s*[-–]\s*\d+\s*호")
_CONFIRM_RE = re.compile(r"확정|승인|선정\s*통보")
_FEASIBILITY_TOPIC_RE = re.compile(r"타당성|필요성|사업\s*추진\s*(?:근거|사유)")


def _confirmed_doc_no(doc_text: str) -> str | None:
    """'확정/승인' 문맥과 같은 줄에 있는 공문번호를 찾는다(없으면 None)."""
    for line in doc_text.splitlines():
        if _CONFIRM_RE.search(line):
            m = _DOC_NO_RE.search(line)
            if m:
                return m.group().strip()
    return None


class MeritGate:
    def apply(
        self, findings: list[_FindingLike], doc_text: str
    ) -> tuple[list[_FindingLike], list[MeritExclusion]]:
        """지적후보 → (본문 유지, 실익제외 목록). 순서는 보존한다."""
        doc_no = _confirmed_doc_no(doc_text)
        has_resolved_clause = bool(_RESOLVED_DOC_RE.search(doc_text))

        kept: list[_FindingLike] = []
        excluded: list[MeritExclusion] = []
        for f in findings:
            topic = f"{f.question} {f.evidence}"

            if has_resolved_clause and _SPEC_TOPIC_RE.search(topic):
                excluded.append(MeritExclusion(
                    f.item_id, "해소단서",
                    "문서에 '동등 이상' 증빙 제시 단서가 이미 기재되어 있음",
                ))
                continue

            if _UNDER_RE.search(topic):
                excluded.append(MeritExclusion(
                    f.item_id, "과소계상",
                    "과소계상 방향 지적 — 조치 실익 없음(과다계상만 본문 대상)",
                ))
                continue

            if doc_no and _FEASIBILITY_TOPIC_RE.search(f.question):
                excluded.append(MeritExclusion(
                    f.item_id, "확정공문",
                    f"상위기관 확정공문({doc_no}) 존재 — 사업추진 타당성은 기확정",
                ))
                continue

            kept.append(f)
        return kept, excluded


def format_exclusions(excluded: list[MeritExclusion]) -> list[str]:
    """말미 목록화 서식 — [실익제외: 항목·사유 한 줄] (SOP ⑥ 형식)."""
    return [f"[실익제외: {e.item_id} · ({e.rule}) {e.reason}]" for e in excluded]
