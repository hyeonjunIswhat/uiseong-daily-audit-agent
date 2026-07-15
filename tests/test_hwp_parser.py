"""hwp(5.x 바이너리) 파서 테스트 — 형식 판별 + 실물(있으면) 추출 검증."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.parsers.hwp import HwpParseError, parse_hwp

REAL_BASE = Path("/private/tmp/claude-501/-Users-aidata-AI-Workspace/"
                 "26e4f879-618d-4efe-be9f-01b1861018ac/scratchpad/audit_real")
REAL_REQ = REAL_BASE / "일상감사 검토요청(생성형 AI 플랫폼 구축 사업)외5/1. 일상감사요청서.hwp"
REAL_OPINION = REAL_BASE / "일상감사 검토결과 통보(의성군 인공지능 GPU 서버 구매)외1/일상감사 의견서.hwp"


class TestFormatGuards(unittest.TestCase):
    def test_ole_아닌_파일은_에러(self):
        import tempfile
        p = Path(tempfile.mkdtemp()) / "not.hwp"
        p.write_bytes(b"plain text, not an OLE compound file at all........")
        with self.assertRaises(HwpParseError):
            parse_hwp(p)

    def test_hwpx는_hwp_아님(self):
        # hwpx(zip)를 hwp 파서에 넣으면 명확한 에러 (조용한 오동작 금지)
        import io
        import tempfile
        import zipfile
        p = Path(tempfile.mkdtemp()) / "fake.hwp"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", "application/hwp+zip")
        p.write_bytes(buf.getvalue())
        with self.assertRaises(HwpParseError):
            parse_hwp(p)


@unittest.skipUnless(REAL_REQ.exists(), "실물 hwp 없음")
class TestRealHwp(unittest.TestCase):
    def test_요청서_서식_추출(self):
        d = parse_hwp(REAL_REQ)
        self.assertIn("일 상 감 사 요 청 서", d.text)
        self.assertIn("의성군 생성형 AI 플랫폼 구축", d.text)
        self.assertIn("의성군 일상감사 규정", d.text)
        self.assertIn("310,000,000", d.text)

    def test_의견서_4축_구조_추출(self):
        d = parse_hwp(REAL_OPINION)
        self.assertIn("합법성 및 필요성", d.text)
        self.assertIn("계약방법 및 절차의 적정성", d.text)


if __name__ == "__main__":
    unittest.main()
