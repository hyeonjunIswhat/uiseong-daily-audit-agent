"""회차 2 테스트 — 문서 간 정합성 대조(A5.5) + 번들 분리 + 발췌 앵커.

검증 대상:
- 번들 분리: 표제 줄 경계, 80자 미만 조각(첨부 목록) 흡수, 표제 2개 미만이면 단일
- 대조기: 사업명(포함 관계 허용)·총액(천원 환산)·배점(합계·문서 간)·기간('개월'만)
- 보수 원칙: '12월'(달력 월) 오인 금지, 추정가격(부가세 제외)은 총액 대조에서 제외
- DAG 통합: cross_flags → 포매터 섹션·종결구(조치요구 기본 허용)
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.rules.cross_check import DocPart, cross_check, split_bundle

REQ = """일상감사 요청서(제6조제1항관련)
업무(사업)명
의성군 생성형 AI 플랫폼 구축
• 추정금액: 금310,000,000원(금삼억일천만원)
사업기간
계약일로부터 8개월
첨 부 서 류
산출내역서 1부
"""

NEG = """협상에 의한 계약 대상사업 검토서
①사업명
의성군 생성형 AI 플랫폼 구축 사업
④사업비
금310,000천원
③사업기간
2026.4.~2026.12.(8개월)
제안서 평가: 기술평가 90점, 가격평가 10점
"""


class TestSplit(unittest.TestCase):
    def test_split_two_docs_and_absorb_tiny(self):
        parts = split_bundle(REQ + NEG)
        # 첨부 목록의 '산출내역서 1부'가 가짜 문서로 분리되지 않아야 함(80자 흡수)
        self.assertEqual(len(parts), 2)
        self.assertIn("요청서", parts[0].label)
        self.assertIn("검토서", parts[1].label)

    def test_single_doc_not_split(self):
        parts = split_bundle(REQ)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].label, "문서")


class TestComparators(unittest.TestCase):
    def test_consistent_bundle_no_flags(self):
        # 사업명은 포함 관계('… 구축' ⊂ '… 구축 사업') → 동일 취급,
        # 총액은 310,000,000원 = 310,000천원 → 환산 일치, 기간 8개월 = 8개월
        self.assertEqual(cross_check(split_bundle(REQ + NEG)), [])

    def test_name_mismatch(self):
        bad = NEG.replace("생성형 AI 플랫폼", "스마트팜 빅데이터")
        kinds = [f.kind for f in cross_check(split_bundle(REQ + bad))]
        self.assertIn("사업명", kinds)

    def test_amount_mismatch_with_unit_conversion(self):
        bad = NEG.replace("금310,000천원", "금280,000천원")
        flags = cross_check(split_bundle(REQ + bad))
        f = next(f for f in flags if f.kind == "총액")
        self.assertIn("상이함", f.note)
        self.assertIn("정정하시기 바람", f.note)  # SOP ⑤ 표준 서술
        # 발췌 앵커: 문서 라벨·행 번호·원문 인용 포함
        self.assertTrue(all(e.line_no > 0 for e in f.extracts))
        self.assertIn("행 「", f.note)

    def test_period_mismatch_and_calendar_month_ignored(self):
        bad = NEG.replace("(8개월)", "(6개월)")
        kinds = [f.kind for f in cross_check(split_bundle(REQ + bad))]
        self.assertIn("사업기간", kinds)
        # '12월'(달력 월) 표기는 기간으로 오인하지 않음(보수 원칙)
        cal = NEG.replace("(8개월)", "")  # 검토서 기간 정보 제거
        req_cal = REQ.replace("계약일로부터 8개월", "계약일로부터 ∼ ’26. 12월")
        self.assertEqual([f.kind for f in cross_check(split_bundle(req_cal + cal))
                          if f.kind == "사업기간"], [])

    def test_score_sum_and_cross(self):
        # 문서 내 합계 ≠ 100
        bad = NEG.replace("가격평가 10점", "가격평가 20점")
        flags = cross_check(split_bundle(REQ + bad))
        self.assertTrue(any(f.kind == "배점" and "합계가 100이 아님" in f.note for f in flags))
        # 문서 간 배점 상이
        parts = [DocPart("공고문", "평가: 기술평가 80점, 가격평가 20점"),
                 DocPart("제안요청서", "평가: 기술평가 90점, 가격평가 10점")]
        flags2 = cross_check(parts)
        self.assertTrue(any(f.kind == "배점" and "상이함" in f.note for f in flags2))

    def test_estimated_price_excluded_from_amount(self):
        # 추정가격(부가세 제외)은 총액 계열과 다른 값이 정상 — 대조 제외
        parts = [DocPart("요청서", "사업비\n금310,000천원"),
                 DocPart("계산서", "추정가격: 금281,818,182원")]
        self.assertEqual([f.kind for f in cross_check(parts) if f.kind == "총액"], [])


class TestDagIntegration(unittest.TestCase):
    def test_cross_flags_in_report_and_formatter(self):
        from test_rebuild_r1 import _orch  # 통합 페이크 재사용
        from audit_core.orchestrator import format_written_review

        orch = _orch(set(), known_refs=[])
        bad = NEG.replace("금310,000천원", "금280,000천원")
        parts = split_bundle(REQ + bad)
        wr = orch.written_review("용역", REQ + bad, doc_parts=parts)
        self.assertEqual(len(wr.report.cross_flags), 1)
        text = format_written_review(wr)
        self.assertIn("자동 확인 — 문서 간 대조", text)
        self.assertIn("정정하시기 바람", text)
        # 사실 불일치 → 종결구 조치요구 기본 허용
        c = next(c for c in wr.closings if c.item_id.startswith("대조#"))
        self.assertEqual(c.proposal, "하시기 바람")


if __name__ == "__main__":
    unittest.main()
