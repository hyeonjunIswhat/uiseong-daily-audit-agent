"""문서유형(doc_type) 감지·프로파일 축 필터·파이프 대상판정 프리체크 테스트."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "pipelines"))

from audit_core.agents.axis_reviewer import AxisReviewer, Rubric
from audit_core.agents.base import OllamaClient
from audit_core.agents.schemas import AxisResult
from audit_core.orchestrator import Orchestrator, format_self_check
from audit_core.rules.doc_type import DocProfiles, DocTypeResult, detect_doc_type

REAL_SAMPLE = Path("/Users/aidata/Downloads/제안서 평가위원(후보자) 모집 공고문.hwpx")


class TestDetect(unittest.TestCase):
    def setUp(self):
        self.p = DocProfiles()

    def test_공고문_키워드(self):
        r = self.p.detect("「의성군 생성형 AI 플랫폼 구축」\n제안서 평가위원(후보자) 모집 공고문\n1. 용역개요")
        self.assertEqual(r.doc_type, "공고문")
        self.assertEqual(r.axes, ("1", "5"))
        self.assertTrue(r.narrows)

    def test_공고문_머리글_패턴(self):
        r = self.p.detect("의성군 공고 제2026-13호\n평가위원 안내")
        self.assertEqual(r.doc_type, "공고문")

    def test_계산서(self):
        r = self.p.detect("원가계산서\n소계 | 100원\n합계 | 110원")
        self.assertEqual(r.doc_type, "계산서")
        self.assertEqual(r.axes, ("2", "6"))

    def test_추진계획서(self):
        r = self.p.detect("2026년 상반기 업무추진계획\n1. 목적")
        self.assertEqual(r.doc_type, "추진계획서")
        self.assertEqual(r.axes, ("1", "3", "4", "5"))

    def test_복합문서는_전축(self):
        # 진짜 번들 — 계산서 '표제 줄'이 서두에 실재하면 복합 → 전축
        r = self.p.detect("업무추진계획\n원가계산서\n소계 | 100원")
        self.assertEqual(r.doc_type, "의뢰서")
        self.assertFalse(r.narrows)
        self.assertIn("복합", r.reason)

    def test_서술문장_속_계산서_언급은_좁히지_않음(self):
        # 실물 결함(2026-07-15): 조치결과 통보서의 감사의견 서술("산출내역서 내
        # 타 사업 명칭 잔존…")이 계산서 프로파일로 오축소되던 사례
        r = self.p.detect(
            "[별지 제3호서식]\n일상감사 의견에 대한 조치결과 통보서(제8조제2항관련)\n"
            "감사의견\n- 산출내역서 내 타 사업 명칭 잔존에 따른 정비 필요")
        self.assertEqual(r.doc_type, "의뢰서")  # 좁히지 않음(전축)

    def test_붙임_언급만으로는_복합_아님(self):
        # '붙임: 원가계산서' 언급뿐(본문 없음)이면 계산서 축(B)을 볼 수 없으므로
        # 추진계획서로 좁히는 것이 옳다(스킵 축은 사유와 함께 표시 — 침묵 금지)
        r = self.p.detect("업무추진계획\n...\n붙임: 원가계산서 1부")
        self.assertEqual(r.doc_type, "추진계획서")

    def test_의뢰서_표지_우선(self):
        r = self.p.detect("일상감사 의뢰서\n붙임: 원가계산서, 모집 공고문")
        self.assertEqual(r.doc_type, "의뢰서")
        self.assertFalse(r.narrows)

    def test_미감지는_전축(self):
        r = self.p.detect("특이 표지가 없는 일반 문서 본문")
        self.assertEqual(r.doc_type, "의뢰서")
        self.assertFalse(r.narrows)

    @unittest.skipUnless(REAL_SAMPLE.exists(), "실샘플 없음")
    def test_실샘플_공고문(self):
        from audit_core.parsers.hwpx import parse_hwpx
        r = self.p.detect(parse_hwpx(REAL_SAMPLE).text)
        self.assertEqual(r.doc_type, "공고문")


class FakeClient(OllamaClient):
    """축 요청에 전 항목 OK로 답하는 모의 (test_review와 동일 패턴)."""

    def __init__(self, responses):
        self.responses = responses

    def chat_json(self, *, model, prompt, schema, **kw):
        import re as _re
        ids = _re.findall(r"^- ([A-Za-z0-9]\w*(?:-\d+)?): ", prompt, _re.M)
        return AxisResult.model_validate({"axis": "ALL", "items": [
            {"item_id": i, "verdict": "OK", "evidence": "확인", "severity": 1} for i in ids]})


class _NoLaw:
    def fetch_ref(self, ref):
        raise RuntimeError("법령 조회 비활성(테스트)")


def _ok_responses(rubric, biz):
    return {
        a["axis"]: {"axis": a["axis"], "items": [
            {"item_id": it["item_id"], "verdict": "OK", "evidence": "확인", "severity": 1}
            for it in a["items"]
        ]}
        for a in rubric.active_axes(biz)
    }


class TestAxisFilter(unittest.TestCase):
    def setUp(self):
        self.rubric = Rubric()
        self.orch = Orchestrator(
            reviewer=AxisReviewer(client=FakeClient(_ok_responses(self.rubric, "용역"))),
            rubric=self.rubric,
            law_fetcher=_NoLaw(),
        )

    def test_계산서_프로파일은_BC만(self):
        profile = detect_doc_type("원가계산서\n용역 원가")
        report = self.orch.self_check("용역", "원가계산서 본문", doc_profile=profile)
        run = {ar.axis for ar in report.axis_results}
        self.assertEqual(run, {"2", "6"})
        skipped_names = [n for n, _ in report.skipped_axes]
        self.assertEqual(len(skipped_names), 5)  # 1·3·4·5·7
        text = format_self_check(report)
        self.assertIn("자료 부족 등으로 확인하지 못한 항목", text)
        self.assertIn("판단 불가", report.skipped_axes[0][1])

    def test_전축_프로파일은_필터없음(self):
        profile = detect_doc_type("일상감사 의뢰서")
        report = self.orch.self_check("용역", "본문", doc_profile=profile)
        self.assertEqual(len(report.axis_results), 7)
        self.assertEqual(report.skipped_axes, [])

    def test_프로파일_미지정은_기존동작(self):
        report = self.orch.self_check("용역", "본문")
        self.assertEqual(len(report.axis_results), 7)

    def test_전축스킵이면_산식검산만(self):
        empty = DocTypeResult(doc_type="계산서", label="테스트", axes=(),
                              skip_note="판단 불가", reason="테스트")
        doc = "소계: 100원\n부가가치세: 10원\n합계: 999원"
        report = self.orch.self_check("용역", doc, doc_profile=empty)
        self.assertEqual(report.axis_results, [])
        self.assertEqual(len(report.numeric_flags), 1)
        self.assertEqual(len(report.skipped_axes), 7)


class TestTargetPreface(unittest.TestCase):
    def setUp(self):
        from daily_audit_pipe import Pipeline
        self.pipe = Pipeline()

    def test_라벨금액_있으면_판정(self):
        doc = "OO 정보화 용역 추진\n추정가격: 1,200,000,000원 (부가세 제외)"
        out = self.pipe._target_preface(doc)
        self.assertIn("일상감사 대상", out)
        self.assertIn("대상판정(참고)", out)

    def test_라벨없는_금액은_침묵(self):
        out = self.pipe._target_preface("용역 개요\n1,200,000,000원")
        self.assertEqual(out, "")

    def test_유형없으면_침묵(self):
        out = self.pipe._target_preface("추정가격: 1,200,000,000원")
        self.assertEqual(out, "")

    @unittest.skipUnless(REAL_SAMPLE.exists(), "실샘플 없음")
    def test_실샘플_공고문은_금액없어_침묵(self):
        from audit_core.parsers.hwpx import parse_hwpx
        out = self.pipe._target_preface(parse_hwpx(REAL_SAMPLE).text)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
