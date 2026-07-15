"""문서 간 정합성 대조 — A5.5 (설계서 §2·§7, 마스터 SOP ⑤, REBUILD 회차 2).

여러 문서(요청서·검토서·산출내역·RFP 등) 사이에서 같은 사실이 다르게 적힌 것을
결정론으로 잡는다. 실물 골든 지적("산출내역서 내 타 사업 명칭 잔존")이 이 유형.

대조 항목(v1 — 명확한 모순만, 보수 원칙):
  사업명   문서마다 '사업명·업무(사업)명·건명' 라벨 값이 서로 다르면(타 사업 명칭 잔존)
  총액     '사업비·총사업비·추정금액' 계열 금액이 문서 간 상이(천원·백만원 단위 환산)
  배점     '기술/가격' 평가 배점이 문서 간 상이 + 문서 내 합계≠100
  사업기간 'N개월' 표기가 문서 간 상이

앵커(A3 첫 실체): 모든 검출값은 {문서 라벨, 줄번호, 원문} 좌표를 갖는다 —
의견서의 "A와 B에 기재된 ~가 상이함" 서술이 원문 위치로 검증 가능해진다.

서술은 SOP ⑤ 표준형: "A와 B에 기재된 ~가 아래와 같이 상이함 → 수치 나열 →
일치시켜 정정하시기 바람". 사실 불일치는 판단이 아니라 확인이므로 종결구
조치요구 등급이 기본 허용된다.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal

from audit_core.rules.completeness import RequiredDocs


@dataclass(frozen=True)
class Extract:
    """문서에서 뽑은 값 1건 — 발췌 앵커 포함."""

    doc: str        # 문서 라벨(분리기가 붙임)
    line_no: int    # 1-기반 줄번호(해당 문서 내)
    raw: str        # 원문 발췌(그 줄)
    value: str      # 정규화 값(비교용)


@dataclass
class CrossFlag:
    kind: str                      # 사업명 | 총액 | 배점 | 사업기간
    note: str                      # SOP ⑤ 표준 서술
    extracts: list[Extract] = field(default_factory=list)


@dataclass(frozen=True)
class DocPart:
    label: str
    text: str


# ── 번들 분리 (표제 줄 = 문서 경계) ──────────────────────────

def split_bundle(text: str, catalog: RequiredDocs | None = None) -> list[DocPart]:
    """붙여넣기 번들을 표제 줄 기준으로 문서 단위로 나눈다.

    보수 원칙: 표제가 2개 미만이면 나누지 않고 단일 문서로 취급(교차 대조 스킵).
    같은 유형이 반복되면 라벨에 번호를 붙인다.
    """
    catalog = catalog or RequiredDocs()
    lines = text.splitlines()
    bounds: list[tuple[int, str]] = []  # (줄 인덱스, 라벨)
    for i, ln in enumerate(lines):
        hit = catalog.title_line_key(ln)
        if hit:
            bounds.append((i, hit[1]))
    if len(bounds) < 2:
        return [DocPart("문서", text)]

    seen: dict[str, int] = {}
    parts: list[DocPart] = []
    for n, (start, label) in enumerate(bounds):
        end = bounds[n + 1][0] if n + 1 < len(bounds) else len(lines)
        body = "\n".join(lines[start:end])
        # 첨부 목록의 서류명 줄("산출내역서 1부") 같은 극소 조각은 문서가 아니라
        # 언급이므로 직전 문서에 흡수한다(보수 원칙 — 가짜 문서로 대조 오염 방지).
        # 실측: 실물 첨부 조각 54자 / 최소 실문서 본문 ~70자 → 경계 60자
        if len(body.replace(" ", "")) < 60 and parts:
            parts[-1] = DocPart(parts[-1].label, parts[-1].text + "\n" + body)
            continue
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label}#{seen[label]}"
        parts.append(DocPart(label, body))
    return parts if len(parts) >= 2 else [DocPart("문서", text)]


# ── 값 추출기 ────────────────────────────────────────────────

def _labeled_value(part: DocPart, label_re: re.Pattern) -> list[tuple[int, str, str]]:
    """'라벨: 값' 또는 '라벨 줄 다음 줄 = 값'(hwp 서식 표) 패턴 추출.
    반환: (줄번호, 그 줄 원문, 값 문자열)."""
    out = []
    lines = part.text.splitlines()
    for i, ln in enumerate(lines):
        compact = ln.replace(" ", "")
        m = label_re.match(compact)
        if not m:
            continue
        rest = compact[m.end():].lstrip(":：|·-—")
        if rest:
            out.append((i + 1, ln.strip(), rest))
        else:  # 서식 표 — 값은 다음 비어있지 않은 줄
            for j in range(i + 1, min(i + 3, len(lines))):
                nxt = lines[j].replace(" ", "")
                if nxt:
                    out.append((j + 1, lines[j].strip(), nxt))
                    break
    return out


_NAME_LABEL = re.compile(r"^[①-⑳•o○\-\d.]*(?:사업명칭|업무\(사업\)명|사업명|건명)")
_AMOUNT_LABEL = re.compile(r"^[①-⑳•o○\-\d.]*(?:총사업비|사업비|추정금액|추정가격)")
_PERIOD_LABEL = re.compile(r"^[①-⑳•o○\-\d.]*사업기간")

_AMOUNT_RE = re.compile(r"금?([\d,]+)(천원|백만원|억원|원)")
# '12월'(달력 월)과의 오인을 막기 위해 명시적 '개월'만 인정(보수 원칙 — 실물
# 요청서 "∼'26.12월" vs 검토서 "(9월)"이 기간 불일치로 오탐되던 사례)
_MONTHS_RE = re.compile(r"(\d+)\s*개월")
_UNIT = {"원": 1, "천원": 1_000, "백만원": 1_000_000, "억원": 100_000_000}

_SCORE_RE = re.compile(r"기술[^\d]{0,8}(\d{1,3})\s*점?.{0,20}?가격[^\d]{0,8}(\d{1,3})\s*점?")


def _amount_won(s: str) -> int | None:
    m = _AMOUNT_RE.search(s)
    if not m:
        return None
    return int(Decimal(m.group(1).replace(",", "")) * _UNIT[m.group(2)])


# ── 대조기 ───────────────────────────────────────────────────

def _sop5(kind: str, extracts: list[Extract]) -> str:
    docs = " · ".join(dict.fromkeys(e.doc for e in extracts))
    listing = " / ".join(f"{e.doc} {e.line_no}행 「{e.raw}」" for e in extracts)
    return (f"{docs}에 기재된 {kind}이(가) 아래와 같이 상이함 — {listing} — "
            f"일치시켜 정정하시기 바람")


def _compare_names(parts: list[DocPart]) -> list[CrossFlag]:
    ex: list[Extract] = []
    for p in parts:
        for line_no, raw, val in _labeled_value(p, _NAME_LABEL):
            ex.append(Extract(p.label, line_no, raw, val))
    if len({e.doc for e in ex}) < 2:
        return []
    # 포함 관계면 동일 취급("의성군 생성형AI플랫폼구축" ⊂ "의성군 생성형AI플랫폼구축 사업")
    base = min((e.value for e in ex), key=len)
    diff = [e for e in ex if base not in e.value and e.value not in base]
    if not diff:
        return []
    shown = [next(e for e in ex if base in e.value or e.value in base)] + diff
    return [CrossFlag("사업명", _sop5("사업명", shown), shown)]


def _compare_amounts(parts: list[DocPart]) -> list[CrossFlag]:
    ex: list[Extract] = []
    for p in parts:
        for line_no, raw, val in _labeled_value(p, _AMOUNT_LABEL):
            # 라벨 구분: 추정가격(부가세 제외)은 총액 계열과 다른 값이 정상이므로 제외
            if raw.replace(" ", "").find("추정가격") != -1:
                continue
            won = _amount_won(val)
            if won:
                ex.append(Extract(p.label, line_no, raw, f"{won:,}원"))
    amounts = {e.value for e in ex}
    if len({e.doc for e in ex}) < 2 or len(amounts) < 2:
        return []
    return [CrossFlag("총액", _sop5("사업비 총액", ex), ex)]


def _compare_scores(parts: list[DocPart]) -> list[CrossFlag]:
    flags: list[CrossFlag] = []
    ex: list[Extract] = []
    for p in parts:
        for i, ln in enumerate(p.text.splitlines()):
            m = _SCORE_RE.search(ln.replace(" ", ""))
            if not m:
                continue
            t, g = int(m.group(1)), int(m.group(2))
            e = Extract(p.label, i + 1, ln.strip(), f"기술 {t}:가격 {g}")
            ex.append(e)
            if t + g != 100:
                flags.append(CrossFlag(
                    "배점", f"{p.label} {i + 1}행의 평가 배점 합계가 100이 아님"
                            f"(기술 {t} + 가격 {g} = {t + g}) — 확인하시기 바람", [e]))
            break  # 문서당 첫 배점 표기만
    if len({e.doc for e in ex}) >= 2 and len({e.value for e in ex}) >= 2:
        flags.append(CrossFlag("배점", _sop5("평가 배점(기술:가격)", ex), ex))
    return flags


def _compare_periods(parts: list[DocPart]) -> list[CrossFlag]:
    ex: list[Extract] = []
    for p in parts:
        for line_no, raw, val in _labeled_value(p, _PERIOD_LABEL):
            m = _MONTHS_RE.search(val) or _MONTHS_RE.search(raw.replace(" ", ""))
            if m:
                ex.append(Extract(p.label, line_no, raw, f"{m.group(1)}개월"))
    if len({e.doc for e in ex}) < 2 or len({e.value for e in ex}) < 2:
        return []
    return [CrossFlag("사업기간", _sop5("사업기간", ex), ex)]


def cross_check(parts: list[DocPart]) -> list[CrossFlag]:
    """문서 2건 이상일 때만 의미. 명확한 모순만 반환(보수 원칙)."""
    if len(parts) < 2:
        return []
    flags: list[CrossFlag] = []
    flags += _compare_names(parts)
    flags += _compare_amounts(parts)
    flags += _compare_scores(parts)
    flags += _compare_periods(parts)
    return flags
