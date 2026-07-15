"""종결구 강도 3단 제안 + 금칙어 점검 (마스터 SOP ⑦·금지, 설계서 §7 A7).

종결구 3단:
  ① "하시기 바람"          — 사실 불일치·명백한 기준 저촉 등 조치 필요 사항만
  ② "할 필요가 있음"        — 중간
  ③ "참고하여 주시기 바람"  — 단순 참고

규칙(결정론):
  - 후보 등급은 심각도에서 오고(3→①, 2→②, 1→③), **제안은 한 단계 낮은 쪽**이
    기본값(SOP ⑦ — J5 강도 과잉 방어).
  - 예외: 결정론 검산이 잡은 사실 불일치(산식·문서 대조)는 판단이 아니라 확인이므로
    ①을 그대로 제안한다(SOP ⑤·설계서 A5.5).
  - 확정은 담당자 몫 — 시스템 임의 확정 금지(P1). 출력은 항상 '제안+사유'다.

금칙어(코드 검증 — 프롬프트 지시를 믿지 않는다, 규칙 3):
  - 명백한 위반이 아닌 사안에 "위반·부적정·반드시" 금지
  - 법적 의무 없는 권고에 의무부과 표현("하여야 한다" 등) 금지
  명백성 판단은 사람 몫이므로 검사는 flag-only — 검출 위치·발췌를 보고만 하고
  재작성하지 않는다. 담당자가 J5/J6 여부를 확정한다.
"""

import re
from dataclasses import dataclass

GRADE_ACT = "하시기 바람"
GRADE_NEED = "할 필요가 있음"
GRADE_REFER = "참고하여 주시기 바람"
GRADES = (GRADE_ACT, GRADE_NEED, GRADE_REFER)

_BY_SEVERITY = {3: GRADE_ACT, 2: GRADE_NEED, 1: GRADE_REFER}
_ONE_LOWER = {GRADE_ACT: GRADE_NEED, GRADE_NEED: GRADE_REFER, GRADE_REFER: GRADE_REFER}


@dataclass(frozen=True)
class ClosingSuggestion:
    item_id: str
    proposal: str    # 기본 제안(한 단계 낮춤 반영)
    candidate: str   # 심각도 기준 후보(담당자 상향 판단용)
    reason: str      # 제안 사유 한 줄


def suggest(item_id: str, severity: int, deterministic: bool = False) -> ClosingSuggestion:
    """지적 1건의 종결구 제안. deterministic=True는 산식·문서대조 등 사실 불일치."""
    if deterministic:
        return ClosingSuggestion(
            item_id=item_id, proposal=GRADE_ACT, candidate=GRADE_ACT,
            reason="결정론 검산이 확인한 사실 불일치 — 조치요구 등급 기본 허용(SOP ⑤)",
        )
    candidate = _BY_SEVERITY.get(severity, GRADE_REFER)
    proposal = _ONE_LOWER[candidate]
    if proposal == candidate:
        reason = f"심각도 {severity} — 최저 등급(단순 참고)"
    else:
        reason = f"심각도 {severity} 후보 '{candidate}'에서 한 단계 하향(기본값) — 상향은 담당자 판단"
    return ClosingSuggestion(item_id=item_id, proposal=proposal, candidate=candidate, reason=reason)


# ── 금칙어 점검 ──────────────────────────────────────────────

@dataclass(frozen=True)
class ForbiddenHit:
    term: str      # 검출 표현
    rule: str      # 위반 유형(강한 단정 | 의무부과)
    excerpt: str   # 검출 문맥 발췌


# 강한 단정 — 명백한 위반이 아닌 사안에 금지되는 표현
_STRONG_RE = re.compile(r"위반|부적정|반드시")
# 의무부과 — 법적 의무 없는 권고에 금지되는 표현
_DUTY_RE = re.compile(r"(?:하|해)여?야\s*(?:한다|함|할\s*것)|의무가\s*있")

_EXCERPT_SPAN = 25


def _hits(text: str, pattern: re.Pattern, rule: str) -> list[ForbiddenHit]:
    out = []
    for m in pattern.finditer(text):
        s = max(0, m.start() - _EXCERPT_SPAN)
        e = min(len(text), m.end() + _EXCERPT_SPAN)
        excerpt = ("…" if s > 0 else "") + text[s:e].replace("\n", " ") + ("…" if e < len(text) else "")
        out.append(ForbiddenHit(term=m.group(), rule=rule, excerpt=excerpt))
    return out


def scan_forbidden(text: str) -> list[ForbiddenHit]:
    """의견서 본문 텍스트에서 금칙 표현 검출(flag-only — 재작성하지 않는다)."""
    return _hits(text, _STRONG_RE, "강한 단정(명백한 위반 아닌 사안 금지)") + _hits(
        text, _DUTY_RE, "의무부과(법적 의무 없는 권고 금지)"
    )
