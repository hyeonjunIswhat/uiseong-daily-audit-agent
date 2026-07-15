"""PDF 파서 — 텍스트 레이어 추출·스캔본 명시 실패."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.parsers.pdf import ParsedPdf, PdfParseError, parse_pdf

DATA = Path("/Users/aidata/AI_Workspace/일상감사 데이터")
REAL = DATA / "일상감사 검토(의성군 인공지능 GPU 서버 구매)외2/일상감사 검토(의성군 인공지능 GPU 서버 구매).pdf"
SCANNED = DATA / "일상감사 검토요청(생성형 AI 플랫폼 구축 사업)외5/5. 2026년 인공지능 행정혁신 추진계획.pdf"


class TestPdfParser(unittest.TestCase):
    @unittest.skipUnless(REAL.exists(), "실물 없음")
    def test_real_official_pdf(self):
        r = parse_pdf(REAL)
        self.assertIsInstance(r, ParsedPdf)
        self.assertIn("일상감사", r.text)
        self.assertGreater(len(r.text), 300)

    @unittest.skipUnless(SCANNED.exists(), "실물 없음")
    def test_scanned_pdf_fails_loudly(self):
        # 스캔본은 조용한 빈 결과가 아니라 'OCR 필요'로 명시 실패해야 한다
        with self.assertRaises(PdfParseError) as ctx:
            parse_pdf(SCANNED)
        self.assertIn("OCR", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
