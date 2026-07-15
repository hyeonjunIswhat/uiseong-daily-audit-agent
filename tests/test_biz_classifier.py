"""BusinessClassifier — 사업성격 해석 → 법정유형 매핑 (사용자 명세 케이스)."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.rules.biz_classifier import BusinessClassifier, to_target_type
from audit_core.rules.target_check import TargetInput, check_target


class TestBusinessClassifier(unittest.TestCase):
    def setUp(self):
        self.c = BusinessClassifier()

    def test_sw_platform_rfp_is_service(self):
        p = self.c.classify("사업명\n생성형 AI 플랫폼 구축 사업\n제안요청서\n소프트웨어 개발 범위…")
        self.assertEqual(p.primary_type, "용역")
        self.assertEqual(p.subtype, "SW개발용역")
        self.assertEqual(p.confidence, "high")   # 사업명 안의 '구축'
        self.assertTrue(p.evidence)

    def test_maintenance_is_service(self):
        p = self.c.classify("사업명: 행정정보시스템 고도화 및 유지관리")
        self.assertEqual(p.primary_type, "용역")
        self.assertIn(p.subtype, ("SW개발용역", "유지관리용역"))

    def test_gpu_server_is_goods_with_ancillary_note(self):
        p = self.c.classify("사업명: GPU 서버 구매 및 설치\n납품 조건…")
        self.assertEqual(p.primary_type, "물품")
        self.assertEqual(p.subtype, "장비도입")
        self.assertIn("설치", p.mixed_notes)     # 설치는 부수 — 메모만

    def test_license_is_goods(self):
        p = self.c.classify("사업명: 한컴 SDK 서버라이선스 구매")
        self.assertEqual(p.primary_type, "물품")
        self.assertEqual(p.subtype, "라이선스구매")

    def test_event_agency_is_service(self):
        p = self.c.classify("사업명: 의성 스카이 디펜스런 행사 운영 대행")
        self.assertEqual(p.primary_type, "용역")
        self.assertEqual(p.subtype, "행사대행")

    def test_telecom_construction(self):
        p = self.c.classify("사업명: 정보통신 설비 공사\n배선 및 시공 범위…")
        self.assertEqual(p.primary_type, "비종합공사")
        self.assertEqual(p.subtype, "정보통신공사")

    def test_negotiation_is_method_not_type(self):
        p = self.c.classify("계약방법: 협상에 의한 계약으로 추진")
        self.assertEqual(p.contract_method, "협상에 의한 계약")
        self.assertNotIn("협상", p.primary_type)  # 유형이 되면 안 됨

    def test_install_alone_is_not_construction(self):
        p = self.c.classify("사업명: 회의실 장비 설치")
        self.assertNotIn("공사", p.primary_type)

    def test_no_legal_label_still_classified(self):
        # 법정 라벨('용역' 등)이 한 글자도 없어도 분류되어야 한다
        doc = "사업명: 군정 홍보 챗봇 개발\n주요 과업: 대화 시나리오 설계, 플랫폼 연계"
        p = self.c.classify(doc)
        self.assertEqual(p.primary_type, "용역")
        self.assertNotEqual(p.confidence, "low")

    def test_mixed_goods_and_service(self):
        doc = ("사업명: 스마트 관제 시스템 구축 및 서버 장비 도입\n"
               "소프트웨어 개발과 서버·스토리지 납품을 포함")
        p = self.c.classify(doc)
        self.assertTrue(p.mixed)
        self.assertIn("주된 계약 목적물", p.mixed_notes)
        self.assertEqual(p.confidence, "medium")  # 혼합은 신뢰 하향

    def test_classifier_feeds_rule_engine_not_decides(self):
        # 분류기는 유형 후보만 — 대상판정은 결정론 룰엔진이 한다
        p = self.c.classify("사업명: 생성형 AI 플랫폼 구축\n추정금액: 310,000,000원")
        t = to_target_type(p)
        self.assertEqual(t, "용역")
        d = check_target(TargetInput(biz_type=t, amount=310_000_000))
        self.assertEqual(d.decision, "TARGET")   # 용역 7천만↑ — 별표 기준은 룰엔진 소관

    def test_unknown_returns_confirm(self):
        p = self.c.classify("특이 표현이 전혀 없는 일반 안내문입니다.")
        self.assertEqual(p.primary_type, "확인필요")
        self.assertEqual(p.confidence, "low")


if __name__ == "__main__":
    unittest.main()
