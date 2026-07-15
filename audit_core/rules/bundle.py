"""파일별 사업 프로파일링 + 사업 묶음 분리(2026-07-15). LLM 미관여, 결정론.

실장애: 스카이디펜스런 제안요청서(용역·협상) + GPU 서버 구매(물품) 4파일을
한 번에 첨부하자 첫 문서(전체의 79%)가 병합 분류를 지배해 전부 '용역·협상'
으로 검토됨. 서로 다른 사업의 문서를 한 검토로 섞으면 축 판정·완결성·대조가
전부 무의미해지므로, **다른 사업이 감지되면 검토를 시작하지 않는다**.

파일마다 독립 추출: 사업명(라벨 > 파일명 괄호 > 표제 줄), 문서유형, 사업유형
후보, 금액, 계약방법 — 각각 근거 위치(줄번호·원문) 보존.
묶음 판정: 사업명 후보 간 포함 관계(공백 제거, 6자 이상)면 같은 사업.
"""

import re
from dataclasses import dataclass, field

from audit_core.rules.biz_classifier import BusinessClassifier
from audit_core.rules.cross_check import _AMOUNT_LABEL, _NAME_LABEL, DocPart, _amount_won, _labeled_value
from audit_core.rules.doc_type import detect_doc_type

# 파일명·표제에서 사업명이 아닌 문서유형 어휘(괄호 안이 유형명일 뿐인 경우 제외용)
_DOCTYPE_WORDS = re.compile(
    r"제안요청서|과업지시서|규격서|요청서|검토서|계획서|공고문|산출내역서|원가계산서|"
    r"내역서|일상감사|검토결과|조치결과|납품조건|별지|서식")


def _norm(s: str) -> str:
    return re.sub(r"[\s「」『』\"'()\[\]·•ㆍ.\-]", "", s)


@dataclass
class FileProfile:
    name: str                       # 파일 표시명
    text: str = ""
    biz_name: str | None = None     # 대표 사업명(원문 표기)
    biz_name_src: str = ""          # 근거 위치: "7행 「사업명: …」" | "파일명 괄호"
    candidates: list[str] = field(default_factory=list)  # 정규화 후보(묶음 판정용)
    doc_type: str = ""              # 문서유형 라벨
    biz_type: str | None = None     # 사업유형 후보(물품·용역·공사…)
    biz_confidence: str = ""
    contract_method: str | None = None
    amount: int | None = None
    amount_src: str = ""


def profile_file(name: str, text: str, classifier: BusinessClassifier | None = None) -> FileProfile:
    """파일 1건의 독립 프로파일 — 사업명·유형·금액·계약방법을 근거 위치와 함께."""
    p = FileProfile(name=name, text=text)
    part = DocPart(name, text)

    # ① 사업명: 라벨 값(사업명·업무(사업)명·건명) 우선
    for line_no, raw, val in _labeled_value(part, _NAME_LABEL):
        val = val.lstrip("·•ㆍ-:：")
        if len(_norm(val)) >= 6 and not _DOCTYPE_WORDS.fullmatch(_norm(val)):
            if p.biz_name is None:
                p.biz_name = val
                p.biz_name_src = f"{line_no}행 「{raw[:50]}」"
            p.candidates.append(_norm(val))
    # ② 파일명 괄호(문서유형 어휘 단독이면 제외 — "(제안요청서)" 등)
    for paren in re.findall(r"\(([^()]{4,40})\)", name):
        n = _norm(paren)
        if len(n) >= 6 and not _DOCTYPE_WORDS.search(paren.replace(" ", "")[:6]) \
                and not _DOCTYPE_WORDS.fullmatch(n):
            if p.biz_name is None:
                p.biz_name, p.biz_name_src = paren, "파일명 괄호"
            p.candidates.append(n)
    # ③ 표제 줄 폴백 — 사업명 신호 어휘가 있을 때만(공문 표어·구호 오인 방지:
    #    "다시 도약하는 대한민국" 같은 머리글이 사업명이 되면 별개 묶음으로 오분리)
    _BIZ_HINT = re.compile(r"사업|용역|구매|구축|공사|설치|대행|개발|행사|임차|도입")
    if not p.candidates:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        core = _DOCTYPE_WORDS.sub("", _norm(first))
        if 6 <= len(core) <= 40 and _BIZ_HINT.search(core):
            p.biz_name, p.biz_name_src = first[:50], "표제 줄(1행)"
            p.candidates.append(core)

    p.doc_type = detect_doc_type(text).label if text else ""
    if text:
        bp = (classifier or BusinessClassifier()).classify(text)
        p.biz_type, p.biz_confidence = bp.primary_type, bp.confidence
        p.contract_method = bp.contract_method if bp.contract_method != "미상" else None
    for line_no, raw, val in _labeled_value(part, _AMOUNT_LABEL):
        won = _amount_won(val)
        if won:
            p.amount, p.amount_src = won, f"{line_no}행 「{raw[:50]}」"
            break
    return p


def group_projects(profiles: list[FileProfile]) -> list[list[FileProfile]]:
    """사업명 후보 포함 관계로 파일을 사업 단위 묶음으로 분할.

    후보가 하나도 없는 파일(공문 표지 등)은 어느 사업인지 단정하지 않고
    별도 '미상' 묶음으로 남긴다 — 단, 전체가 한 묶음이면 거기에 흡수.
    """
    idx = list(range(len(profiles)))
    parent = idx[:]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    for i in range(len(profiles)):
        for j in range(i + 1, len(profiles)):
            ci, cj = profiles[i].candidates, profiles[j].candidates
            if any(a in b or b in a for a in ci for b in cj):
                union(i, j)
    groups: dict[int, list[FileProfile]] = {}
    unknown: list[FileProfile] = []
    for i, p in enumerate(profiles):
        if not p.candidates:
            unknown.append(p)
        else:
            groups.setdefault(find(i), []).append(p)
    out = list(groups.values())
    if unknown:
        if len(out) == 1:       # 사업이 하나뿐이면 미상 파일은 그 사업으로 흡수
            out[0].extend(unknown)
        else:
            out.append(unknown)  # 여러 사업이면 미상은 별도 표시(단정 금지)
    return out or [unknown]


def format_split_report(groups: list[list[FileProfile]]) -> str:
    """다른 사업 감지 시 사용자 안내 — 근거 위치와 함께, 검토는 시작하지 않음."""
    lines = ["## ⛔ 서로 다른 사업의 문서가 섞여 있어 검토를 시작하지 않았습니다", "",
             "한 번의 검토는 **한 사업**의 서류 묶음이어야 정확합니다. "
             "확인된 묶음은 다음과 같습니다.", ""]
    for gi, g in enumerate(groups, 1):
        named = next((p for p in g if p.biz_name), None)
        title = named.biz_name if named else "사업명 미상(문서에서 확인 못 함)"
        lines.append(f"### 묶음 {gi} — {title}")
        for p in g:
            bits = [f"유형: {p.biz_type or '미상'}"]
            if p.contract_method:
                bits.append(f"계약방법: {p.contract_method}")
            if p.amount:
                bits.append(f"금액: {p.amount:,}원 ({p.amount_src})")
            src = f" — 사업명 근거: {p.biz_name_src}" if p.biz_name else ""
            lines.append(f"- `{p.name}` · {' · '.join(bits)}{src}")
        lines.append("")
    lines.append("**다음 조치**: 한 사업의 파일만 남기고 다시 첨부해 주세요. "
                 "묶음별로 따로 올리면 각각 정상 검토합니다.")
    return "\n".join(lines)
