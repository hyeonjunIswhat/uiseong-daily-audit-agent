"""구현 1~2단계 검증: config, target_check, deadline, ledger, pipe 골격.

완료 기준(SPEC §7): 동일 입력 3회 반복 시 동일 판정 코드(재현성).
실행: .venv/bin/python -m unittest discover tests -v
"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "pipelines"))

from audit_core.config import get_settings
from audit_core.ledger.ledger import Ledger, amount_band
from audit_core.rules.deadline import HolidayCalendar, audit_deadlines, compute_deadline
from audit_core.rules.target_check import TargetInput, TargetRuleSet, check_target


class TestConfig(unittest.TestCase):
    def test_defaults_load(self):
        s = get_settings()
        self.assertEqual(s.AUDIT_MODEL_LIGHT, "qwen3:8b")  # v0.2: 4b 미설치
        self.assertEqual(s.AUDIT_TRAIL_LEVEL, "code_only")
        self.assertEqual(s.deadline_days(), (7, 7, 14))


class TestTargetCheck(unittest.TestCase):
    def setUp(self):
        self.rules = TargetRuleSet()

    def decide(self, biz_type, amount, **kw):
        return check_target(TargetInput(biz_type=biz_type, amount=amount, **kw), self.rules)

    # ── 의성군 일상감사 규정 별표 기준 (ELIS 원문, 2026-07-15 확보) ──
    def test_종합공사_3억(self):
        self.assertEqual(self.decide("공사", 300_000_000).decision, "TARGET")   # 3억 이상
        self.assertEqual(self.decide("공사", 299_999_999).decision, "NOT_TARGET")

    def test_비종합공사_2억(self):
        d = self.decide("전기공사", 200_000_000)
        self.assertEqual(d.decision, "TARGET")
        self.assertEqual(d.biz_type, "비종합공사")
        self.assertEqual(self.decide("정보통신공사", 199_999_999).decision, "NOT_TARGET")

    def test_용역_7천만(self):
        self.assertEqual(self.decide("일반용역", 70_000_000).decision, "TARGET")
        self.assertEqual(self.decide("용역", 69_999_999).decision, "NOT_TARGET")
        # 별표는 계약방법 구분 없는 용역 단일 기준 — 협상·축제대행·학술도 동일
        self.assertEqual(self.decide("협상용역", 70_000_000).decision, "TARGET")
        self.assertEqual(self.decide("학술용역", 70_000_000).decision, "TARGET")

    def test_물품_2천만(self):
        d = self.decide("물품구매", 20_000_000)
        self.assertEqual(d.decision, "TARGET")
        self.assertEqual(d.biz_type, "물품")
        self.assertEqual(self.decide("물품구매", 19_999_999).decision, "NOT_TARGET")

    def test_민간보조_위탁_1억(self):
        self.assertEqual(self.decide("보조사업", 100_000_000).decision, "TARGET")
        self.assertEqual(self.decide("보조사업", 99_999_999).decision, "NOT_TARGET")
        self.assertEqual(self.decide("민간위탁", 100_000_000).decision, "TARGET")
        self.assertEqual(self.decide("민간위탁", 99_999_999).decision, "NOT_TARGET")

    def test_예산관리_금액무관_대상(self):
        self.assertEqual(self.decide("예비비", 0).decision, "TARGET")
        self.assertEqual(self.decide("지방채", 0).decision, "TARGET")
        self.assertEqual(self.decide("투융자심사", 0).decision, "TARGET")

    # ── 실물 골든 3건 — 실제 일상감사 처리된 사건이 TARGET으로 재현되는가 ──
    def test_golden_real_cases(self):
        # 한컴 SDK 서버라이선스 구매(관리번호 2026-32): 물품 3,080만 — 서울 잠정
        # 기준으론 비대상이었으나 실제 처리됨 → 별표(물품 2천만)로 설명(F1 해소)
        self.assertEqual(self.decide("물품구매", 30_800_000).decision, "TARGET")
        # 의성군 인공지능 GPU 서버 구매: 물품 1.4억
        self.assertEqual(self.decide("물품구매", 140_000_000).decision, "TARGET")
        # 생성형 AI 플랫폼 구축 사업: 협상용역 3.1억
        self.assertEqual(self.decide("협상용역", 310_000_000).decision, "TARGET")

    # ── 제외·선결 규칙 ──
    def test_채무부담_계약_금액무관(self):
        d = self.decide("물품구매", 0, flags=frozenset({"채무부담"}))
        self.assertEqual(d.decision, "TARGET")
        self.assertEqual(d.rule_id, "PRE-DEBT-CONTRACT")

    def test_재해복구_예비비_제외(self):
        d = self.decide("예비비", 0, flags=frozenset({"재해복구"}))
        self.assertEqual(d.decision, "NOT_TARGET")
        self.assertEqual(d.rule_id, "PRE-DISASTER-RESERVE")

    def test_공사_설계변경_review(self):
        d = self.decide("공사", 600_000_000, flags=frozenset({"설계변경"}))
        self.assertEqual(d.decision, "REVIEW")
        self.assertEqual(d.rule_id, "PRE-DESIGN-CHANGE")

    def test_post_execution_review_overrides_amount(self):
        d = self.decide("공사", 5_000_000_000, stage="사후")
        self.assertEqual(d.decision, "REVIEW")
        self.assertEqual(d.rule_id, "PRE-POST-EXECUTION")

    def test_urgent_flag_review(self):
        d = self.decide("공사", 5_000_000_000, flags=frozenset({"긴급"}))
        self.assertEqual(d.rule_id, "PRE-URGENT")

    def test_excluded_other_audit(self):
        self.assertEqual(self.decide("공사", 5_000_000_000, flags=frozenset({"복무감사"})).decision, "EXCLUDED")

    def test_precondition_priority_order(self):
        d = self.decide("공사", 5_000_000_000, stage="사후", flags=frozenset({"긴급"}))
        self.assertEqual(d.rule_id, "PRE-POST-EXECUTION")  # 먼저 배열된 규칙

    def test_unknown_type_is_review(self):
        d = self.decide("우주개발", 999_999_999_999)
        self.assertEqual(d.decision, "REVIEW")
        self.assertIsNone(d.threshold)

    def test_cumulative_amount_used(self):
        # 변경계약 누계 금액이 판정에 사용되는가 (물품: 단건 1,500만 미만이나 누계 2,500만)
        # ※ 공사 변경계약은 PRE-DESIGN-CHANGE(REVIEW) 선결이므로 물품으로 검증
        d = self.decide("물품구매", 15_000_000, cumulative_amount=25_000_000, flags=frozenset({"변경계약"}))
        self.assertEqual(d.amount, 25_000_000)
        self.assertEqual(d.decision, "TARGET")

    def test_negative_amount_rejected(self):
        with self.assertRaises(ValueError):
            self.decide("용역", -1)

    def test_reproducibility_3x(self):
        results = [self.decide("공사", 2_000_000_000).decision for _ in range(3)]
        self.assertEqual(results, ["TARGET"] * 3)

    def test_rubric_group_mapping(self):
        from audit_core.rules.target_check import rubric_group
        self.assertEqual(rubric_group("비종합공사"), "공사")
        self.assertEqual(rubric_group("용역"), "용역")
        self.assertEqual(rubric_group("민간자본보조"), "민간보조")
        self.assertEqual(rubric_group("예비비"), "기타")


class TestDeadline(unittest.TestCase):
    def setUp(self):
        self.cal = HolidayCalendar()

    def test_calendar_roll_plain(self):
        # 2026-07-06(월) + 7일 = 7/13(월), 휴일 아님
        r = compute_deadline(date(2026, 7, 6), 7, mode="calendar_roll", calendar=self.cal)
        self.assertEqual(r.due, date(2026, 7, 13))
        self.assertFalse(r.rolled)

    def test_calendar_roll_weekend(self):
        # 2026-07-04(토) + 7일 = 7/11(토) → 7/13(월) 이월
        r = compute_deadline(date(2026, 7, 4), 7, mode="calendar_roll", calendar=self.cal)
        self.assertEqual(r.due, date(2026, 7, 13))
        self.assertTrue(r.rolled)

    def test_calendar_roll_holiday_chain(self):
        # 추석 연휴: 2026-09-17(목) + 7일 = 9/24(추석연휴) → 9/25·26·27(일)·28(대체) 모두 휴일 → 9/29(화)
        r = compute_deadline(date(2026, 9, 17), 7, mode="calendar_roll", calendar=self.cal)
        self.assertEqual(r.due, date(2026, 9, 29))
        self.assertTrue(r.rolled)

    def test_business_mode(self):
        # 2026-02-13(금) + 근무일 3일: 14(토)15(일)16~18(설연휴) 제외 → 19(목)20(금)23(월)
        r = compute_deadline(date(2026, 2, 13), 3, mode="business", calendar=self.cal)
        self.assertEqual(r.due, date(2026, 2, 23))

    def test_audit_deadlines_chain(self):
        r = audit_deadlines(date(2026, 7, 6), self.cal)
        self.assertEqual(r["notify"].due, date(2026, 7, 13))
        # 조치결과: 통보기한 7/13 + 14일 = 7/27(월)
        self.assertEqual(r["action_report"].due, date(2026, 7, 27))

    def test_uncovered_year_flagged(self):
        r = compute_deadline(date(2027, 3, 1), 7, mode="calendar_roll", calendar=self.cal)
        self.assertFalse(r.calendar_covered)  # 2027 데이터 미등록 → 경고 플래그

    def test_reproducibility_3x(self):
        dues = [audit_deadlines(date(2026, 7, 6), self.cal)["notify"].due for _ in range(3)]
        self.assertEqual(len(set(dues)), 1)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(self.tmp.name)
        self.cal = HolidayCalendar()

    def tearDown(self):
        self.tmp.cleanup()

    def test_amount_band_no_exact_amount(self):
        self.assertEqual(amount_band(85_000_000), "5천만~1억")
        self.assertEqual(amount_band(49_999_999), "5천만 미만")
        self.assertEqual(amount_band(2_000_000_000), "10억 이상")

    def test_create_autonumber_and_due(self):
        e1 = self.ledger.create("재무과", "청사 보수공사", "공사", 250_000_000, date(2026, 7, 6), self.cal)
        e2 = self.ledger.create("농정과", "스마트팜 용역", "용역", 85_000_000, date(2026, 7, 6), self.cal)
        self.assertEqual(e1.entry_no, "2026-001")
        self.assertEqual(e2.entry_no, "2026-002")
        self.assertEqual(e1.notify_due, "2026-07-13")
        self.assertEqual(e1.amount_band, "2억~5억")  # 정확 금액 미기록

    def test_update_and_result_code_validation(self):
        e = self.ledger.create("재무과", "건", "공사", 250_000_000, date(2026, 7, 6), self.cal)
        u = self.ledger.update(e.entry_no, notified_date="2026-07-10", result_code="의견통보")
        self.assertEqual(u.result_code, "의견통보")
        with self.assertRaises(ValueError):
            self.ledger.update(e.entry_no, result_code="반려")  # 미정의 코드
        with self.assertRaises(ValueError):
            self.ledger.update(e.entry_no, dept="다른과")  # 접수 필드 수정 불가

    def test_overdue(self):
        e = self.ledger.create("재무과", "건", "공사", 250_000_000, date(2026, 7, 6), self.cal)
        self.assertEqual([x.entry_no for x in self.ledger.overdue(2026, date(2026, 7, 14))], [e.entry_no])
        self.ledger.update(e.entry_no, notified_date="2026-07-13")
        self.assertEqual(self.ledger.overdue(2026, date(2026, 7, 14)), [])

    def test_export_csv_xlsx(self):
        self.ledger.create("재무과", "건", "공사", 250_000_000, date(2026, 7, 6), self.cal)
        csv_p = self.ledger.export_csv(2026, Path(self.tmp.name) / "out.csv")
        xlsx_p = self.ledger.export_xlsx(2026, Path(self.tmp.name) / "out.xlsx")
        self.assertTrue(csv_p.exists() and csv_p.stat().st_size > 0)
        self.assertTrue(xlsx_p.exists() and xlsx_p.stat().st_size > 0)
        header = csv_p.read_text(encoding="utf-8-sig").splitlines()[0]
        self.assertIn("접수번호", header)


class TestPipeSkeleton(unittest.TestCase):
    def setUp(self):
        from daily_audit_pipe import Pipeline, parse_amount
        self.pipe = Pipeline()
        self.parse_amount = parse_amount

    def test_parse_amount(self):
        self.assertEqual(self.parse_amount("8500만원"), 85_000_000)
        self.assertEqual(self.parse_amount("2억"), 200_000_000)
        self.assertEqual(self.parse_amount("1억 5천만원"), 150_000_000)
        self.assertEqual(self.parse_amount("850,000,000원"), 850_000_000)
        self.assertIsNone(self.parse_amount("금액 없음"))

    def test_target_mode_target(self):
        # 편람 기준: 용역 10억 이상 대상
        out = self.pipe.pipe("대상? 용역 12억원", "m", [], {})
        self.assertIn("일상감사 대상", out)
        self.assertIn("잠정", out)

    def test_target_mode_not_target(self):
        out = self.pipe.pipe("대상? 용역 6500만원", "m", [], {})  # 별표: 용역 7천만 미만
        self.assertIn("대상 아님", out)

    def test_deadline_mode(self):
        out = self.pipe.pipe("기한 2026-07-06", "m", [], {})
        self.assertIn("2026-07-13", out)

    def test_guide_fallback(self):
        out = self.pipe.pipe("안녕", "m", [], {})
        self.assertIn("효규가영", out)

    def test_review_without_doc_asks_for_text(self):
        # 문서 본문 없이 검토 요청 → 붙여넣기 안내 (LLM 미호출)
        out = "".join(self.pipe.pipe("검토", "m", [], {}))
        self.assertIn("문서를 함께", out)

    def test_review_unknown_biztype_proceeds_with_기타(self):
        # 유형 미감지 시 중단하지 않고 '기타'(공통 축)로 진행 + 안내 (doc_type 단계 개선)
        doc = "점검 " + "사업비 30,000,000원. 이 문서에는 사업유형 키워드가 전혀 없습니다. " * 5  # 문서 신호(산식 라벨)는 있으나 유형 미감지
        gen = self.pipe.pipe(doc, "m", [], {})
        header = next(gen)  # 헤더까지만 소비 — 이후는 LLM 검토라 테스트에서 진행 안 함
        self.assertIn("미감지", header)
        self.assertIn("기타", header)


if __name__ == "__main__":
    unittest.main()
