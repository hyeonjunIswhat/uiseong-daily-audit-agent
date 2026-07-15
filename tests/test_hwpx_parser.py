"""hwpx 파서 테스트 — 합성 hwpx(인메모리 zip)로 구조·검산 연동을 검증하고,
실제 샘플(Downloads 공고문)이 있으면 완전성까지 확인한다."""

import io
import sys
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.agents.verifier import arithmetic_flags
from audit_core.parsers.hwpx import HwpxParseError, parse_hwpx

NS = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'

REAL_SAMPLE = Path("/Users/aidata/Downloads/제안서 평가위원(후보자) 모집 공고문.hwpx")


def _p(text: str) -> str:
    return f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>"


def _cell(text: str) -> str:
    return f"<hp:tc><hp:subList>{_p(text)}</hp:subList></hp:tc>"


def _table(rows: list[list[str]]) -> str:
    trs = "".join(f"<hp:tr>{''.join(_cell(c) for c in row)}</hp:tr>" for row in rows)
    return f"<hp:p><hp:run><hp:tbl>{trs}</hp:tbl></hp:run></hp:p>"


def _make_hwpx(body_xml: str, section_name: str = "Contents/section0.xml") -> bytes:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><hp:sec {NS}>{body_xml}</hp:sec>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr(section_name, xml)
    return buf.getvalue()


class TestHwpxParser(unittest.TestCase):
    def test_paragraphs_and_split_runs_joined(self):
        # 한 문단이 여러 런으로 쪼개져도 한 줄로 복원
        body = ('<hp:p><hp:run><hp:t>용역명: 의</hp:t></hp:run>'
                '<hp:run><hp:t>성군 AI 구축</hp:t></hp:run></hp:p>' + _p("2. 개요"))
        doc = parse_hwpx(_make_hwpx(body))
        self.assertEqual(doc.text.splitlines()[0], "용역명: 의성군 AI 구축")
        self.assertEqual(doc.n_sections, 1)

    def test_table_rows_rendered_inline(self):
        body = _p("원가계산서") + _table([["항목", "금액"], ["소계", "72,600,000원"]])
        doc = parse_hwpx(_make_hwpx(body))
        self.assertIn("소계 | 72,600,000원", doc.text)
        self.assertEqual(doc.tables, [[["항목", "금액"], ["소계", "72,600,000원"]]])
        self.assertEqual(doc.money_tokens, ["72,600,000원"])

    def test_table_money_flows_to_arithmetic_check(self):
        # 표 안의 소계/부가세/합계가 검산 정규식에 걸리는지 (합계 오류 심음)
        body = _table([
            ["소계", "72,600,000원"],
            ["부가가치세", "7,260,000원"],
            ["합계", "85,000,000원"],
        ])
        doc = parse_hwpx(_make_hwpx(body))
        flags = arithmetic_flags(doc.text)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].expected, 79_860_000)
        self.assertEqual(flags[0].claimed, 85_000_000)

    def test_multi_section_order(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i, txt in [(0, "첫 섹션"), (1, "둘째 섹션")]:
                xml = f'<?xml version="1.0"?><hp:sec {NS}>{_p(txt)}</hp:sec>'
                zf.writestr(f"Contents/section{i}.xml", xml)
        doc = parse_hwpx(buf.getvalue())
        self.assertEqual(doc.text.splitlines(), ["첫 섹션", "둘째 섹션"])
        self.assertEqual(doc.n_sections, 2)

    def test_not_a_zip_raises(self):
        with self.assertRaises(HwpxParseError):
            parse_hwpx(b"HWP Document File binary...")  # 구버전 .hwp 등

    def test_zip_without_sections_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("something.txt", "not hwpx")
        with self.assertRaises(HwpxParseError):
            parse_hwpx(buf.getvalue())


@unittest.skipUnless(REAL_SAMPLE.exists(), "실샘플(Downloads 공고문) 없음")
class TestRealSample(unittest.TestCase):
    def test_announcement_extraction(self):
        doc = parse_hwpx(REAL_SAMPLE)
        self.assertGreater(len(doc.text), 3000)
        self.assertEqual(len(doc.tables), 2)
        self.assertIn("제안서 평가위원(후보자) 모집 공고", doc.text)
        self.assertIn("의성군 생성형 AI 플랫폼 구축", doc.text)
        # 표1 = 등록신청서 서식
        self.assertIn("제안서 평가위원(후보자) 등록 신청서", doc.tables[0][0][0])


if __name__ == "__main__":
    unittest.main()
