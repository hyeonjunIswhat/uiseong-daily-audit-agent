"""PDF 파서 — 텍스트 레이어 추출 (pypdf, 2026-07-15).

경위: PDF는 외부 파서(kordoc :8001) 담당이었으나 미기동 상태가 길어져
자체 파서로 대체한다. 텍스트 레이어가 있는 공문 PDF가 대상이며,
스캔 이미지 PDF(텍스트 없음)는 OCR 필요로 명시 실패한다(조용한 빈 결과 금지).

의존성: pypdf(순수 파이썬) — open-webui·pipelines 컨테이너에는 기본 탑재.
"""

from dataclasses import dataclass
from pathlib import Path

MIN_TEXT_PER_PAGE = 20  # 페이지당 평균 이 미만이면 스캔본으로 간주


class PdfParseError(Exception):
    pass


@dataclass
class ParsedPdf:
    text: str
    n_pages: int


def parse_pdf(src: str | Path) -> ParsedPdf:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # 폐쇄망 반입 누락 대비 — 원인 명시
        raise PdfParseError(f"pypdf 미설치: {e}")

    try:
        reader = PdfReader(str(src))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as e:
        raise PdfParseError(f"PDF 해석 실패: {e}")

    text = "\n".join(t for t in pages if t)
    n = len(pages) or 1
    if len(text) < MIN_TEXT_PER_PAGE * n:
        raise PdfParseError(
            f"텍스트 레이어가 거의 없음({len(text)}자/{n}쪽) — 스캔 이미지 PDF로 보이며 OCR이 필요합니다")
    return ParsedPdf(text=text, n_pages=n)
