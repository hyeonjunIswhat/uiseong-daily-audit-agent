"""의견서 초안 작성 (SPEC §3.2, 구현 5단계). 서면검토 파이프 전용.

검증을 통과한 지적후보(FLAG)·산식 불일치·법령 발췌를 받아 감사 의견서 본문을
쟁점별 IRAC(쟁점→근거→적용→소결) 구조로 초안한다. 참조 구조는 harness-100
70-legal-research의 legal-writing-methodology(질의요지/사실관계/검토의견/종합/권고).

제약:
- **초안까지만**: 확정 판정이 아니다. 면책·'AI 초안' 고지는 포매터가 결정론적으로
  부가하므로 LLM이 누락할 수 없다.
- **점수·등급 없음**: 루브릭 가중치 협의 전까지 정량 점수를 내지 않는다(제약 B2).
- **재현성**: temperature=0·seed 고정(base.py). 동일 입력 → 동일 초안.
- **폴백**: LLM 장애·스키마 실패 시 지적후보로부터 결정론 템플릿 초안을 조립한다
  (파이프 중단 방지, 서술 품질만 저하).
"""

from dataclasses import dataclass, field

from audit_core.agents.base import LLMUnavailable, OllamaClient, SchemaValidationError
from audit_core.agents.schemas import LawSearchHit, OpinionDraft, OpinionIssue
from audit_core.config import get_settings


@dataclass
class Finding:
    """synthesizer 입력 — 검증을 통과한 지적후보 1건."""

    item_id: str
    axis: str
    question: str
    evidence: str
    severity: int
    law_refs: list[str] = field(default_factory=list)
    law_text: str = ""
    # 근거 태그(SOP 제2부): ref → 직접적용|취지참고|원가적용|미분류.
    # [직접적용]이 없는 지적은 orchestrator가 여기 오기 전에 강등한다.
    ref_tags: dict[str, str] = field(default_factory=dict)


SYSTEM = (
    "너는 지방자치단체 일상감사의 의견서 작성 보조자다. 주어진 '지적후보'와 '법령 "
    "발췌'만을 근거로 감사 의견서 초안을 작성한다. 다음을 반드시 지킨다:\n"
    "- 주어진 사실과 법령 발췌 밖의 내용을 지어내지 않는다.\n"
    "- 문서에서 확인되지 않은 사실은 단정하지 말고 '~로 전제한다'로 적는다.\n"
    "- 각 쟁점의 certainty(확실성)는 근거가 조문으로 뒷받침되면 '높음', 해석 여지가 "
    "있으면 '보통', 문서 근거가 약하면 '낮음'으로 정한다. '명확'은 조문 위반이 "
    "명백할 때만 쓴다.\n"
    "- 인용 규율: '(취지 참고)' 표시가 붙은 근거는 결론의 근거로 쓰지 말고 "
    "'…한 취지 참고' 형태로만 언급한다. 결론의 근거는 표시 없는(직접적용) 조문만 쓴다.\n"
    "- 서술 규율: rule은 '조문 인용 → 본 사업 적용 → 판단' 순으로, conclusion은 "
    "결론부터 적는다. '위반·부적정·반드시' 같은 단정 표현과 의무부과 표현은 "
    "명백한 기준 저촉이 아니면 쓰지 않는다.\n"
    "- 점수나 등급을 매기지 않는다. 권고는 담당 부서가 취할 조치 위주로 적는다."
)


class Synthesizer:
    def __init__(self, client: OllamaClient | None = None, model: str | None = None):
        self.client = client or OllamaClient()
        self.model = model or get_settings().AUDIT_MODEL_SYNTH

    def _prompt(
        self,
        biz_type: str,
        findings: list[Finding],
        numeric_notes: list[str],
        search_hits: list[LawSearchHit],
        rule_notes: list[str] | None = None,
    ) -> str:
        from audit_core.rules.citation_tags import format_tagged_refs

        blocks = []
        for f in findings:
            if f.ref_tags:
                refs = f", 인용근거 {format_tagged_refs(f.ref_tags)}"
            elif f.law_refs:
                refs = f", 인용조문 {', '.join(f.law_refs)}"
            else:
                refs = ""
            law = f"\n    조문발췌: {f.law_text}" if f.law_text else ""
            blocks.append(
                f"- [{f.item_id}] (심각도 {f.severity}{refs})\n"
                f"    점검항목: {f.question}\n"
                f"    확인된 사실: {f.evidence}{law}"
            )
        findings_txt = "\n".join(blocks) if blocks else "(지적후보 없음)"
        num_txt = ("\n[산식 불일치]\n" + "\n".join(f"- {n}" for n in numeric_notes)) if numeric_notes else ""
        if rule_notes:
            num_txt += "\n[결정론 지적(계약방법·절차) — 규칙엔진 확인분, 쟁점에 반드시 포함]\n" + "\n".join(
                f"- {n}" for n in rule_notes
            )
        hit_txt = ""
        if search_hits:
            hit_txt = "\n[관련 규정·판례 후보(참고, 미검증 포함)]\n" + "\n".join(
                f"- {h.title} {h.ref}".strip() for h in search_hits
            )
        return (
            f"[사업유형] {biz_type}\n\n"
            f"[검증 통과 지적후보]\n{findings_txt}\n"
            f"{num_txt}\n{hit_txt}\n\n"
            "위 지적후보를 쟁점으로 묶어 의견서 초안을 작성하라. 지적후보가 없으면 "
            "issues를 비우고 overall에 '검토 결과 지적사항 없음'을 적는다. "
            "각 지적후보 하나가 하나의 쟁점(issue)이 되도록 하고, item_id를 title에 포함한다."
        )

    def draft(
        self,
        biz_type: str,
        findings: list[Finding],
        numeric_notes: list[str] | None = None,
        search_hits: list[LawSearchHit] | None = None,
        rule_notes: list[str] | None = None,
        skip_llm: bool = False,
    ) -> OpinionDraft:
        numeric_notes = numeric_notes or []
        search_hits = search_hits or []
        if skip_llm:
            # 시간 예산 초과(성능 규율) — 결정론 초안으로 즉시 반환(부분 결과 우선)
            return self._fallback(biz_type, findings, numeric_notes)
        try:
            return self.client.chat_json(
                model=self.model,
                system=SYSTEM,
                prompt=self._prompt(biz_type, findings, numeric_notes, search_hits, rule_notes),
                schema=OpinionDraft,
                num_predict=3072,
                stage="synth",
                timeout_s=get_settings().AUDIT_TIMEOUT_SYNTH_S,
            )
        except (SchemaValidationError, LLMUnavailable):
            return self._fallback(biz_type, findings, numeric_notes)

    def _fallback(self, biz_type: str, findings: list[Finding], numeric_notes: list[str]) -> OpinionDraft:
        """LLM 없이 지적후보를 그대로 쟁점으로 옮긴 결정론 초안."""
        issues = [
            OpinionIssue(
                title=f"[{f.item_id}] {f.question}",
                issue=f.question,
                rule=(", ".join(f.law_refs) + (f" — {f.law_text}" if f.law_text else "")) if f.law_refs else "관련 근거 확인 필요",
                application=f.evidence,
                conclusion="보완·확인이 필요한 사항으로 판단됨(자동 초안).",
                certainty="보통" if f.law_refs else "낮음",
            )
            for f in findings
        ]
        recs = [f"[{f.item_id}] 관련 근거 보완 또는 소명 요청" for f in findings]
        recs += [f"산식 불일치 확인: {n}" for n in numeric_notes]
        overall = (
            f"{biz_type} 사업 서면검토 결과 지적후보 {len(findings)}건, 산식 불일치 "
            f"{len(numeric_notes)}건이 확인됨(자동 초안 — 감사인 확인 필요)."
            if (findings or numeric_notes)
            else "검토 결과 지적사항 없음(자동 초안 — 감사인 확인 필요)."
        )
        return OpinionDraft(
            query=f"{biz_type} 사업 계약·집행의 적정성 서면검토",
            facts="문서에서 확인된 사실을 전제로 한다. 세부 사실관계는 원 문서를 따른다.",
            issues=issues,
            overall=overall,
            recommendations=recs or ["특이사항 없음"],
        )
