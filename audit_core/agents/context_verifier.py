"""검증 2차: LLM 문맥검증 (SPEC §3.3). 서면검토(의견서) 파이프 전용.

1차(결정론) 검증을 통과한 지적후보만 대상으로, 인용 조문의 '내용'이 지적
'내용'과 문맥상 부합하는지 판정한다. 판정 결과는 강등에만 쓰인다:

- supports=True  → 지적 유지
- supports=False → 지적을 '확인 필요'로 강등 (자동 인용에서 제외)

이 단계는 새 지적을 만들거나 1차 결정론 판정(조문 실존·금액 대조)을 번복하지
못한다(flag-only 권한). LLM 장애·스키마 실패 시에는 보수적으로 supports=True로
두어(지적을 임의로 지우지 않음) 사람 검토로 넘긴다.
"""

from audit_core.agents.base import LLMUnavailable, OllamaClient, SchemaValidationError
from audit_core.agents.schemas import ContextCheck, ContextCheckBatch
from audit_core.config import get_settings

SYSTEM = (
    "너는 지방자치단체 일상감사의 검증 보조자다. 하나의 '지적후보'와 그것이 인용한 "
    "'법령 조문 발췌'가 주어진다. 조문의 내용이 지적 내용을 실제로 뒷받침하는지만 "
    "판정한다. 지적의 옳고 그름을 새로 판단하지 말고, 오직 '인용된 조문이 이 지적의 "
    "근거로 적절한가'만 본다.\n"
    "- supports=true: 조문 내용이 지적의 근거로 부합한다\n"
    "- supports=false: 조문이 지적과 무관하거나 근거로 부적절하다\n"
    "reason에는 그 판단의 근거를 한 문장으로 적는다."
)


class ContextVerifier:
    def __init__(self, client: OllamaClient | None = None, model: str | None = None):
        self.client = client or OllamaClient()
        self.model = model or get_settings().AUDIT_MODEL_SYNTH

    def _prompt(self, item_id: str, question: str, evidence: str, law_context: str) -> str:
        return (
            f"[지적후보 {item_id}]\n"
            f"점검항목: {question}\n"
            f"지적 근거: {evidence}\n\n"
            f"[인용 법령 조문 발췌]\n{law_context}\n\n"
            f"위 조문이 이 지적의 근거로 부합하는지 판정하라. item_id는 '{item_id}'로 둔다."
        )

    def check(self, item_id: str, question: str, evidence: str, law_context: str) -> ContextCheck:
        """지적 1건의 조문-지적 문맥 부합 판정. 실패 시 보수적으로 supports=True."""
        if not law_context.strip():
            # 인용 조문 발췌가 없으면 문맥검증 불가 — 강등하지 않고 사람 몫으로.
            return ContextCheck(item_id=item_id, supports=True, reason="인용 조문 발췌 없음 — 문맥검증 미수행")
        try:
            result = self.client.chat_json(
                model=self.model,
                system=SYSTEM,
                prompt=self._prompt(item_id, question, evidence, law_context),
                schema=ContextCheck,
                num_predict=512,
                stage="verify",
                timeout_s=get_settings().AUDIT_TIMEOUT_VERIFY_S,
            )
        except (SchemaValidationError, LLMUnavailable):
            return ContextCheck(item_id=item_id, supports=True, reason="문맥검증 수행 불가(LLM) — 지적 유지")
        # LLM이 item_id를 바꾸는 경우 교정(강등 판단만 신뢰).
        result.item_id = item_id
        return result

    def check_all(self, candidates: list[dict]) -> list[ContextCheck]:
        """지적후보 전체를 1콜로 판정(2026-07-15 성능 규율 — 후보당 1콜 폐지).

        candidates: [{"item_id", "question", "evidence", "refs": [ref, ...]}]
        law_texts는 후보들이 공유하는 조문을 중복 없이 한 번만 싣기 위해
        {ref: 조문 발췌}로 따로 받는다 → candidates[i]["law_texts"]가 아니라
        전체에서 유일 ref만 [법령 발췌] 절에 나열하고 후보는 ref 이름으로 참조.
        실패·미회신 항목은 보수적으로 supports=True(강등 전용 원칙).
        """
        cands = [c for c in candidates if c.get("law_texts")]
        passthrough = [
            ContextCheck(item_id=c["item_id"], supports=True,
                         reason="인용 조문 발췌 없음 — 문맥검증 미수행")
            for c in candidates if not c.get("law_texts")
        ]
        if not cands:
            return passthrough

        # 동일 조문 중복 첨부 금지 — 유일 ref만 발췌 절에 1회 수록
        law_blocks: dict[str, str] = {}
        for c in cands:
            for ref, text in c["law_texts"].items():
                law_blocks.setdefault(ref, text)
        laws = "\n\n".join(f"[{ref}]\n{text}" for ref, text in law_blocks.items())
        items = "\n\n".join(
            f"- item_id: {c['item_id']}\n  점검항목: {c['question']}\n"
            f"  지적 근거: {c['evidence'][:300]}\n  인용 조문: {', '.join(c['law_texts'])}"
            for c in cands
        )
        prompt = (f"[법령 조문 발췌 — 아래 지적후보들이 공유]\n{laws}\n\n"
                  f"[지적후보 목록]\n{items}\n\n"
                  "각 지적후보에 대해, 그 후보가 인용한 조문이 지적의 근거로 부합하는지 "
                  "checks 배열로 전부 판정하라. item_id는 목록의 값을 그대로 쓴다.")
        try:
            batch = self.client.chat_json(
                model=self.model,
                system=SYSTEM,
                prompt=prompt,
                schema=ContextCheckBatch,
                num_predict=1024,
                stage="verify",
                timeout_s=get_settings().AUDIT_TIMEOUT_VERIFY_S,
            )
            by_id = {c.item_id: c for c in batch.checks}
        except (SchemaValidationError, LLMUnavailable):
            by_id = {}
        out = []
        for c in cands:
            got = by_id.get(c["item_id"])
            out.append(got if got is not None else ContextCheck(
                item_id=c["item_id"], supports=True,
                reason="문맥검증 미회신(시간 제한·장애 포함) — 지적 유지"))
        return out + passthrough
