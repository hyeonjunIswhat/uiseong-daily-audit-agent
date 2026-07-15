"""서류 완결성 검증 — A2 (설계서 §2·§6, REBUILD 회차 1). LLM 미관여, 결정론.

drop-anything 접수 원칙: 이용자가 뭘 올려야 하는지 몰라도 된다. 받은 내용에서
문서유형을 인식하고(내용 기반 — 파일명은 힌트), 사건유형(계약방법·사업유형)별
필수서류와 대조해 3분류 리포트를 반환한다:
  인식됨   — 내용·파일명에서 유형 표지를 확인
  누락     — 필수인데 미확인 → 보완요청 문안 생성(원가심사 규칙 §6③의 자동화)
  확인필요 — 파일명으로만 인식(내용 미확인) 등 신뢰도 낮은 인식

보수 원칙: 완결성 '통과'가 검토 착수의 전제조건은 아니다 — 누락이 있어도 검토는
진행하되 리포트를 함께 낸다(차단하지 말고 안내, 설계서 §6 '불명' 버킷 사상).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from audit_core.config import get_settings


@dataclass(frozen=True)
class DocHit:
    key: str
    label: str
    source: str        # 인식 근거: "본문 키워드 '…'" | "파일명 '…'"
    weak: bool = False  # 파일명 단독 인식 = 확인필요


@dataclass
class CompletenessReport:
    method: str | None
    biz_type: str | None
    recognized: list[DocHit] = field(default_factory=list)
    uncertain: list[DocHit] = field(default_factory=list)          # weak 인식
    missing: list[tuple[str, str, str]] = field(default_factory=list)  # (key, label, 서식 안내)
    applied_groups: list[str] = field(default_factory=list)
    provisional: bool = True

    @property
    def complete(self) -> bool:
        return not self.missing


class RequiredDocs:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().REQUIRED_DOCS_PATH)
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.provisional: bool = bool(raw.get("provisional", True))
        self.source: str = raw.get("source", "")
        self.doc_types: dict[str, dict] = raw.get("doc_types") or {}
        self.requirements: list[dict] = raw.get("requirements") or []
        if not self.doc_types or not self.requirements:
            raise ValueError("required_docs.yaml에 doc_types/requirements가 없음")

    # 언급≠실물 구분(실물 검증 2026-07-15): 요청서의 '첨부서류' 목록에 적힌
    # 서류명이 '제출된 것'으로 강인식되어 누락 검출이 무력화되던 결함.
    #   강인식 = 표제로 등장(문서 최선두 또는 줄 시작, 첨부 문맥 밖)
    #   약인식(확인필요) = 첨부·붙임 마커 뒤 MENTION_WINDOW 이내이거나 줄 중간 언급
    _ATTACH_RE = re.compile(r"첨부|붙임|제출서류")
    MENTION_WINDOW = 150   # compact 문자 기준
    HEAD_CHARS = 60        # 문서 최선두 = 무조건 표제

    def _keyword_strength(self, lines_compact: list[str], kw: str) -> str | None:
        """키워드 등장 강도: 'strong'(표제) | 'mention'(언급) | None(없음).

        줄 단위로 본다 — 표제는 줄 시작에 오고, 첨부 목록 항목도 줄 시작이지만
        직전 MENTION_WINDOW 안에 첨부 마커가 있으므로 언급으로 강등된다.
        """
        found = None
        offset = 0
        attach_positions: list[int] = []
        for line in lines_compact:
            for m in self._ATTACH_RE.finditer(line):
                attach_positions.append(offset + m.start())
            pos = line.find(kw)
            if pos != -1:
                abs_pos = offset + pos
                in_attach = any(0 <= abs_pos - a <= self.MENTION_WINDOW for a in attach_positions)
                # 표제 = 최선두 또는 줄 시작, 단 첨부 문맥 안이면 항상 언급
                is_title = (abs_pos < self.HEAD_CHARS or pos == 0) and not in_attach
                if is_title:
                    return "strong"
                found = "mention"
            offset += len(line)
        return found

    # 표제 줄 판정(공용) — cross_check의 번들 분리기도 사용
    TITLE_MAX_LEN = 40

    def title_line_key(self, line: str) -> tuple[str, str] | None:
        """이 줄이 어떤 문서유형의 '표제 줄'이면 (key, label)을, 아니면 None.

        표제 = 문서명 명사로 끝나는 짧은 줄(doc_type._title_match과 동일 규칙).
        '[별지 제1호서식]' 같은 접두 줄은 표제가 아니므로 걸리지 않는다.
        """
        ln = line.replace(" ", "").rstrip(").]」』>-·.…:;○o•")
        ln = re.sub(r"\([^()]*$|\([^()]*\)$", "", ln)  # 꼬리 조문 괄호: "요청서(제6조제1항관련)"
        if not ln or len(ln) > self.TITLE_MAX_LEN:
            return None
        for key, spec in self.doc_types.items():
            for kw in spec.get("keywords", []):
                if ln.endswith(kw.replace(" ", "")):
                    return key, spec.get("label", key)
        return None

    # ── 유형 인식 (신호: 표제 > 본문 언급 > 파일명) ─────────────
    def detect(self, doc_text: str, filenames: list[str] | None = None) -> list[DocHit]:
        lines_compact = [ln.replace(" ", "") for ln in doc_text.splitlines()]
        hits: dict[str, DocHit] = {}
        for key, spec in self.doc_types.items():
            for kw in spec.get("keywords", []):
                strength = self._keyword_strength(lines_compact, kw.replace(" ", ""))
                if strength == "strong":
                    hits[key] = DocHit(key, spec.get("label", key), f"본문 표제 '{kw}'")
                    break
                if strength == "mention" and key not in hits:
                    hits[key] = DocHit(
                        key, spec.get("label", key),
                        f"본문에서 '{kw}' 문구는 확인했지만 실제 파일은 첨부되지 않았습니다", weak=True,
                    )
                    # 다른 키워드가 표제로 나올 수 있으므로 break 없이 계속 탐색
        for name in filenames or []:
            base = re.sub(r"\.[A-Za-z0-9]+$", "", name).replace(" ", "")
            for key, spec in self.doc_types.items():
                for kw in spec.get("keywords", []):
                    if kw.replace(" ", "") not in base:
                        continue
                    prev = hits.get(key)
                    if prev is None:
                        hits[key] = DocHit(
                            key, spec.get("label", key), f"파일명 '{name}'", weak=True
                        )
                    elif prev.weak and "첨부되지 않았습니다" in prev.source:
                        # 본문 언급으로 '미첨부' 판정됐지만 실제로 그 파일이 첨부됨
                        # (2026-07-15 실장애: 첨부한 제안요청서를 미첨부로 안내)
                        hits[key] = DocHit(
                            key, prev.label,
                            f"파일명 '{name}' 첨부 확인(본문 표제는 미확인)", weak=True,
                        )
                    break
        return list(hits.values())

    # ── 필수서류 산정 ──────────────────────────────────────
    def required_for(self, method: str | None, biz_type: str | None) -> tuple[list[str], list[str]]:
        """(필수 doc_type 키 목록, 적용된 그룹 id 목록)."""
        m = (method or "").replace(" ", "")
        b = (biz_type or "").replace(" ", "")
        keys: list[str] = []
        groups: list[str] = []
        for g in self.requirements:
            m_cond = g.get("when_method_contains")
            b_cond = g.get("when_biz_contains")
            if m_cond and not any(c.replace(" ", "") in m for c in m_cond):
                continue
            if b_cond and not any(c.replace(" ", "") in b for c in b_cond):
                continue
            groups.append(g["id"])
            keys += [k for k in g.get("docs", []) if k not in keys]
        return keys, groups

    def check(
        self,
        doc_text: str,
        method: str | None = None,
        biz_type: str | None = None,
        filenames: list[str] | None = None,
    ) -> CompletenessReport:
        hits = self.detect(doc_text, filenames)
        by_key = {h.key: h for h in hits}
        required, groups = self.required_for(method, biz_type)

        report = CompletenessReport(
            method=method, biz_type=biz_type, applied_groups=groups,
            provisional=self.provisional,
        )
        report.recognized = [h for h in hits if not h.weak]
        report.uncertain = [h for h in hits if h.weak]
        for key in required:
            if key not in by_key:
                spec = self.doc_types.get(key, {})
                form = spec.get("form", "")
                hint = f"서식 보유: {form}" if form else ""
                report.missing.append((key, spec.get("label", key), hint))
        return report


def format_completeness(report: CompletenessReport) -> str:
    """3분류 리포트 + 보완요청 문안. 첫 번째 산출물(설계서 §6)."""
    lines = ["## 서류 완결성 확인", ""]
    if report.provisional:
        lines.append("> ⚠ 필수서류 기준은 실물 사례 기반 잠정값입니다(감사팀 협의 대상).\n")
    scope = " · ".join(x for x in [report.method, report.biz_type] if x) or "기본(공통 서류만)"
    lines.append(f"적용 기준: {scope} (그룹: {', '.join(report.applied_groups)})\n")

    if report.recognized:
        lines.append(f"### ✅ 인식됨 {len(report.recognized)}건")
        for h in report.recognized:
            lines.append(f"- **{h.label}** — {h.source}")
        lines.append("")
    if report.uncertain:
        lines.append(f"### ❔ 확인 필요 {len(report.uncertain)}건 (표제 미확인 — 언급·파일명 인식)")
        for h in report.uncertain:
            lines.append(f"- {h.label} — {h.source}")
        lines.append("")
    if report.missing:
        lines.append(f"### ⛔ 누락 {len(report.missing)}건")
        for _key, label, hint in report.missing:
            lines.append(f"- **{label}**" + (f" — {hint}" if hint else ""))
        lines.append("")
        lines.append("**보완요청 문안(초안)**")
        docs = ", ".join(label for _k, label, _h in report.missing)
        lines.append(
            f"> 일상감사 처리를 위하여 다음 서류의 보완을 요청드립니다: {docs}. "
            f"일상감사에 필요한 서류를 제출받은 날부터 처리기한(7일)이 기산될 수 "
            f"있습니다(의성군 일상감사 규정 §8① 참조)."
        )
    else:
        lines.append("### 누락 서류 없음 — 필수서류가 모두 확인되었습니다.")
    return "\n".join(lines)
