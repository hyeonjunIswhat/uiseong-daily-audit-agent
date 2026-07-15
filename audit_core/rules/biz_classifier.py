"""BusinessClassifier — 사업성격 해석 → 법정유형 매핑 (REBUILD 회차 3).

현실 문서("생성형 AI 플랫폼 구축 제안요청서")에는 법정 라벨('용역')이 없을 때가
많다. 이 계층이 행위(구축·개발·구매…)·산출물(플랫폼·서버…)·공종 신호를 읽어
일상감사 법정 유형(용역/물품/종합·비종합공사/민간보조/민간위탁)으로 접는다.

설계 원칙:
  - 이 분류기는 **유형 후보와 근거만** 낸다. 대상/비대상 최종 판정은 여전히
    결정론 룰엔진(check_target)이 한다 — LLM·분류기가 판정을 대신하지 않는다.
  - 계약방법(협상·수의)은 유형이 아니다 — contract_method 필드로만 분리 보고.
  - '설치'만으로 공사가 아니다 — 공종어+공사 신호가 있을 때만 공사 후보.
  - 혼합(물품+용역 등)은 주된 목적물을 고르되 confidence를 낮추고 사유를 남긴다.
  - 1차 결정론(신호 사전 스코어링, 오프라인·재현 가능) → 신뢰 낮을 때만
    LLM 보조(classify_with_llm — 스키마 강제, 실패 시 결정론 결과 유지).
  - 명시 라벨 경로(TargetRuleSet.detect_type_keyword)는 폴백·짧은 명령용으로 보존.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from audit_core.config import get_settings

CONFIRM = "확인필요"


@dataclass
class BusinessProfile:
    primary_type: str = CONFIRM      # 용역|물품|종합공사|비종합공사|민간자본보조|민간위탁|기타|확인필요
    subtype: str = ""
    contract_method: str = "미상"
    confidence: str = "low"          # high|medium|low
    evidence: list[str] = field(default_factory=list)
    reason: str = ""
    mixed: bool = False
    mixed_notes: str = ""
    biz_name: str = ""               # 추출한 사업명(있으면)


_NAME_LABEL = re.compile(r"[①-⑳•o○\-\d.\s]*(?:사업명칭|업무\(사업\)명|사업명|건명|계약명|과업명)")
_METHOD_PATTERNS = (
    ("협상에 의한 계약", ("협상에의한계약", "협상계약")),
    ("수의계약", ("수의계약", "수의시담")),
    ("지명경쟁입찰", ("지명경쟁", "지명입찰")),
    ("일반입찰", ("일반입찰", "일반경쟁", "경쟁입찰")),
)


class BusinessClassifier:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().BIZ_SIGNALS_PATH)
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.actions: dict[str, list[str]] = raw.get("actions", {})
        self.outputs: dict[str, list[str]] = raw.get("outputs", {})
        self.trades: dict[str, list[str]] = raw.get("trades", {})
        self.grants: dict[str, list[str]] = raw.get("grants", {})
        self.ancillary: list[str] = raw.get("ancillary", [])
        self.subtypes: dict[str, dict[str, list[str]]] = raw.get("subtypes", {})

    # ── 추출기 ───────────────────────────────────────────
    @staticmethod
    def _biz_name(doc: str) -> str:
        lines = doc.splitlines()
        for i, ln in enumerate(lines[:40]):
            if _NAME_LABEL.match(ln.replace(" ", "")) :
                rest = re.sub(r"^[①-⑳•o○\-\d.\s]*(?:사업명칭|업무\(사업\)명|사업명|건명|계약명|과업명)\s*[:：|]?", "", ln.replace(" ", ""))
                if rest:
                    return rest
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].strip():
                        return lines[j].strip()
        # 라벨이 없으면 첫 의미 줄(표제)을 사업명으로 근사
        for ln in lines[:8]:
            c = ln.strip()
            if 6 <= len(c.replace(" ", "")) <= 60 and not c.startswith("["):
                return c
        return ""

    def _detect_method(self, compact: str) -> str:
        for label, pats in _METHOD_PATTERNS:
            if any(p in compact for p in pats):
                return label
        return "미상"

    @staticmethod
    def _hits(compact: str, name_c: str, words: list[str]) -> tuple[int, list[str]]:
        """신호 점수(사업명 내 ×3)와 발견 어휘."""
        score, found = 0, []
        for w in words:
            n = compact.count(w)
            if not n:
                continue
            found.append(w)
            score += n + (3 if w in name_c else 0)
        return score, found

    # ── 분류 ─────────────────────────────────────────────
    def classify(self, doc: str) -> BusinessProfile:
        compact = doc.replace(" ", "")
        name = self._biz_name(doc)
        name_c = name.replace(" ", "")
        p = BusinessProfile(biz_name=name, contract_method=self._detect_method(compact))

        scores: dict[str, tuple[int, list[str]]] = {}
        for t in ("용역", "물품"):
            a, fa = self._hits(compact, name_c, self.actions.get(t, []))
            o, fo = self._hits(compact, name_c, self.outputs.get(t, []))
            scores[t] = (a * 2 + o, fa + fo)  # 행위가 산출물보다 강한 신호

        # 공사: '설치'만으로는 아님 — 공사 행위어 또는 (공종어 + 공사/설비/시공)
        act_g, fg = self._hits(compact, name_c, self.actions.get("공사", []))
        for trade_type, trade_words in self.trades.items():
            tw, ft = self._hits(compact, name_c, trade_words)
            if tw and (act_g or re.search(r"설비|시공", compact)):
                scores[trade_type] = (tw * 2 + act_g, ft + fg)
        if act_g and not any(t in scores for t in self.trades):
            scores["종합공사"] = (act_g, fg)  # 공종 불명의 '공사'는 종합 후보(경계=보수)

        for gtype, gwords in self.grants.items():
            g, fgr = self._hits(compact, name_c, gwords)
            if g:
                scores[gtype] = (g * 2, fgr)

        ranked = sorted(((s, t, f) for t, (s, f) in scores.items() if s > 0), reverse=True)
        if not ranked:
            p.reason = "행위·산출물·공종 신호를 찾지 못함 — 법정 라벨 직접 표기도 없음"
            return p

        top_s, top_t, top_f = ranked[0]
        p.primary_type = top_t
        p.evidence = [f"'{w}'" for w in top_f[:5]]
        p.reason = f"{'·'.join(top_f[:3])} 표현을 근거로 {top_t} 후보로 분류"

        # 혼합 판단 — 2위 신호가 1위의 40% 이상이면 혼합
        if len(ranked) > 1 and ranked[1][0] >= max(2, int(top_s * 0.4)):
            second = ranked[1]
            p.mixed = True
            p.mixed_notes = (f"{top_t}({top_s})와 {second[1]}({second[0]}) 신호가 병존 — "
                             f"주된 계약 목적물 판단 필요(근거: {'·'.join(second[2][:2])})")
        anc = [w for w in self.ancillary if w in compact]
        if anc and p.primary_type == "물품":
            p.mixed = p.mixed or False
            note = f"'{anc[0]}' 등 부수 행위 포함 — 설치·구성이 부수적인지 확인 필요"
            p.mixed_notes = (p.mixed_notes + " / " + note).strip(" /")

        # subtype
        for st, kws in self.subtypes.get(p.primary_type, {}).items():
            if not kws or any(k in compact for k in kws):
                p.subtype = st
                break

        # confidence: 사업명 안에 주 신호가 있으면 high, 혼합·본문 신호만이면 medium
        in_name = any(w in name_c for w in top_f)
        if p.mixed:
            p.confidence = "medium"
        elif in_name or top_s >= 6:
            p.confidence = "high"
        elif top_s >= 2:
            p.confidence = "medium"
        else:
            p.confidence = "low"
        return p

    # ── LLM 보조(선택) — 결정론이 low일 때만, 실패 시 결정론 결과 유지 ──
    def classify_with_llm(self, doc: str, base: BusinessProfile) -> BusinessProfile:
        try:
            from pydantic import BaseModel, Field

            class _LLMProfile(BaseModel):
                primary_type: str = Field(description="용역|물품|종합공사|비종합공사|민간자본보조|민간위탁|기타|확인필요")
                subtype: str = ""
                confidence: str = Field(description="high|medium|low")
                evidence: list[str] = Field(description="문서에서 근거가 되는 짧은 발췌 1~3개")
                reason: str = ""
                mixed: bool = False
                mixed_notes: str = ""

            from audit_core.agents.base import OllamaClient
            st = get_settings()
            r = OllamaClient().chat_json(
                model=st.AUDIT_MODEL_LIGHT,
                system=("너는 지방계약 문서의 사업성격 분류기다. 문서 발췌에서 주된 계약 "
                        "목적물이 무엇인지 판단해 법정 유형으로 접는다. 계약방법(협상·수의)은 "
                        "유형이 아니다. '설치'만으로 공사로 단정하지 않는다. 혼합이면 주된 "
                        "목적물을 고르되 mixed=true와 사유를 남긴다. evidence는 반드시 문서에 "
                        "실제로 있는 짧은 구절만 쓴다."),
                prompt=f"[문서 발췌]\n{doc[:2500]}", schema=_LLMProfile, num_predict=512,
            )
            if r.primary_type in ("용역", "물품", "종합공사", "비종합공사", "민간자본보조", "민간위탁", "기타"):
                base.primary_type = r.primary_type
                base.subtype = r.subtype or base.subtype
                base.confidence = "medium" if r.confidence == "high" else "low"  # LLM 단독 high 금지
                base.evidence = r.evidence or base.evidence
                base.reason = f"(LLM 보조) {r.reason}" if r.reason else base.reason
                base.mixed = base.mixed or r.mixed
                if r.mixed_notes:
                    base.mixed_notes = (base.mixed_notes + " / " + r.mixed_notes).strip(" /")
        except Exception:
            pass  # LLM 보조 실패 → 결정론 결과 그대로(보수)
        return base


# 법정유형 → TargetRuleSet categories 키 (동일 명칭이라 직결)
def to_target_type(p: BusinessProfile) -> str | None:
    return None if p.primary_type in (CONFIRM, "기타", "") else p.primary_type
