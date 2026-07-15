"""FP(기능점수) 검산 테스트 — 소수 보정계수·퍼센트·N인수 곱셈 체인 + SW개발비 합산.

산식 수치는 KOSA SW사업 대가산정 가이드 구조(개발원가 = FP × 단가 × 보정계수,
SW개발비 = 개발원가 + 직접경비 + 이윤)를 따르되 임의 예시값 사용.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.agents.verifier import arithmetic_flags, check_arithmetic


class TestFpChain(unittest.TestCase):
    def test_소수계수_정상_체인(self):
        # 500 × 553,114 × 0.83 = 229,542,310 (정확)
        doc = "개발원가: 500FP × 553,114원 × 0.83 = 229,542,310원"
        self.assertEqual(arithmetic_flags(doc), [])

    def test_소수계수_결함_검출(self):
        # 계수 0.83을 빠뜨리고 계산한 값을 기재 — 큰 차이 → FLAG
        doc = "개발원가: 500FP × 553,114원 × 0.83 = 276,557,000원"
        flags = arithmetic_flags(doc)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].expected, 229_542_310)
        self.assertEqual(flags[0].claimed, 276_557_000)

    def test_다인수_보정계수_체인(self):
        # 4인수: 500 × 553,114 × 0.83 × 1.2 = 275,450,772
        doc = "500FP × 553,114원 × 0.83 × 1.2 = 275,450,772원"
        self.assertEqual(arithmetic_flags(doc), [])

    def test_반올림_관행은_허용오차내(self):
        # 497 × 553,114 × 0.79 = 217,168,884.02 → 원단위 절사 기재 217,168,884
        # 여기서 일부러 500원 어긋난 값(중간 반올림 흉내) — 오탐 없어야 함
        doc = "497FP × 553,114원 × 0.79 = 217,168,384원"
        self.assertEqual(arithmetic_flags(doc), [])

    def test_퍼센트_이윤_정상(self):
        doc = "이윤: 229,542,310원 × 25% = 57,385,578원"
        self.assertEqual(arithmetic_flags(doc), [])

    def test_퍼센트_이윤_결함(self):
        # 25%라 쓰고 30%로 계산한 값 기재 → FLAG
        doc = "이윤: 229,542,310원 × 25% = 68,862,693원"
        flags = arithmetic_flags(doc)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].expected, 57_385_578)

    def test_정수체인은_기존처럼_정확일치(self):
        # 소수·%가 없으면 허용오차 미적용 — 1원 차이도 FLAG (하위호환)
        doc = "1명 × 4 × 8,000,000원 = 32,000,001원"
        flags = arithmetic_flags(doc)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].expected, 32_000_000)

    def test_표렌더링_줄에서도_검출(self):
        # hwpx 파서의 '셀 | 셀' 행 렌더링 형태
        doc = "기능점수 산출 | 500FP × 553,114원 × 0.83 = 276,557,000원 | 비고"
        self.assertEqual(len(arithmetic_flags(doc)), 1)


class TestFpSum(unittest.TestCase):
    DOC_OK = ("개발원가: 229,542,310원\n"
              "직접경비: 15,000,000원\n"
              "이윤: 57,385,578원\n"
              "SW개발비: 301,927,888원")

    def test_합산_정상(self):
        self.assertEqual(arithmetic_flags(self.DOC_OK), [])

    def test_합산_결함_검출(self):
        doc = self.DOC_OK.replace("301,927,888", "310,000,000")
        flags = arithmetic_flags(doc)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].kind, "fp_sum")
        self.assertEqual(flags[0].expected, 301_927_888)
        self.assertEqual(flags[0].claimed, 310_000_000)

    def test_소프트웨어개발비_표기도_인식(self):
        doc = self.DOC_OK.replace("SW개발비", "소프트웨어 개발비")
        checks = [c for c in check_arithmetic(doc) if c.kind == "fp_sum"]
        self.assertTrue(checks and checks[0].match)

    def test_부가세포함_표기는_침묵(self):
        # 실물 산출내역서 표기: 'SW개발비(부가세 포함, 십만단위 절사)' — 3자 합산 불성립하므로 검산 제외
        doc = (self.DOC_OK.replace("SW개발비: 301,927,888원",
                                   "SW개발비(부가세 포함, 십만단위 절사): 310,000,000원"))
        self.assertEqual([c for c in check_arithmetic(doc) if c.kind == "fp_sum"], [])

    def test_라벨_불완전이면_침묵(self):
        # 이윤 라벨이 없으면 재구성하지 않음(보수 원칙)
        doc = "개발원가: 100원\n직접경비: 10원\nSW개발비: 999원"
        self.assertEqual([c for c in check_arithmetic(doc) if c.kind == "fp_sum"], [])

    def test_기존_소계부가세합계와_공존(self):
        doc = (self.DOC_OK.replace("301,927,888", "310,000,000")
               + "\n소계: 100원\n부가가치세: 10원\n합계: 999원")
        kinds = {f.kind for f in arithmetic_flags(doc)}
        self.assertEqual(kinds, {"fp_sum", "sum"})


if __name__ == "__main__":
    unittest.main()
