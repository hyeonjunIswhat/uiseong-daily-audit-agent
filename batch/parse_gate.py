"""파싱 관문 검증 도구 (SPEC §7-3.5). 실제 샘플 문서의 추출 품질을 육안 대조.

사용: .venv/bin/python batch/parse_gate.py <파일> [파일2 ...]
지원: .hwpx(구조 보존) · .hwp(5.x 바이너리, 선형 텍스트) · .xlsx(산출내역서 검산)

출력(파일별):
1. 추출 통계 — 섹션·문자수·표 수·행/셀 수
2. **완전성 지표** — 원본 XML의 텍스트 런(hp:t) 총 문자수 대비 추출 문자수 비율.
   100% 미만이면 손실 지점을 찾아야 함(그림·수식 개체 등)
3. 금액 토큰 전수 — 관문 기준(금액 필드 95% 일치)의 대조 목록.
   원문(한글 뷰어)과 이 목록을 육안 대조해 일치율을 기록한다
4. 표 렌더링 미리보기 — 셀 경계 확인용

관문 통과 기준: 검증 대상 금액 필드의 95% 이상 원문 일치 (SPEC §8).
"""

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit_core.parsers.hwpx import HwpxParseError, _local, parse_hwpx  # noqa: E402


def _raw_t_chars(path: str) -> int:
    """원본 XML의 모든 hp:t 텍스트 총 문자수(공백 제외) — 완전성 분모."""
    total = 0
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not re.search(r"Contents/section\d+\.xml$", name):
                continue
            root = ElementTree.fromstring(zf.read(name))
            for el in root.iter():
                if _local(el.tag) == "t":
                    total += len(re.sub(r"\s", "", "".join(el.itertext())))
    return total


def inspect_hwp(path: str) -> None:
    from audit_core.parsers.hwp import HwpParseError, parse_hwp
    try:
        doc = parse_hwp(path)
    except HwpParseError as e:
        print(f"  ✗ hwp 파싱 실패: {e}")
        return
    money = re.findall(r"[0-9][0-9,]*\s*원", doc.text)
    print(f"  (hwp 5.x) 섹션 {doc.n_sections} · 텍스트 {len(doc.text):,}자 · 금액 토큰 {len(money)}건"
          + (": " + ", ".join(money[:20]) if money else ""))
    print("  --- 앞 8줄 ---")
    for line in doc.text.splitlines()[:8]:
        print(f"    {line}")


def inspect_xlsx(path: str) -> None:
    from audit_core.parsers.cost_xlsx import CostSheetError, check_cost_sheet
    try:
        r = check_cost_sheet(path)
    except CostSheetError as e:
        print(f"  ✗ 산출내역서 서식 인식 불가: {e}")
        return
    print(f"  (산출내역서 검산) 시트 '{r.sheet}' — 검산 {len(r.checks)}건, 불일치 {len(r.flags)}건")
    for c in r.checks:
        mark = "✓" if c.match else "✗"
        print(f"    {mark} [{c.kind}] {c.expr[:64]} → 재계산 {c.expected:,} vs 기재 {c.claimed:,}")
    for n in r.notes:
        print(f"    ℹ {n}")


def inspect(path: str) -> None:
    print(f"\n{'=' * 60}\n📄 {path}")
    low = path.lower()
    if low.endswith(".hwp"):
        inspect_hwp(path)
        return
    if low.endswith(".xlsx"):
        inspect_xlsx(path)
        return
    try:
        doc = parse_hwpx(path)
    except HwpxParseError as e:
        print(f"  ✗ 파싱 실패: {e}")
        return

    extracted = len(re.sub(r"\s", "", doc.text.replace("|", "")))
    raw = _raw_t_chars(path)
    ratio = extracted / raw * 100 if raw else 0.0

    print(f"  섹션 {doc.n_sections} · 텍스트 {len(doc.text):,}자 · 표 {len(doc.tables)}개")
    for i, t in enumerate(doc.tables, 1):
        print(f"    표{i}: {len(t)}행 × 최대 {max(len(r) for r in t)}셀")
    print(f"  완전성: 추출 {extracted:,} / 원본 텍스트런 {raw:,} = {ratio:.1f}%"
          + ("  ✓" if ratio >= 99.5 else "  ⚠ 손실 의심 — 미추출 개체 확인 필요"))

    money = doc.money_tokens
    print(f"  금액 토큰 {len(money)}건" + (": " + ", ".join(money[:30]) if money else " (금액 없는 문서)"))
    if len(money) > 30:
        print(f"    … 외 {len(money) - 30}건")

    print("  --- 본문 앞 8줄 ---")
    for line in doc.text.splitlines()[:8]:
        print(f"    {line}")
    if doc.tables:
        print("  --- 표1 앞 3행 ---")
        for row in doc.tables[0][:3]:
            print(f"    {row}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for p in sys.argv[1:]:
        inspect(p)
