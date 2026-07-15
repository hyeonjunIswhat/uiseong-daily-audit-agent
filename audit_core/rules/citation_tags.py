"""근거 태그 분류기 — 인용 권한 (마스터 SOP 제2부, 설계서 §4·REBUILD 회차 1).

L1~L4는 저장·처리 분류이고 실제 '인용 권한'은 이 태그가 결정한다:
  [직접적용] 결론 근거로 조문 인용 가능
  [취지참고] "~취지 참고" 형태로만 — 결론 근거 금지
  [원가적용] 원가 쟁점 전용 — 원가심사 출력 인용만, 재계산 금지
  [미분류]   어느 기준에도 안 걸림 — 인용 전 담당자 확인(SOP: 애매하면 질문)

규칙은 citation_tags.yaml로 외부화. 분류는 결정론(패턴 부분일치, 선언 순서 우선).
집행 지점:
  - orchestrator.written_review: [직접적용] 근거가 하나도 없는 지적은 '확인 필요' 강등
  - synthesizer 프롬프트: 태그 표기 주입(취지참고를 결론 근거로 못 쓰게 지시)
  - format_written_review: 인용 조문 옆 태그 표기
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from audit_core.config import get_settings

DIRECT = "직접적용"
SPIRIT = "취지참고"
COST = "원가적용"
UNCLASSIFIED = "미분류"

UNCLASSIFIED_DESC = "분류기준에 없는 자료 — 인용 전 담당자 확인 필요"


@dataclass(frozen=True)
class TagRule:
    tag: str
    desc: str
    patterns: tuple[str, ...]


class CitationTags:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().CITATION_TAGS_PATH)
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.provisional: bool = bool(raw.get("provisional", True))
        self.source: str = raw.get("source", "")
        self.rules: list[TagRule] = [
            TagRule(tag=name, desc=spec.get("desc", ""), patterns=tuple(spec.get("patterns", [])))
            for name, spec in (raw.get("tags") or {}).items()
        ]
        if not self.rules:
            raise ValueError("citation_tags.yaml에 태그 정의가 없음")

    def classify(self, ref: str) -> str:
        """조문 참조·자료명 → 태그. yaml 선언 순서대로 첫 매칭(취지참고 우선)."""
        compact = ref.replace(" ", "")
        for rule in self.rules:
            for p in rule.patterns:
                if p.replace(" ", "") in compact:
                    return rule.tag
        return UNCLASSIFIED

    def classify_all(self, refs: list[str]) -> dict[str, str]:
        return {r: self.classify(r) for r in refs}

    def desc(self, tag: str) -> str:
        for rule in self.rules:
            if rule.tag == tag:
                return rule.desc
        return UNCLASSIFIED_DESC

    def has_direct(self, refs: list[str]) -> bool:
        """결론 근거 요건 — [직접적용] 태그 근거가 1건 이상인가."""
        return any(self.classify(r) == DIRECT for r in refs)

    def demotion_reason(self, refs: list[str]) -> str | None:
        """지적을 '확인 필요'로 강등해야 하면 사유를, 아니면 None.

        인용 규율(설계서 §7 A6): 결론의 주 근거는 [직접적용] 필수. 근거가 아예
        없는 지적은 기존 1차 검증(조문 실존)이 다루므로 여기서는 '근거는 있는데
        전부 취지참고·원가적용·미분류'인 경우만 잡는다.
        """
        if not refs or self.has_direct(refs):
            return None
        tags = sorted({self.classify(r) for r in refs})
        return (
            f"결론 근거로 쓸 수 있는 [{DIRECT}] 규정 없음"
            f"(인용 근거 태그: {', '.join(tags)}) — 담당자 확인 필요"
        )


def format_tagged_refs(ref_tags: dict[str, str]) -> str:
    """'지방계약법-제22조 · 서울시편람(취지 참고)' 식 표기. 직접적용은 태그 생략."""
    parts = []
    for ref, tag in ref_tags.items():
        if tag == DIRECT:
            parts.append(ref)
        elif tag == SPIRIT:
            parts.append(f"{ref}(취지 참고)")
        elif tag == COST:
            parts.append(f"{ref}(원가심사 인용)")
        else:
            parts.append(f"{ref}(미분류·확인 필요)")
    return " · ".join(parts)
