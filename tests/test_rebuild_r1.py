"""REBUILD 회차 1 테스트 — 마스터 SOP 규율 이식(전부 결정론 모듈).

검증 대상:
- 근거 태그(SOP 제2부): 분류·[직접적용] 없는 지적의 강등 사유·표기
- 실익 게이트(SOP ⑥): 해소단서·과소계상·확정공문 3종 제외 + 사유 보존, 보수 원칙
- 종결구 3단(SOP ⑦): 기본 한 단계 하향, 사실 불일치는 조치요구 유지, 금칙어 flag-only
- 서류 완결성(A2): 유형 인식(본문>파일명)·계약방법별 필수서류·보완요청 문안
- 계약방법 오버레이(SOP 제3부): 모듈 매칭·공사+협상 모순(결정론)·축 주입
"""

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.agents.axis_reviewer import ContractMethodOverlay
from audit_core.rules import closing
from audit_core.rules.citation_tags import (
    COST, DIRECT, SPIRIT, UNCLASSIFIED, CitationTags, format_tagged_refs,
)
from audit_core.rules.completeness import RequiredDocs, format_completeness
from audit_core.rules.merit_gate import MeritGate, format_exclusions


@dataclass
class _F:  # merit_gate·closing이 보는 최소 형상
    item_id: str
    question: str
    evidence: str
    severity: int = 2


# ── 근거 태그 ────────────────────────────────────────────

class TestCitationTags(unittest.TestCase):
    def setUp(self):
        self.tags = CitationTags()

    def test_classify(self):
        self.assertEqual(self.tags.classify("지방계약법시행령-제25조"), DIRECT)
        self.assertEqual(self.tags.classify("의성군 일상감사 규정 §8①"), DIRECT)
        self.assertEqual(self.tags.classify("서울시 일상감사 편람 p.74"), SPIRIT)
        self.assertEqual(self.tags.classify("조달청 질의회신 2020-12"), SPIRIT)
        self.assertEqual(self.tags.classify("국가계약법 제7조"), SPIRIT)
        self.assertEqual(self.tags.classify("의성군 계약원가심사 업무처리 규칙"), COST)
        self.assertEqual(self.tags.classify("알 수 없는 지침"), UNCLASSIFIED)

    def test_spirit_wins_over_direct_pattern(self):
        # '서울시 … 계약 기준'처럼 두 계열 패턴이 섞이면 배제(취지참고)가 우선
        self.assertEqual(self.tags.classify("서울시 지방계약법 해설"), SPIRIT)

    def test_demotion(self):
        # 직접적용 근거가 하나라도 있으면 강등 없음
        self.assertIsNone(self.tags.demotion_reason(["지방계약법-제22조", "서울시편람"]))
        # 전부 취지참고 → 강등 사유 반환
        reason = self.tags.demotion_reason(["서울시편람 p.74", "조달청 회신"])
        self.assertIn("직접적용", reason)
        # 근거 자체가 없으면 태그 규율 소관 아님(1차 실존 검증 소관)
        self.assertIsNone(self.tags.demotion_reason([]))

    def test_format(self):
        out = format_tagged_refs({"지방계약법-제22조": DIRECT, "서울시편람": SPIRIT})
        self.assertIn("지방계약법-제22조", out)
        self.assertIn("서울시편람(취지 참고)", out)


# ── 실익 게이트 ──────────────────────────────────────────

class TestMeritGate(unittest.TestCase):
    def setUp(self):
        self.gate = MeritGate()

    def test_resolved_clause(self):
        doc = "규격: OO칩셋 또는 동등 이상 성능 증빙 제시"
        f = _F("B1", "특정 상표 지정 여부", "특정 규격 제한이 확인됨")
        kept, exc = self.gate.apply([f], doc)
        self.assertFalse(kept)
        self.assertEqual(exc[0].rule, "해소단서")

    def test_resolved_clause_requires_topic(self):
        # 문서에 해소 단서가 있어도 지적 주제가 규격·상표가 아니면 제외하지 않음(보수)
        doc = "규격: 동등 이상 증빙 제시"
        f = _F("C1", "예산 과목 적정성", "예산 과목 근거 미비")
        kept, exc = self.gate.apply([f], doc)
        self.assertEqual(len(kept), 1)
        self.assertFalse(exc)

    def test_under_estimation(self):
        f = _F("B2", "경비 산정 적정성", "간접경비가 기준 대비 과소계상됨")
        kept, exc = self.gate.apply([f], "본문")
        self.assertFalse(kept)
        self.assertEqual(exc[0].rule, "과소계상")

    def test_confirmed_doc(self):
        doc = "행정안전부 제2026-153호로 사업이 확정 통보됨"
        f = _F("A1", "사업추진 타당성 확보 여부", "타당성 근거 서술 미비")
        kept, exc = self.gate.apply([f], doc)
        self.assertFalse(kept)
        self.assertEqual(exc[0].rule, "확정공문")
        self.assertIn("2026-153", exc[0].reason)

    def test_confirmed_doc_needs_confirm_context(self):
        # 공문번호가 있어도 '확정·승인' 문맥이 없으면 제외하지 않음(보수)
        doc = "관련 문서: 행정안전부 제2026-153호 참조"
        f = _F("A1", "사업추진 타당성 확보 여부", "타당성 근거 서술 미비")
        kept, _exc = self.gate.apply([f], doc)
        self.assertEqual(len(kept), 1)

    def test_format(self):
        _, exc = self.gate.apply(
            [_F("B2", "x", "노무비 과소계상")], "본문")
        line = format_exclusions(exc)[0]
        self.assertTrue(line.startswith("[실익제외: B2"))


# ── 종결구·금칙어 ────────────────────────────────────────

class TestClosing(unittest.TestCase):
    def test_one_step_lower(self):
        s = closing.suggest("A1", severity=3)
        self.assertEqual(s.candidate, closing.GRADE_ACT)
        self.assertEqual(s.proposal, closing.GRADE_NEED)  # 한 단계 하향

    def test_lowest_stays(self):
        s = closing.suggest("A1", severity=1)
        self.assertEqual(s.proposal, closing.GRADE_REFER)

    def test_deterministic_keeps_act(self):
        s = closing.suggest("산식#1", severity=3, deterministic=True)
        self.assertEqual(s.proposal, closing.GRADE_ACT)  # 사실 불일치는 조치요구 허용

    def test_forbidden_scan(self):
        text = "이는 규정 위반으로 부적정하며 반드시 시정하여야 한다."
        hits = closing.scan_forbidden(text)
        terms = {h.term for h in hits}
        self.assertTrue({"위반", "부적정", "반드시"} <= terms)
        self.assertTrue(any(h.rule.startswith("의무부과") for h in hits))
        # 깨끗한 문장은 무검출
        self.assertFalse(closing.scan_forbidden("산출 근거를 보완할 필요가 있음."))


# ── 서류 완결성 ──────────────────────────────────────────

class TestCompleteness(unittest.TestCase):
    def setUp(self):
        self.req = RequiredDocs()

    def test_negotiation_case(self):
        doc = ("일상감사 요청서\n사업추진계획 개요…\n산출내역서 총괄표…\n"
               "협상에 의한 계약 대상사업 검토서 첨부")
        rep = self.req.check(doc, method="협상에 의한 계약")
        recognized = {h.key for h in rep.recognized}
        self.assertIn("일상감사요청서", recognized)
        self.assertIn("산출내역서", recognized)
        self.assertIn("계약방법검토서", recognized)
        # 협상 건 필수인 제안요청서 미제출 → 누락
        self.assertIn("제안요청서", [k for k, _l, _h in rep.missing])
        self.assertFalse(rep.complete)

    def test_sole_source_requires_reason_doc(self):
        rep = self.req.check("일상감사 요청서\n추진계획\n산출내역서", method="수의계약")
        self.assertIn("수의계약사유서", [k for k, _l, _h in rep.missing])

    def test_filename_only_is_uncertain(self):
        rep = self.req.check("본문에 표지 없음", filenames=["3. 산출내역서.xlsx"])
        self.assertIn("산출내역서", {h.key for h in rep.uncertain})

    def test_attachment_mention_is_uncertain(self):
        # 실물 결함(2026-07-15): 요청서 '첨부서류' 목록에 적힌 서류명이 제출된
        # 것으로 강인식되어 누락 검출이 무력화되던 사례 — 언급은 '확인 필요'로
        doc = ("일상감사 요청서\n업무(사업)명: AI 플랫폼 구축\n"
               "첨 부 서 류\n산출내역서 1부\n제안요청서 1부")
        rep = self.req.check(doc, method="협상에 의한 계약")
        self.assertEqual([h.key for h in rep.recognized], ["일상감사요청서"])
        self.assertIn("산출내역서", {h.key for h in rep.uncertain})
        self.assertIn("제안요청서", {h.key for h in rep.uncertain})
        # 언급도 없는 필수서류는 여전히 누락으로
        self.assertIn("계약방법검토서", [k for k, _l, _h in rep.missing])

    def test_emergency_form_hint_and_format(self):
        rep = self.req.check("일상감사 요청서", method="긴급입찰")
        missing_keys = [k for k, _l, _h in rep.missing]
        self.assertIn("긴급입찰사유서", missing_keys)
        out = format_completeness(rep)
        self.assertIn("보완요청 문안", out)
        self.assertIn("긴급입찰", out)

    def test_goods_requires_spec(self):
        rep = self.req.check("일상감사 요청서", biz_type="물품")
        self.assertIn("규격서", [k for k, _l, _h in rep.missing])


# ── 계약방법 오버레이 ────────────────────────────────────

class TestOverlay(unittest.TestCase):
    def setUp(self):
        self.ov = ContractMethodOverlay()

    def test_module_matching(self):
        self.assertEqual(self.ov.module_for("협상에 의한 계약")["axis"], "M1")
        self.assertEqual(self.ov.module_for("수의계약")["axis"], "M2")
        self.assertIsNone(self.ov.module_for("일반입찰"))
        self.assertIsNone(self.ov.module_for(None))

    def test_construction_negotiation_incompatible(self):
        note = self.ov.incompatibility("협상에 의한 계약", "공사")
        self.assertIsNotNone(note)
        self.assertIn("제43조", note)
        # 모순 건은 오버레이 축을 겹치지 않는다(결정론 지적으로 갈음)
        self.assertFalse(self.ov.overlay_axes("협상에 의한 계약", "공사"))

    def test_service_negotiation_ok(self):
        self.assertIsNone(self.ov.incompatibility("협상에 의한 계약", "용역"))
        axes = self.ov.overlay_axes("협상에 의한 계약", "용역")
        self.assertEqual(len(axes), 1)
        self.assertEqual(axes[0]["axis"], "M1")
        self.assertTrue(axes[0]["items"])

    def test_sole_source_all_biz(self):
        # allowed_biz 미선언(null) 모듈은 전 분야 허용
        self.assertIsNone(self.ov.incompatibility("수의계약", "공사"))
        self.assertTrue(self.ov.overlay_axes("수의계약", "공사"))

    def test_item_lookup(self):
        self.assertIn("지방계약법시행령-제43조", self.ov.item_law_refs("M1-1"))
        self.assertIn("제43조", self.ov.item_question("M1-1"))


# ── DAG 통합 — 서면검토 경로에서 오버레이·태그·게이트·종결구가 작동하는가 ──

import re

from audit_core.agents.axis_reviewer import AxisReviewer, Rubric
from audit_core.agents.base import LLMUnavailable, OllamaClient
from audit_core.agents.context_verifier import ContextVerifier
from audit_core.agents.law_search import LawSearchClient
from audit_core.agents.synthesizer import Synthesizer
from audit_core.orchestrator import Orchestrator, format_written_review

_ITEM_RE = re.compile(r"^- ([A-Za-z]\w*(?:-\d+)?): ", re.M)


class FlagByIdClient(OllamaClient):
    """축 프롬프트의 항목 목록을 그대로 읽어 지정 id만 FLAG — 오버레이 M축 포함."""

    def __init__(self, flag_ids, evidence="근거 미비"):
        self.flag_ids, self.evidence = set(flag_ids), evidence

    def chat_json(self, *, model, prompt, schema, **kw):
        axis = "ALL"  # 통합 호출(2026-07-15) — 축은 회신 재그룹이 담당
        items = [
            {"item_id": i,
             "verdict": "FLAG" if i in self.flag_ids else "OK",
             "evidence": self.evidence if i in self.flag_ids else "확인",
             "severity": 3 if i in self.flag_ids else 1}
            for i in _ITEM_RE.findall(prompt)
        ]
        return schema.model_validate({"axis": axis, "items": items})


class StubSynthContext(OllamaClient):
    """ContextCheck는 supports=True, OpinionDraft는 장애(결정론 폴백 경로)."""

    def chat_json(self, *, model, prompt, schema, **kw):
        if schema.__name__ == "ContextCheck":
            return schema.model_validate({"item_id": "?", "supports": True, "reason": "모의"})
        raise LLMUnavailable("모의 장애 — 폴백")


class _KnownLaw:
    def __init__(self, known):
        self.known = set(known)

    def fetch_ref(self, ref):
        if ref not in self.known:
            raise RuntimeError(f"미존재: {ref}")
        art = type("Art", (), {})()
        art.law_name, art.article, art.text = ref, "(조문)", f"{ref} 본문(모의)"
        return art


def _orch(flag_ids, known_refs, evidence="근거 미비"):
    return Orchestrator(
        reviewer=AxisReviewer(client=FlagByIdClient(flag_ids, evidence)),
        rubric=Rubric(),
        law_fetcher=_KnownLaw(known_refs),
        context_verifier=ContextVerifier(client=StubSynthContext()),
        synthesizer=Synthesizer(client=StubSynthContext()),
        law_search=LawSearchClient(base_url=""),
    )


class TestWrittenReviewIntegration(unittest.TestCase):
    def test_overlay_axis_reviewed_and_closing_suggested(self):
        orch = _orch({"M1-1"}, known_refs=["지방계약법시행령-제43조"])
        wr = orch.written_review(
            "용역", "협상에 의한 계약 대상 용역", contract_method="협상에 의한 계약")
        self.assertIn("M1-1", [f.item_id for f in wr.confirmed])
        f = next(f for f in wr.confirmed if f.item_id == "M1-1")
        self.assertEqual(f.ref_tags["지방계약법시행령-제43조"], DIRECT)
        # 종결구: 심각도 3 후보 '하시기 바람' → 기본 한 단계 하향 제안
        c = next(c for c in wr.closings if c.item_id == "M1-1")
        self.assertEqual(c.proposal, closing.GRADE_NEED)
        text = format_written_review(wr)
        self.assertIn("종결구 제안", text)
        self.assertIn("M1-1", text)

    def test_construction_negotiation_deterministic_finding(self):
        orch = _orch(set(), known_refs=[])
        wr = orch.written_review(
            "공사", "공사 협상에 의한 계약 신청 건", contract_method="협상에 의한 계약")
        self.assertIsNotNone(wr.report.method_incompat)
        # 모순 건은 M축을 겹치지 않는다
        self.assertNotIn("M1", [ar.axis for ar in wr.report.axis_results])
        text = format_written_review(wr)
        self.assertIn("자동 확인 — 계약방법", text)
        self.assertIn("제43조", text)

    def test_merit_gate_excludes_in_dag(self):
        # 문서에 해소 단서('동등 이상') + 지적 주제가 특정 상표 → 본문 제외·말미 목록화
        orch = _orch({"A1"}, known_refs=["지방계약법-제9조"],
                     evidence="특정 상표 지정이 확인됨")
        wr = orch.written_review("용역", "규격서: 동등 이상 증빙 제시 단서 포함")
        self.assertNotIn("A1", [f.item_id for f in wr.confirmed])
        self.assertEqual(wr.merit_excluded[0].item_id, "A1")
        self.assertIn("[실익제외: A1", format_written_review(wr))


if __name__ == "__main__":
    unittest.main()


# ── 민감도 라우팅 골격 (회차 3 선행) ─────────────────────────

class TestSensitivity(unittest.TestCase):
    def test_red_doc_blocked_even_when_enabled(self):
        from audit_core.rules.sensitivity import egress_allowed
        ok, reason = egress_allowed("산출내역서", "본문", external_enabled=True)
        self.assertFalse(ok)
        self.assertIn("RED", reason)

    def test_switch_off_blocks_everything(self):
        from audit_core.rules.sensitivity import egress_allowed
        ok, reason = egress_allowed("일상감사요청서", "본문", external_enabled=False)
        self.assertFalse(ok)
        self.assertIn("미승인", reason)

    def test_mask_gate_catches_rrn_and_prices(self):
        from audit_core.rules.sensitivity import egress_allowed, mask_gate
        text = "담당자 주민번호 900101-1234567\n예정가격: 310,000,000원"
        hits = mask_gate(text)
        self.assertEqual({h.rule for h in hits}, {"개인정보", "가격정보"})
        ok, reason = egress_allowed("일상감사요청서", text, external_enabled=True)
        self.assertFalse(ok)
        self.assertIn("마스킹 게이트", reason)

    def test_clean_yellow_allowed_when_enabled(self):
        from audit_core.rules.sensitivity import egress_allowed
        ok, reason = egress_allowed("제안요청서", "과업 내용 서술", external_enabled=True)
        self.assertTrue(ok)
        self.assertIn("YELLOW", reason)

    def test_config_default_off(self):
        from audit_core.config import get_settings
        self.assertFalse(get_settings().EXTERNAL_LLM_ENABLED)
