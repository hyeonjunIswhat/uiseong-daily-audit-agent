"""hwpx 파서 (구현 3.5단계, 파싱 관문). 표준 라이브러리만 사용 — 폐쇄망 무의존.

hwpx는 zip + HWPML(XML)이라 kordoc 없이 직접 추출한다(kordoc은 구버전 바이너리
.hwp·PDF 전용으로 축소). 실측 근거: 실제 공고문 hwpx(표 2·셀 93)에서 본문·표가
완전 추출됨을 확인(2026-07-09). 구조: section > p > run > (t | tbl),
tbl > tr > tc > subList > p.

출력 설계:
- text: 문서 순서 그대로의 선형 텍스트. **표는 행 단위로 '셀 | 셀 | …' 렌더링** —
  라벨과 금액이 한 줄에 놓여 verifier의 산식 검산 정규식(_LABELED 등)과 그대로
  호환된다(예: "합계 | 79,860,000원").
- tables: 표 구조 보존(행×셀 문자열) — 파싱 관문 육안 대조·후속 FP 검산용.

한계(관문 검증 시 확인 대상): 셀 병합은 펼치지 않고 기재된 셀만 나열, 중첩 표는
바깥 셀 텍스트로 평탄화, 그림·수식 개체 내 텍스트는 미추출.
"""

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

_SECTION_RE = re.compile(r"Contents/section(\d+)\.xml$")


class HwpxParseError(Exception):
    pass


@dataclass
class ParsedDoc:
    text: str                                   # 본문+표 선형 텍스트(검토·검산 입력)
    tables: list[list[list[str]]] = field(default_factory=list)  # [표][행][셀]
    n_sections: int = 0

    @property
    def money_tokens(self) -> list[str]:
        """문서 내 금액 표기 전수 — 파싱 관문(금액 95% 일치) 육안 대조용."""
        return re.findall(r"[0-9][0-9,]*\s*원", self.text)


def _local(tag: str) -> str:
    """'{namespace}tag' → 'tag'."""
    return tag.rsplit("}", 1)[-1]


def _para_text(p) -> str:
    """문단(hp:p) 안 텍스트 런(hp:t)만 이어붙인다. 표 개체는 별도 처리."""
    parts = []
    for run in p:
        if _local(run.tag) != "run":
            continue
        for item in run:
            if _local(item.tag) == "t":
                parts.append("".join(item.itertext()))
    return "".join(parts).strip()


def _para_tables(p) -> list:
    return [
        item
        for run in p if _local(run.tag) == "run"
        for item in run if _local(item.tag) == "tbl"
    ]


def _cell_text(tc) -> str:
    """셀(hp:tc) → 텍스트. subList 문단들을 공백으로 잇고, 중첩 표는 평탄화."""
    lines = []
    for sub in tc:
        if _local(sub.tag) != "subList":
            continue
        for p in (e for e in sub if _local(e.tag) == "p"):
            t = _para_text(p)
            if t:
                lines.append(t)
            for inner in _para_tables(p):  # 중첩 표 → 셀 텍스트로 평탄화
                for row in _table_grid(inner):
                    lines.append(" | ".join(row))
    return " ".join(lines).strip()


def _table_grid(tbl) -> list[list[str]]:
    """표(hp:tbl) → [행][셀] 문자열. tbl>tr>tc 직계 구조."""
    grid = []
    for tr in (e for e in tbl if _local(e.tag) == "tr"):
        row = [_cell_text(tc) for tc in tr if _local(tc.tag) == "tc"]
        if any(row):
            grid.append(row)
    return grid


def _parse_section(xml_bytes: bytes, lines: list[str], tables: list) -> None:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as e:
        raise HwpxParseError(f"섹션 XML 해석 실패: {e}") from e
    for p in (e for e in root if _local(e.tag) == "p"):
        t = _para_text(p)
        if t:
            lines.append(t)
        for tbl in _para_tables(p):
            grid = _table_grid(tbl)
            if grid:
                tables.append(grid)
                lines.extend(" | ".join(row) for row in grid)


def parse_hwpx(src: str | Path | bytes) -> ParsedDoc:
    """hwpx 파일(경로 또는 바이트) → ParsedDoc. hwpx가 아니면 HwpxParseError."""
    fp = BytesIO(src) if isinstance(src, bytes) else str(src)
    try:
        zf = zipfile.ZipFile(fp)
    except (zipfile.BadZipFile, FileNotFoundError, OSError) as e:
        raise HwpxParseError(f"hwpx(zip) 열기 실패: {e}") from e

    with zf:
        sections = sorted(
            (m for m in zf.namelist() if _SECTION_RE.search(m)),
            key=lambda m: int(_SECTION_RE.search(m).group(1)),
        )
        if not sections:
            raise HwpxParseError("Contents/section*.xml 없음 — hwpx 형식이 아님")
        lines: list[str] = []
        tables: list[list[list[str]]] = []
        for name in sections:
            _parse_section(zf.read(name), lines, tables)

    return ParsedDoc(text="\n".join(lines), tables=tables, n_sections=len(sections))
