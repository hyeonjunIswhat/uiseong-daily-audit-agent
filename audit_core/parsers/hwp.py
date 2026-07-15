"""구버전 .hwp(HWP 5.x 바이너리) 텍스트 추출기 — olefile + zlib만 사용.

hwp 5.x = OLE 복합문서. BodyText/Section* 스트림이 (기본) raw-deflate 압축된
레코드 열이고, 레코드 헤더는 4바이트(tagid 10bit | level 10bit | size 12bit,
size=0xFFF면 다음 4바이트가 실제 크기). HWPTAG_PARA_TEXT(=0x43+0? 문단 텍스트,
tagid 67)의 페이로드가 UTF-16LE 문자열이며, 코드 0~31은 제어문자다:
  - 확장 컨트롤(1,2,3,11,12,14~18,21~23): 뒤따르는 7워드(총 8워드)가 컨트롤 데이터
  - 인라인(4~9,19,20): 마찬가지로 8워드 소비
  - 13(문단 끝) → 줄바꿈, 나머지(10,24~31 등) 1워드 스킵

용도: 일상감사 실물 문서(.hwp 요청서·의견서) 텍스트 인입. 표 구조는 보존하지
않는다(셀 문단이 순서대로 나열됨) — 구조 검산이 필요한 산출내역서는 xlsx 경로
(cost_xlsx.py)를 쓴다. hwpx는 parsers/hwpx.py(구조 보존) 사용.
"""

import zlib
from dataclasses import dataclass
from pathlib import Path

_HWP5_SIGNATURE = b"HWP Document File"
_TAG_PARA_TEXT = 67
# 8워드(자기 자신 + 7워드)를 소비하는 제어문자 집합 (HWP 5.0 스펙 표 60)
_EXTENDED = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}


class HwpParseError(Exception):
    pass


@dataclass
class ParsedHwp:
    text: str
    n_sections: int


def _decode_para_text(payload: bytes) -> str:
    out = []
    i = 0
    n = len(payload)
    while i + 1 < n:
        code = int.from_bytes(payload[i:i + 2], "little")
        if code in _EXTENDED:
            i += 16  # 컨트롤 워드 8개(16바이트) 소비
            continue
        if code < 32:
            if code == 13:
                out.append("\n")
            i += 2
            continue
        out.append(chr(code))
        i += 2
    return "".join(out)


def _iter_records(data: bytes):
    i = 0
    n = len(data)
    while i + 4 <= n:
        hdr = int.from_bytes(data[i:i + 4], "little")
        tag = hdr & 0x3FF
        size = (hdr >> 20) & 0xFFF
        i += 4
        if size == 0xFFF:
            if i + 4 > n:
                break
            size = int.from_bytes(data[i:i + 4], "little")
            i += 4
        yield tag, data[i:i + size]
        i += size


def parse_hwp(src: str | Path) -> ParsedHwp:
    """hwp 5.x 파일 → 선형 텍스트. hwp 형식이 아니면 HwpParseError."""
    import olefile

    try:
        ole = olefile.OleFileIO(str(src))
    except OSError as e:
        raise HwpParseError(f"OLE 열기 실패(hwp 5.x 아님?): {e}") from e

    with ole:
        try:
            header = ole.openstream("FileHeader").read()
        except OSError as e:
            raise HwpParseError("FileHeader 스트림 없음 — hwp 형식 아님") from e
        if not header.startswith(_HWP5_SIGNATURE):
            raise HwpParseError("HWP 5.x 시그니처 불일치")
        flags = int.from_bytes(header[36:40], "little")
        compressed = bool(flags & 1)
        if flags & 2:
            raise HwpParseError("암호화된 hwp — 추출 불가")

        sections = sorted(
            (e for e in ole.listdir() if e[0] == "BodyText"),
            key=lambda e: int("".join(ch for ch in e[1] if ch.isdigit()) or 0),
        )
        if not sections:
            raise HwpParseError("BodyText 섹션 없음")

        lines: list[str] = []
        for entry in sections:
            data = ole.openstream(entry).read()
            if compressed:
                try:
                    data = zlib.decompress(data, -15)
                except zlib.error as e:
                    raise HwpParseError(f"{'/'.join(entry)} 압축 해제 실패: {e}") from e
            for tag, payload in _iter_records(data):
                if tag == _TAG_PARA_TEXT:
                    t = _decode_para_text(payload).strip()
                    if t:
                        lines.append(t)

    return ParsedHwp(text="\n".join(lines), n_sections=len(sections))
