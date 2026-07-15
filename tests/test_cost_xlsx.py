"""xlsx 산출내역서 검산기 테스트 — 합성 통합문서 + 실물(있으면) 검증."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.parsers.cost_xlsx import CostSheetError, check_cost_sheet

REAL_BASE = Path("/private/tmp/claude-501/-Users-aidata-AI-Workspace/"
                 "26e4f879-618d-4efe-be9f-01b1861018ac/scratchpad/audit_real")
REAL_ORIG = REAL_BASE / "일상감사 검토요청(생성형 AI 플랫폼 구축 사업)외5/3. 산출내역서.xlsx"


def _make_sheet(tmp: Path, *, profit_amount=21_599_077, profit_rate=0.09,
                total=310_000_000, total_label="소프트웨어 개발비(부가세 포함, 십만단위 절사)"):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SW개발비 산정"
    rows = [
        ["○ 개발원가 산정"],
        ["총기능점수", None, "기능점수당 단가", None, "보정계수"],
        [None, None, None, None, "규모", "연계복잡성", "성능", "운영환경", "보안성", "개발원가"],
        [316, None, 605784, None, 1.28, 0.88, 1.05, 1, 1.06, 239_989_746.67],
        ["합계(보정 후 개발원가)", None, None, None, None, None, None, None, None, 239_989_746.67],
        ["이윤", None, None, None, None, None, None, None, profit_rate, profit_amount],
        ["직접경비", None, None, None, None, None, None, None, None, 20_700_000],
        ["부가세", None, None, None, None, None, None, None, 0.1, 28_228_882.39],
        [total_label, None, None, None, None, None, None, None, None, total],
        ["○ 직접경비"],
        ["구분", None, "산출내역", None, None, None, None, None, None, "금액"],
        ["여비", None, "150,000원*3인*10회", None, None, None, None, None, None, 4_500_000],
        ["합 계", None, None, None, None, None, None, None, None, 20_700_000],
    ]
    for r in rows:
        ws.append(r)
    p = tmp / "cost.xlsx"
    wb.save(p)
    return p


class TestCostSheet(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_정상_시트는_전부_통과(self):
        r = check_cost_sheet(_make_sheet(self.tmp))
        self.assertGreaterEqual(len(r.checks), 5)
        self.assertEqual(r.flags, [])

    def test_이윤_오계산_검출(self):
        r = check_cost_sheet(_make_sheet(self.tmp, profit_amount=25_000_000))
        kinds = {c.kind for c in r.flags}
        self.assertIn("fp_profit", kinds)

    def test_이윤율_상한_초과_검출(self):
        r = check_cost_sheet(_make_sheet(self.tmp, profit_rate=0.30, profit_amount=71_996_924))
        kinds = {c.kind for c in r.flags}
        self.assertIn("fp_profit_rate", kinds)

    def test_절사_초과_차이는_검출(self):
        # 절사 허용(100만원 미만)을 넘는 하향 차이 → FLAG
        r = check_cost_sheet(_make_sheet(self.tmp, total=305_000_000))
        self.assertIn("fp_total", {c.kind for c in r.flags})

    def test_비표준_서식은_에러(self):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.append(["아무 관계 없는 표"])
        p = self.tmp / "other.xlsx"
        wb.save(p)
        with self.assertRaises(CostSheetError):
            check_cost_sheet(p)


@unittest.skipUnless(REAL_ORIG.exists(), "실물 산출내역서 없음")
class TestRealCostSheet(unittest.TestCase):
    def test_실물_전건_통과_절사노트(self):
        r = check_cost_sheet(REAL_ORIG)
        self.assertEqual(r.flags, [])
        self.assertGreaterEqual(len(r.checks), 7)
        self.assertTrue(any("절사" in n for n in r.notes))


if __name__ == "__main__":
    unittest.main()
