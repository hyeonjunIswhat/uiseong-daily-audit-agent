"""일상감사 대상 판별 규칙엔진 (기능 #1·#4). LLM 미관여 (DESIGN.md 4장).

판정은 우선순위 규칙 파이프라인:
  1) 선결 규칙(preconditions)을 순서대로 평가 — 제외·확인 사유가 유형·금액보다 우선
  2) 안 걸리면 유형별 기준(categories)으로 판정
     - kind=threshold: 추정가격(변경계약이면 누계) >= min_amount → TARGET
       (inclusive=false면 '초과')
     - kind=always: 금액 무관, decision 그대로(민간위탁 등 조건부)

기준·유형·금액은 target_rules.yaml 데이터로 외부화(서울시 편람·행안부 지침 기준).
세부규정 확정 시 값 교체만으로 반영. 동일 입력 → 동일 판정(결정론).
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from audit_core.config import get_settings

# 결정 코드
TARGET = "TARGET"
NOT_TARGET = "NOT_TARGET"
REVIEW = "REVIEW"
EXCLUDED = "EXCLUDED"

DECISION_LABEL = {
    TARGET: "일상감사 대상",
    NOT_TARGET: "대상 아님",
    REVIEW: "확인 필요",
    EXCLUDED: "제외 대상",
}

# 대상판정 세분유형 → 루브릭 축 선택용 그룹(공사/용역/물품/민간보조/기타).
# 대상판정은 계약방식·사업성격까지 세분하지만, 서면검토 루브릭(A~E축)은 목적물
# 기준이라 그룹으로 접어 축을 고른다.
RUBRIC_GROUP = {
    "종합공사": "공사", "비종합공사": "공사",
    "용역": "용역",
    "물품": "물품",
    "민간자본보조": "민간보조", "민간위탁": "민간위탁",
    "투융자심사신규": "기타", "예비비": "기타", "지방채": "기타", "군수선정업무": "기타",
}


def rubric_group(norm_type: str | None) -> str:
    """정규화된 대상판정 유형 → 루브릭 그룹. 미매핑은 '기타'."""
    return RUBRIC_GROUP.get(norm_type or "", "기타")


@dataclass(frozen=True)
class TargetInput:
    biz_type: str
    amount: int                          # 추정가격(부가세 제외) 권장
    method: str | None = None
    stage: str = "사전"                   # 사전 | 사후
    flags: frozenset[str] = frozenset()  # 긴급·재해복구·변경계약·실지감사대상 등
    cumulative_amount: int | None = None  # 변경·추가계약 누계

    def effective_amount(self) -> int:
        return self.cumulative_amount if self.cumulative_amount is not None else self.amount


@dataclass(frozen=True)
class TargetDecision:
    decision: str
    biz_type: str                 # 정규화된 유형(미매칭이면 입력 원문)
    amount: int                   # 판정에 쓴 금액(누계 우선)
    threshold: int | None         # 적용 기준금액(threshold 유형만)
    rule_id: str
    provisional: bool
    basis: tuple[str, ...]
    notes: tuple[str, ...]
    reason: str
    condition: str = ""           # 조건부(always) 결정의 조건 설명

    @property
    def label(self) -> str:
        return DECISION_LABEL[self.decision]

    @property
    def is_target(self) -> bool:
        return self.decision == TARGET


class TargetRuleSet:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().TARGET_RULES_PATH)
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.provisional: bool = bool(raw.get("provisional", True))
        self.source: str = raw.get("source", "")
        self.amount_basis: str = raw.get("amount_basis", "")
        self.preconditions: list[dict] = raw.get("preconditions", []) or []
        self.categories: dict[str, dict] = raw.get("categories", {}) or {}
        self.unknown_type_decision: str = raw.get("unknown_type_decision", REVIEW)
        self.aliases: dict[str, str] = raw.get("aliases", {}) or {}
        self.excluded_type_aliases: dict[str, str] = raw.get("excluded_type_aliases", {}) or {}
        self.method_aliases: dict[str, str] = raw.get("method_aliases", {}) or {}
        self.global_notes: tuple[str, ...] = tuple(raw.get("global_notes", []) or [])
        self.statutory_basis: tuple[str, ...] = tuple(raw.get("statutory_basis", []) or [])

    # ── 입력 정규화 ──────────────────────────────
    def normalize_type(self, biz_type: str) -> str | None:
        """categories 키로 정규화. 제외 유형(학술용역)은 '__EXCLUDED__:이름'으로 표식."""
        t = biz_type.strip()
        if t in self.excluded_type_aliases:
            return f"__EXCLUDED__:{self.excluded_type_aliases[t]}"
        if t in self.categories:
            return t
        return self.aliases.get(t)

    def normalize_method(self, method: str | None) -> str | None:
        if not method:
            return None
        return self.method_aliases.get(method.strip(), method.strip())

    @property
    def types(self) -> list[str]:
        return list(self.categories)

    @property
    def type_keywords(self) -> list[str]:
        """메시지에서 유형 감지용 — 정규화 키 + 별칭 + 제외유형(구체어 우선 정렬)."""
        kws = set(self.categories) | set(self.aliases) | set(self.excluded_type_aliases)
        return sorted(kws, key=len, reverse=True)  # 긴 표현 우선(전기공사 > 공사, 학술용역 > 용역)

    def detect_type_keyword(self, text: str) -> str | None:
        """자유 텍스트에서 유형 표현을 최장일치로 탐지(substring 오매칭 방지)."""
        for t in self.type_keywords:
            if t in text:
                return t
        return None

    # ── 규칙 평가 ────────────────────────────────
    def _match_precondition(self, rule: dict, inp: TargetInput, norm_type: str | None) -> bool:
        when = rule.get("when", {}) or {}
        if not when:
            return False
        if "stage" in when and inp.stage != when["stage"]:
            return False
        if "flag_any" in when and not (set(when["flag_any"]) & set(inp.flags)):
            return False
        if "type_any" in when:
            excl_name = norm_type[len("__EXCLUDED__:"):] if (norm_type or "").startswith("__EXCLUDED__:") else None
            if not (excl_name in when["type_any"] or inp.biz_type.strip() in when["type_any"]):
                return False
        return True


def check_target(inp: TargetInput, rules: TargetRuleSet | None = None) -> TargetDecision:
    if inp.amount < 0 or (inp.cumulative_amount is not None and inp.cumulative_amount < 0):
        raise ValueError("금액은 0 이상이어야 함")
    rules = rules or TargetRuleSet()

    eff = inp.effective_amount()
    norm = rules.normalize_type(inp.biz_type)

    def base(decision, threshold, rule_id, basis, reason, notes=None, condition=""):
        biz = inp.biz_type.strip()
        if norm and not norm.startswith("__EXCLUDED__:"):
            biz = norm
        elif norm and norm.startswith("__EXCLUDED__:"):
            biz = norm[len("__EXCLUDED__:"):]
        return TargetDecision(
            decision=decision, biz_type=biz, amount=eff, threshold=threshold,
            rule_id=rule_id, provisional=rules.provisional,
            basis=tuple(basis or rules.statutory_basis),
            notes=notes if notes is not None else rules.global_notes,
            reason=reason, condition=condition,
        )

    # 1) 선결 규칙 (제외·확인) — 순서대로, 첫 매칭 채택
    for rule in rules.preconditions:
        if rules._match_precondition(rule, inp, norm):
            return base(rule["decision"], None, rule.get("id", "PRE"),
                        rule.get("basis", []), rule.get("reason", ""))

    # 2) 유형 정규화 실패 → 확인필요
    if norm is None:
        return base(rules.unknown_type_decision, None, "UNKNOWN_TYPE", rules.statutory_basis,
                    f"사업유형 '{inp.biz_type}'을(를) 규칙에서 찾지 못함 — 감사팀 확인 필요")

    # (제외 유형이 선결규칙에 안 걸린 경우 방어적으로 EXCLUDED)
    if norm.startswith("__EXCLUDED__:"):
        return base(EXCLUDED, None, "EXCLUDED-TYPE", rules.statutory_basis,
                    f"{norm[len('__EXCLUDED__:'):]}은(는) 일상감사 제외 대상")

    # 3) 유형별 기준
    spec = rules.categories[norm]
    kind = spec.get("kind", "threshold")
    entry_notes = rules.global_notes + ((spec["note"],) if spec.get("note") else ())

    if kind == "always":
        return base(spec.get("decision", REVIEW), None, f"CAT-{norm}",
                    spec.get("basis", []), f"{norm} — {spec.get('condition', '조건 확인 필요')}",
                    notes=entry_notes, condition=spec.get("condition", ""))

    # threshold
    threshold = int(spec["min_amount"])
    inclusive = spec.get("inclusive", True)
    is_target = (eff >= threshold) if inclusive else (eff > threshold)
    # 문구는 비교 '결과' 기준 — "기준 5억 이상 → 대상 아님" 같은 모순 표현 방지
    cmp_word = ("이상" if inclusive else "초과") if is_target else ("미만" if inclusive else "이하")
    amount_desc = "누계 " if inp.cumulative_amount is not None else ""
    return base(
        TARGET if is_target else NOT_TARGET, threshold, f"CAT-{norm}",
        spec.get("basis", []),
        f"{norm} 추정가격 {amount_desc}{eff:,}원 — 기준 {threshold:,}원 "
        f"{cmp_word} → " + ("일상감사 대상" if is_target else "대상 아님")
        + (" (잠정 기준)" if rules.provisional else ""),
        notes=entry_notes,
    )
