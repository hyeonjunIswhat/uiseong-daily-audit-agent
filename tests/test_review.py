"""축별 검토·검산·오케스트레이터 테스트 (LLM은 모의 주입).

실제 LLM 축별 검토 end-to-end는 tests/smoke_review.py로 별도 확인.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "pipelines"))

from audit_core.agents.axis_reviewer import AxisReviewer, Rubric
from audit_core.agents.base import OllamaClient
from audit_core.agents.schemas import AxisResult
from audit_core.agents.verifier import arithmetic_flags, check_arithmetic
from audit_core.orchestrator import Orchestrator, format_self_check


class TestArithmetic(unittest.TestCase):
    def test_sum_mismatch_detected(self):
        doc = "소계: 72,600,000원\n부가가치세: 7,260,000원\n합계: 85,000,000원"
        flags = arithmetic_flags(doc)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].expected, 79_860_000)
        self.assertEqual(flags[0].claimed, 85_000_000)

    def test_sum_correct_no_flag(self):
        doc = "소계: 72,600,000원\n부가가치세: 7,260,000원\n합계: 79,860,000원"
        self.assertEqual(arithmetic_flags(doc), [])

    def test_hierarchical_no_false_positive(self):
        # 직접인건비=하위합 계층 구조에서 소계를 임의 재구성하지 않음
        doc = ("특급기술자 1명 × 4 × 8,000,000원 = 32,000,000원\n"
               "직접인건비: 55,000,000원\n소계: 72,600,000원\n"
               "부가가치세: 7,260,000원\n합계: 79,860,000원")
        self.assertEqual(arithmetic_flags(doc), [])

    def test_multiplication_check(self):
        checks = [c for c in check_arithmetic("1명 × 4 × 8,000,000원 = 32,000,000원") if c.kind == "mult"]
        self.assertTrue(checks and checks[0].match)

    def test_no_totals_no_checks(self):
        self.assertEqual(check_arithmetic("금액 없음, 텍스트만"), [])


class FakeClient(OllamaClient):
    """축별 검토 LLM 응답 모의."""
    def __init__(self, responses):
        self._responses = responses  # axis → AxisResult dict

    def chat_json(self, *, model, prompt, schema, **kw):
        # 축별 프롬프트(단일 축 review 경로)
        for axis_key, payload in self._responses.items():
            if f"[검토 축] {axis_key}." in prompt:
                return schema.model_validate(payload)
        # 통합 프롬프트(review_all, 2026-07-15) — 등록된 응답을 항목 단위로 합쳐 회신
        import re as _re
        ids = set(_re.findall(r"^- ([A-Za-z0-9]\w*(?:-\d+)?): ", prompt, _re.M))
        if ids:
            items = [it for payload in self._responses.values()
                     for it in payload["items"] if it["item_id"] in ids]
            known = {it["item_id"] for it in items}
            items += [{"item_id": i, "verdict": "OK", "evidence": "확인", "severity": 1}
                      for i in ids - known]
            return schema.model_validate({"axis": "ALL", "items": items})
        return schema.model_validate({"axis": "?", "items": []})


class TestRubric(unittest.TestCase):
    def setUp(self):
        self.rubric = Rubric()

    def test_active_axes_for_용역(self):
        axes = self.rubric.active_axes("용역")
        keys = [a["axis"] for a in axes]
        self.assertIn("2", keys)   # 타당성(구 B 원가 계열)
        self.assertIn("6", keys)   # 법적 요건(구 E 계약조건 계열)
        self.assertEqual(len(keys), 7)  # 규정 §7④ 8축 중 축8(기타 슬롯) 제외

    def test_item_level_applies_to_filter(self):
        # E4(하자담보)는 공사 전용 → 용역의 축6(법적 요건)에서 제외
        ax6 = next(a for a in self.rubric.active_axes("용역") if a["axis"] == "6")
        self.assertNotIn("E4", [i["item_id"] for i in ax6["items"]])
        ax6_gongsa = next(a for a in self.rubric.active_axes("공사") if a["axis"] == "6")
        self.assertIn("E4", [i["item_id"] for i in ax6_gongsa["items"]])

    def test_민간보조_axes(self):
        axes = self.rubric.active_axes("민간보조")
        keys = [a["axis"] for a in axes]
        self.assertIn("1", keys)   # 합법성(구 C1·D1)
        self.assertIn("6", keys)   # 법적 요건(D3 보조사업 조건 포함)
        self.assertNotIn("2", keys)  # 타당성-원가 축은 민간보조 미적용


class TestAxisReviewerCorrection(unittest.TestCase):
    def test_missing_and_bogus_items_normalized(self):
        rubric = Rubric()
        c_axis = next(a for a in rubric.active_axes("용역") if a["axis"] == "1")
        # LLM이 C1만 답하고 존재하지 않는 CX를 지어낸 상황
        fake = FakeClient({"1": {"axis": "1", "items": [
            {"item_id": "C1", "verdict": "FLAG", "evidence": "근거", "severity": 2},
            {"item_id": "CX", "verdict": "FLAG", "evidence": "환각 항목", "severity": 3},
        ]}})
        result = AxisReviewer(client=fake).review(c_axis, "문서")
        ids = [it.item_id for it in result.items]
        self.assertEqual(ids, [it["item_id"] for it in c_axis["items"]])  # 루브릭 순서 그대로
        self.assertNotIn("CX", ids)  # 환각 제거
        d1 = next(it for it in result.items if it.item_id == "D1")
        self.assertEqual(d1.verdict, "UNABLE")  # 누락분은 UNABLE 보정


class TestOrchestrator(unittest.TestCase):
    def test_self_check_merges_numeric_and_axis(self):
        rubric = Rubric()
        # 모든 축을 OK로 답하는 모의 — 산식 오류만 남게
        responses = {}
        for a in rubric.active_axes("용역"):
            responses[a["axis"]] = {"axis": a["axis"], "items": [
                {"item_id": it["item_id"], "verdict": "OK", "evidence": "확인", "severity": 1}
                for it in a["items"]
            ]}
        orch = Orchestrator(
            reviewer=AxisReviewer(client=FakeClient(responses)),
            rubric=rubric,
            law_fetcher=_NoLaw(),
        )
        doc = "용역\n소계: 100원\n부가가치세: 10원\n합계: 999원"
        report = orch.self_check("용역", doc)
        self.assertEqual(len(report.numeric_flags), 1)
        self.assertEqual(report.flags(), [])  # 축은 전부 OK
        text = format_self_check(report)
        self.assertIn("산식 불일치", text)
        self.assertIn("999", text)

    def test_no_active_axis_graceful(self):
        report = Orchestrator(reviewer=AxisReviewer(client=FakeClient({})),
                              rubric=Rubric(), law_fetcher=_NoLaw()).self_check("없는유형", "문서")
        self.assertEqual(report.axis_results, [])


class _NoLaw:
    def fetch_ref(self, ref):
        raise RuntimeError("법령 조회 비활성(테스트)")


if __name__ == "__main__":
    unittest.main()
