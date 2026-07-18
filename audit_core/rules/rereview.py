"""재심사 모드 — 변경점만 검토 (마스터 SOP ②, 설계서 §6, REBUILD 회차 2).

"재심사 건은 직전 버전과 변경점만 추출하여 그 부분만 검토한다. 전체 재검토 금지."
(규정 §9 재검토 요청·조치결과 반영 흐름과 동일 메커니즘)

두 경로:
  ① 마커 감지 — 본문·파일명의 '재공고/재심사/재검토/조치결과 반영' 표지
  ② 버전 쌍 감지 — 번들 분리 결과에 같은 유형 문서가 2벌(X, X#2)이면 diff

결정론(difflib). 변경 조각 = 변경 줄 ± CONTEXT 줄. LLM 축별 검토는 변경 조각만
받고, 산식 검산·완결성 등 결정론 단계는 전체 문서로 수행한다(놓침 방지).
"""

import difflib
import re
from dataclasses import dataclass, field

from audit_core.rules.cross_check import DocPart

RE_MARKER = re.compile(r"재\s*공\s*고|재\s*심\s*사|재검토\s*요청|조치결과\s*(?:반영|통보)")

CONTEXT = 1          # 변경 줄 앞뒤로 함께 보여줄 줄 수
MIN_LINE_CHARS = 2   # 공백·빈 줄 노이즈 제외


def has_marker(text: str, filenames: list[str] | None = None) -> bool:
    if RE_MARKER.search(text):
        return True
    return any(RE_MARKER.search(n) for n in (filenames or []))


@dataclass
class ReReview:
    base_label: str                 # 직전 버전 문서 라벨
    new_label: str                  # 개정 버전 문서 라벨
    similarity: float = 0.0         # 직전본과의 유사도(0~1) — 낮으면 버전 쌍이 아님
    added: list[tuple[int, str]] = field(default_factory=list)    # 개정본 (줄번호, 내용)
    removed: list[tuple[int, str]] = field(default_factory=list)  # 직전본 (줄번호, 내용)
    changed_text: str = ""          # 변경 조각(±CONTEXT) — LLM 검토 대상

    @property
    def n_changes(self) -> int:
        return len(self.added) + len(self.removed)


def _doc_type_key(p: DocPart, catalog) -> str:
    """버전 쌍 그룹 키 — 문서유형(표제 기반 카탈로그 키) 우선, 없으면 라벨.

    실장애(2026-07-18, 테스트 B): 파일 첨부는 DocPart 라벨이 파일명이라
    "4. 제안요청서.hwpx"와 "제안요청서 260323.hwpx"가 같은 유형으로 안 묶여
    재심사 diff가 발동하지 않았다 — 표제 줄로 유형을 읽어 묶는다.
    """
    for ln in p.text.splitlines()[:40]:
        hit = catalog.title_line_key(ln.strip()) if ln.strip() else None
        if hit:
            return hit[0]
    return p.label.split("#")[0]


def _version_pair(parts: list[DocPart]) -> tuple[DocPart, DocPart] | None:
    """같은 유형 문서 2벌 — 앞선 것을 직전본, 뒤를 개정본으로 본다(첨부 순서)."""
    from audit_core.rules.completeness import RequiredDocs
    try:
        catalog = RequiredDocs()
    except Exception:
        catalog = None
    by_base: dict[str, list[DocPart]] = {}
    for p in parts:
        key = _doc_type_key(p, catalog) if catalog else p.label.split("#")[0]
        by_base.setdefault(key, []).append(p)
    for group in by_base.values():
        if len(group) >= 2:
            return group[0], group[-1]
    return None


def diff_docs(old: DocPart, new: DocPart) -> ReReview:
    """직전본 대비 개정본의 변경 줄만 추출(공백 무시 비교)."""
    old_lines = old.text.splitlines()
    new_lines = new.text.splitlines()
    sm = difflib.SequenceMatcher(
        a=[l.replace(" ", "") for l in old_lines],
        b=[l.replace(" ", "") for l in new_lines],
    )
    rr = ReReview(base_label=old.label, new_label=new.label, similarity=sm.ratio())
    keep: set[int] = set()  # 개정본에서 변경 조각으로 보낼 줄 인덱스
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for i in range(i1, i2):
            if len(old_lines[i].replace(" ", "")) >= MIN_LINE_CHARS:
                rr.removed.append((i + 1, old_lines[i].strip()))
        for j in range(j1, j2):
            if len(new_lines[j].replace(" ", "")) >= MIN_LINE_CHARS:
                rr.added.append((j + 1, new_lines[j].strip()))
            for k in range(max(0, j - CONTEXT), min(len(new_lines), j + CONTEXT + 1)):
                keep.add(k)
    rr.changed_text = "\n".join(new_lines[k] for k in sorted(keep))
    return rr


def detect_rereview(parts: list[DocPart]) -> ReReview | None:
    """번들에서 버전 쌍을 찾아 diff. 없으면 None(재심사 아님 또는 직전본 미제출)."""
    pair = _version_pair(parts)
    if not pair:
        return None
    rr = diff_docs(*pair)
    # 유사도 가드(실장애 2026-07-15): 제안요청서와 과업지시서가 같은 카탈로그
    # 유형이라 버전 쌍으로 오인 → 절반 이상 동일하지 않으면 서로 다른 문서로 본다
    if rr.similarity < 0.5:
        return None
    return rr if rr.n_changes else None


def format_rereview(rr: ReReview) -> str:
    """재심사 안내 + 변경점 목록(발췌 앵커 포함)."""
    lines = [
        "## 🔁 재심사 모드 — 변경점만 검토 (SOP: 전체 재검토 금지)",
        f"직전본 「{rr.base_label}」 ↔ 개정본 「{rr.new_label}」 — 변경 {rr.n_changes}줄",
        "",
    ]
    if rr.added:
        lines.append(f"**개정본에 추가·수정된 {len(rr.added)}줄**")
        for no, txt in rr.added[:20]:
            lines.append(f"- {no}행: {txt[:80]}")
        if len(rr.added) > 20:
            lines.append(f"- …외 {len(rr.added) - 20}줄")
        lines.append("")
    if rr.removed:
        lines.append(f"**직전본에서 삭제된 {len(rr.removed)}줄**")
        for no, txt in rr.removed[:10]:
            lines.append(f"- {no}행: {txt[:80]}")
        if len(rr.removed) > 10:
            lines.append(f"- …외 {len(rr.removed) - 10}줄")
        lines.append("")
    lines.append("_AI 축별 검토는 위 변경 조각만 대상으로 합니다. 산식 검산·완결성 확인은 전체 문서로 수행됩니다._")
    return "\n".join(lines)
