"""문서유형(doc_type) 감지 + 검토 프로파일 (LLM 미관여, 결정론).

실무 입력 3종(공고문·계산서·추진계획서)을 키워드로 감지해 판단 가능한 루브릭
축으로 좁힌다. 규칙은 doc_profiles.yaml로 외부화 — 감사팀 협의 시 값 교체만.

보수 원칙(제약 A2): 유형이 단일·명확할 때만 좁힌다.
  - 2개 유형 이상 매칭(복합 문서)·무매칭 → 기본 프로파일(전축)
  - 스킵 축은 오케스트레이터가 사유와 함께 리포트에 표시(침묵 금지)
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from audit_core.config import get_settings

# "의성군 공고 제2026-13호" 류 머리글 — 공고문 보강 패턴
_GONGGO_RE = re.compile(r"공고\s*제?\s*\d{4}\s*-")


@dataclass(frozen=True)
class DocTypeResult:
    doc_type: str            # 프로파일 키(의뢰서/공고문/계산서/추진계획서)
    label: str
    axes: tuple[str, ...]    # 이 문서로 판단 가능한 루브릭 축
    skip_note: str           # 스킵 축 사유(전축이면 빈 문자열)
    reason: str              # 감지 근거(매칭 키워드 등)
    provisional: bool = True

    @property
    def narrows(self) -> bool:
        """전축이 아닌, 좁혀진 프로파일인가."""
        return bool(self.skip_note)


class DocProfiles:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().DOC_PROFILES_PATH)
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.provisional: bool = bool(raw.get("provisional", True))
        self.default_profile: str = raw.get("default_profile", "의뢰서")
        self.profiles: dict[str, dict] = raw.get("profiles", {}) or {}
        if self.default_profile not in self.profiles:
            raise ValueError(f"default_profile '{self.default_profile}'이 profiles에 없음")

    def _result(self, name: str, reason: str) -> DocTypeResult:
        p = self.profiles[name]
        return DocTypeResult(
            doc_type=name,
            label=p.get("label", name),
            axes=tuple(p.get("axes", [])),
            skip_note=p.get("skip_note", ""),
            reason=reason,
            provisional=self.provisional,
        )

    # 좁히기 프로파일의 표제 탐색 범위. 표제는 문서 서두의 '짧은 줄'에 오므로
    # 본문 문장 속 '산출내역서' 같은 언급이 계산서 프로파일로 오인(축 오축소)되는
    # 것을 막는다 — 실물 검증(2026-07-15): 협상 대상사업 검토서(말미 첨부 언급)·
    # 조치결과 통보서(감사의견 서술)가 계산서(B·C축)로 좁혀지던 결함.
    HEAD_LINES = 12          # 표제 탐색: 서두 N줄
    TITLE_MAX_LEN = 40       # 표제 줄 길이 상한(compact) — 서술 문장 배제

    def _title_match(self, head_lines: list[str], kw: str) -> bool:
        """키워드가 서두의 '표제 줄'에 있는가 (compact 줄 기준).

        표제는 문서명 명사로 끝난다("…모집 공고문", "…대상사업 검토서",
        "산출내역서"). 서술 문장 속 언급("산출내역서 내 타 사업 명칭 잔존…")은
        키워드가 줄 끝에 오지 않으므로 배제된다.
        """
        for ln in head_lines:
            ln = ln.rstrip(").]」』>-·.…:;○o•")
            if ln.endswith(kw) and len(ln) <= self.TITLE_MAX_LEN:
                return True
        return False

    def detect(self, doc_text: str) -> DocTypeResult:
        """문서 텍스트 → 프로파일. 공문 서식은 '일 상 감 사 요 청 서'처럼 자간
        공백이 흔하므로 공백 무시 매칭. 매칭 근거를 기록한다.

        보수 원칙: 축을 '좁히는' 프로파일(공고문·계산서·추진계획서)은 문서 서두의
        짧은 표제 줄만 신뢰한다. 전축으로 '넓히는' 기본 프로파일(의뢰서)은 번들
        중간에 표지가 올 수 있으므로 전문을 탐색한다.
        """
        compact = doc_text.replace(" ", "")
        head_lines = [ln.replace(" ", "") for ln in doc_text.splitlines()[: self.HEAD_LINES]]
        matched: dict[str, str] = {}  # 유형 → 근거
        for name, p in self.profiles.items():
            for kw in p.get("keywords", []) or []:
                kw_c = kw.replace(" ", "")
                if name == self.default_profile:
                    ok = kw_c in compact
                else:
                    ok = self._title_match(head_lines, kw_c)
                if ok:
                    matched[name] = f"키워드 '{kw}'"
                    break
        if ("공고문" in self.profiles and "공고문" not in matched
                and _GONGGO_RE.search("\n".join(doc_text.splitlines()[: self.HEAD_LINES]))):
            matched["공고문"] = "머리글 '공고 제NNNN-' 패턴"

        default = self.default_profile
        # 의뢰서(전체 세트) 표지가 있으면 다른 문서가 첨부로 섞여 있어도 전축
        if default in matched:
            return self._result(default, matched[default])

        narrowed = [n for n in matched if n != default]
        if len(narrowed) == 1:
            return self._result(narrowed[0], matched[narrowed[0]])
        if len(narrowed) >= 2:
            kinds = "·".join(sorted(narrowed))
            return self._result(default, f"복합 문서({kinds} 표지 동시 감지) — 전축 검토")
        return self._result(default, "유형 표지 미감지 — 기본(전축) 검토")


def detect_doc_type(doc_text: str, profiles: DocProfiles | None = None) -> DocTypeResult:
    return (profiles or DocProfiles()).detect(doc_text)
