"""관문 내비게이터 P1 + 파이프 UX(자동 인식·스트리밍) 테스트."""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "pipelines"))

from audit_core.rules.gates import GateSet, format_backplan, format_overview


class TestGateSet(unittest.TestCase):
    def setUp(self):
        self.gs = GateSet()

    def test_load_5_gates(self):
        self.assertEqual(len(self.gs.gates), 5)
        labels = {g.label for g in self.gs.gates}
        self.assertIn("보안성검토", labels)
        self.assertIn("일상감사", labels)

    def test_backplan_order_and_critical(self):
        plans = self.gs.backplan(date(2026, 10, 1))
        # 착수마감 오름차순 — 가장 오래 걸리는 보안성검토가 첫 번째
        self.assertEqual(plans[0].gate.key, "security_review")
        self.assertTrue(plans[0].is_critical)
        self.assertEqual(sum(1 for p in plans if p.is_critical), 1)
        # 9주 전 = 2026-07-30(목) — 근무일이므로 그대로
        self.assertEqual(plans[0].start_by, date(2026, 7, 30))

    def test_backplan_holiday_rolls_backward(self):
        # 착수마감이 주말에 걸리면 직전 근무일로 앞당김:
        # 공고 2026-11-01(일) → 1주 관문 착수마감 10-25(일) → 10-23(금)
        plans = self.gs.backplan(date(2026, 11, 1))
        one_week = [p for p in plans if p.gate.lead_weeks == 1]
        for p in one_week:
            self.assertEqual(p.start_by, date(2026, 10, 23))

    def test_formatters(self):
        ov = format_overview(self.gs)
        self.assertIn("크리티컬 패스", ov)
        self.assertIn("보안성검토", ov)
        bp = format_backplan(self.gs, date(2026, 10, 1))
        self.assertIn("2026-07-30", bp)
        self.assertIn("사후 의무", bp)


class TestPipeUX(unittest.TestCase):
    def setUp(self):
        from daily_audit_pipe import Pipeline
        self.pipe = Pipeline()

    def test_관문_overview(self):
        out = self.pipe.pipe("관문", "m", [], {})
        self.assertIn("5관문", out)

    def test_관문_backplan(self):
        out = self.pipe.pipe("관문 2026-10-01", "m", [], {})
        self.assertIn("2026-07-30", out)

    def test_짧은_인사는_가이드(self):
        out = self.pipe.pipe("안녕", "m", [], {})
        self.assertIn("효규가영", out)

    def test_첫턴은_배너_표시(self):
        out = self.pipe.pipe("대상? 용역 12억", "m", [], {})
        self.assertIn("첫 대화 안내", out)      # 배너
        self.assertIn("일상감사 대상", out)      # 본 기능 결과도 함께

    def test_두번째턴은_배너_없음(self):
        history = [{"role": "user", "content": "대상? 용역 12억"},
                   {"role": "assistant", "content": "✅ 일상감사 대상"}]
        out = self.pipe.pipe("대상? 물품 3억", "m", history, {})
        self.assertNotIn("첫 대화 안내", out)

    def test_도움말은_형식별_예시(self):
        out = self.pipe.pipe("도움말", "m", [], {})
        self.assertIn("이렇게 쓰시면 됩니다", out)
        self.assertIn("의견서 초안", out)

    def test_긴_본문은_자동_자가점검(self):
        doc = "용역 사업 추진 문서. 사업비 30,000,000원. " + "내용 " * 60  # 문서 신호(유형·라벨 금액) 포함, 150자 이상
        gen = self.pipe.pipe(doc, "m", [], {})
        header = next(gen)
        self.assertIn("자가점검", header)
        self.assertIn("자동 인식", header)

    def test_스트리밍_진행로그와_결과(self):
        # Orchestrator를 모의로 바꿔 스레드-큐 스트리밍 경로 전체를 검증
        from audit_core.orchestrator import ReviewReport

        class FakeOrch:
            def self_check(self, group, doc, progress=None, **kwargs):
                progress("1단계 진행")
                progress("2단계 진행")
                return ReviewReport(biz_type=group, provisional_rubric=True)

        doc = "점검 용역 사업 본문입니다. " + "내용 " * 60
        with mock.patch("audit_core.orchestrator.Orchestrator", return_value=FakeOrch()):
            chunks = list(self.pipe.pipe(doc, "m", [], {}))
        joined = "".join(chunks)
        self.assertIn("- 1단계 진행", joined)
        self.assertIn("- 2단계 진행", joined)
        self.assertIn("자가점검 결과", joined)  # format_self_check 산출
        # 진행 로그가 결과보다 먼저 스트리밍되는지
        self.assertLess(joined.index("1단계 진행"), joined.index("자가점검 결과"))


if __name__ == "__main__":
    unittest.main()
