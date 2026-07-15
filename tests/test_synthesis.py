"""서면검토 5단계 테스트 — 검증 2차·의견서 초안 (LLM·법령은 모의 주입).

검증 대상 제약:
- flag-only: 검증 2차는 지적을 '확인 필요'로 강등만 하고 번복·신설하지 않음
- 1차(결정론) 탈락 시 2차 미수행
- synthesizer LLM 장애 시 결정론 폴백(재현성)
- 포매터가 면책·'AI 초안'·provisional 고지를 결정론적으로 부가(누락 불가)
- 탐색 레인 미설정 시 무해(빈 결과)
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.agents.axis_reviewer import AxisReviewer, Rubric
from audit_core.agents.base import LLMUnavailable, OllamaClient
from audit_core.agents.context_verifier import ContextVerifier
from audit_core.agents.law_search import LawSearchClient
from audit_core.agents.synthesizer import Finding, Synthesizer
from audit_core.orchestrator import Orchestrator, format_written_review


# ── 모의 ────────────────────────────────────────────────

class _Art:
    def __init__(self, ref):
        self.law_name, self.article, self.text = ref, "(조문)", f"{ref} 조문 본문(모의)"


class FakeLaw:
    """알고 있는 ref만 조회 성공, 나머지는 예외(1차 결정론 실존 검증 모의)."""
    def __init__(self, known):
        self.known = set(known)

    def fetch_ref(self, ref):
        if ref in self.known:
            return _Art(ref)
        raise RuntimeError(f"미존재: {ref}")


class FakeReviewClient(OllamaClient):
    """축별 검토 응답 모의 — 지정 item만 FLAG, 나머지 OK."""
    def __init__(self, flag_item, severity=2, evidence="근거 미비"):
        self.flag_item, self.severity, self.evidence = flag_item, severity, evidence

    def chat_json(self, *, model, prompt, schema, **kw):
        # 통합 호출(2026-07-15): 프롬프트의 항목 id를 읽어 지정 항목만 FLAG
        import re as _re
        ids = _re.findall(r"^- ([A-Za-z0-9]\w*(?:-\d+)?): ", prompt, _re.M)
        items = [{"item_id": i,
                  "verdict": "FLAG" if i == self.flag_item else "OK",
                  "evidence": self.evidence if i == self.flag_item else "확인",
                  "severity": self.severity if i == self.flag_item else 1}
                 for i in ids]
        return schema.model_validate({"axis": "ALL", "items": items})


class FakeSynthContext(OllamaClient):
    """ContextCheck·OpinionDraft 모의. raise_llm=True면 장애 유발(폴백 경로 검증)."""
    def __init__(self, supports=True, raise_llm=False):
        self.supports, self.raise_llm = supports, raise_llm

    def chat_json(self, *, model, prompt, schema, **kw):
        if self.raise_llm:
            raise LLMUnavailable("모의 장애")
        name = schema.__name__
        if name == "ContextCheck":
            return schema.model_validate({"item_id": "?", "supports": self.supports, "reason": "모의"})
        if name == "ContextCheckBatch":
            # 일괄 문맥검증(2026-07-15) — 프롬프트의 item_id들을 읽어 전건 회신
            import re as _re
            ids = _re.findall(r"^- item_id: (\S+)", prompt, _re.M)
            return schema.model_validate({"checks": [
                {"item_id": i, "supports": self.supports, "reason": "모의"} for i in ids]})
        if name == "OpinionDraft":
            return schema.model_validate({
                "query": "모의 질의", "facts": "모의 사실",
                "issues": [{"title": "T", "issue": "I", "rule": "R",
                            "application": "A", "conclusion": "C", "certainty": "높음"}],
                "overall": "모의 종합", "recommendations": ["권고1"],
            })
        raise AssertionError(f"예상 밖 스키마: {name}")


def _orch(flag_item, known_refs, supports=True, raise_llm=False):
    rubric = Rubric()
    return Orchestrator(
        reviewer=AxisReviewer(client=FakeReviewClient(flag_item)),
        rubric=rubric,
        law_fetcher=FakeLaw(known_refs),
        context_verifier=ContextVerifier(client=FakeSynthContext(supports, raise_llm)),
        synthesizer=Synthesizer(client=FakeSynthContext(supports, raise_llm)),
        law_search=LawSearchClient(base_url=""),  # 탐색 레인 비활성
    )


# A1의 law_refs = ["지방계약법-제9조"] (rubric_v0_1)
DOC = "용역 사업\n사업개요"


class TestWrittenReview(unittest.TestCase):
    def test_confirmed_finding_flows_to_opinion(self):
        orch = _orch("A1", known_refs=["지방계약법-제9조"], supports=True)
        wr = orch.written_review("용역", DOC)
        self.assertEqual([f.item_id for f in wr.confirmed], ["A1"])
        self.assertEqual(wr.needs_review, [])
        self.assertIsNotNone(wr.opinion)
        # 인용 조문이 실존 검증되어 Finding에 붙는다
        self.assertEqual(wr.confirmed[0].law_refs, ["지방계약법-제9조"])

    def test_first_stage_downgrade_no_existing_ref(self):
        # 인용 조문이 실존하지 않으면 1차에서 강등, 2차 미수행
        orch = _orch("A1", known_refs=[], supports=True)
        wr = orch.written_review("용역", DOC)
        self.assertEqual(wr.confirmed, [])
        self.assertEqual([i for i, _ in wr.needs_review], ["A1"])
        self.assertIn("1차", wr.needs_review[0][1])
        self.assertEqual(wr.context_checks, [])  # 2차 미수행

    def test_second_stage_downgrade_context_mismatch(self):
        # 조문은 실존하나 문맥 부적합(supports=False) → 강등
        orch = _orch("A1", known_refs=["지방계약법-제9조"], supports=False)
        wr = orch.written_review("용역", DOC)
        self.assertEqual(wr.confirmed, [])
        self.assertEqual([i for i, _ in wr.needs_review], ["A1"])
        self.assertIn("2차", wr.needs_review[0][1])
        self.assertEqual(len(wr.context_checks), 1)

    def test_flag_only_cannot_invent(self):
        # 검증 2차는 지적을 새로 만들 수 없다 — OK만 있으면 confirmed·needs_review 모두 비어야
        orch = _orch("__none__", known_refs=["지방계약법-제9조"], supports=True)
        wr = orch.written_review("용역", DOC)
        self.assertEqual(wr.confirmed, [])
        self.assertEqual(wr.needs_review, [])


class TestSynthesizerFallback(unittest.TestCase):
    def test_llm_failure_falls_back_deterministically(self):
        synth = Synthesizer(client=FakeSynthContext(raise_llm=True))
        finding = Finding("A1", "A", "계약방법 근거 명시?", "근거 미기재", 2,
                          law_refs=["지방계약법-제9조"], law_text="본문")
        d1 = synth.draft("용역", [finding], numeric_notes=["합계 오류"])
        d2 = synth.draft("용역", [finding], numeric_notes=["합계 오류"])
        self.assertEqual(d1.model_dump(), d2.model_dump())  # 재현성
        self.assertEqual(len(d1.issues), 1)
        self.assertIn("A1", d1.issues[0].title)

    def test_no_findings_clean_opinion(self):
        synth = Synthesizer(client=FakeSynthContext(raise_llm=True))
        d = synth.draft("용역", [])
        self.assertEqual(d.issues, [])
        self.assertIn("지적사항 없음", d.overall)


class TestFormatWrittenReview(unittest.TestCase):
    def test_mandatory_notices_always_present(self):
        orch = _orch("A1", known_refs=["지방계약법-제9조"], supports=True)
        text = format_written_review(orch.written_review("용역", DOC))
        self.assertIn("AI가 생성한 검토 초안", text)      # A1 제약 고지
        self.assertIn("협의 전 초안", text)               # provisional 경고(B1)
        self.assertIn("확정 감사의견을 대체하지 않습니다", text)
        self.assertNotIn("종합 준수율", text)             # 점수화 없음(B2)

    def test_downgraded_items_shown_separately(self):
        orch = _orch("A1", known_refs=[], supports=True)  # 1차 강등
        text = format_written_review(orch.written_review("용역", DOC))
        self.assertIn("확인 필요", text)
        self.assertIn("A1", text)


class TestSearchLaneOptional(unittest.TestCase):
    def test_disabled_client_returns_empty(self):
        c = LawSearchClient(base_url="")
        self.assertFalse(c.enabled)
        self.assertEqual(c.search_ordinance("일상감사"), [])

    def test_network_error_degrades_to_empty(self):
        def boom(url, payload, t):
            raise OSError("연결 실패")
        c = LawSearchClient(base_url="http://x", post_fn=boom)
        self.assertTrue(c.enabled)
        self.assertEqual(c.search_precedent("계약"), [])  # 예외 없이 빈 결과


if __name__ == "__main__":
    unittest.main()
