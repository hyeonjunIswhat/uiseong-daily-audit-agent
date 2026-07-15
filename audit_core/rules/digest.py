"""검토 입력 다이제스트(2026-07-15 성능 규율) — LLM 미관여, 결정론.

reviewer 호출에 전체 첨부 원문을 반복해 넣지 않기 위한 발췌기. 문서가
상한(AUDIT_DIGEST_CAP)보다 짧으면 원문 그대로 반환하고, 길면 검토에
필요한 줄만 남긴다:

  - [문서: 이름] 경계 마커(출처 추적 — 발췌 뒤에도 어느 파일의 문장인지 유지)
  - 표제성 짧은 줄(문서 구조·항목 제목)
  - 금액·산식 줄(원, 소계·합계·부가가치세, =, ×)
  - 활성 축 신호 키워드가 있는 줄(axis_signals.yaml과 동일 사전)
  - 각 문서의 서두 줄(사업명·개요가 몰리는 구간)

생략이 발생하면 말미에 원문 대비 발췌 크기를 명시한다(침묵 금지).
"""

import re
from pathlib import Path

import yaml

from audit_core.config import get_settings

_DOC_MARKER = re.compile(r"^\[문서: .+\]$")
_MONEY = re.compile(r"\d[\d,]{2,}\s*원|원정")
_FORMULA = re.compile(r"[=×x]\s*\d|소\s*계|합\s*계|부가가치세|단가|금액")
_TITLE_MAX = 40
_HEAD_LINES = 25   # 문서별 서두 보존 줄 수

_SIGNALS: list[str] | None = None


def _signals() -> list[str]:
    global _SIGNALS
    if _SIGNALS is None:
        path = Path(__file__).parent / "axis_signals.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _SIGNALS = sorted({kw for kws in (data.get("axes") or {}).values() for kw in kws})
    return _SIGNALS


def build_review_digest(doc_text: str, cap: int | None = None) -> str:
    """검토용 발췌 — cap 이하면 원문 그대로(정확도 우선), 초과 시 결정론 발췌."""
    cap = cap or get_settings().AUDIT_DIGEST_CAP
    if len(doc_text) <= cap:
        return doc_text

    signals = _signals()
    lines = doc_text.splitlines()
    kept: list[str] = []
    kept_len = 0
    since_marker = 0
    dropped = 0
    for ln in lines:
        stripped = ln.strip()
        if _DOC_MARKER.match(stripped):
            since_marker = 0
            keep = True
        else:
            since_marker += 1
            compact = stripped.replace(" ", "")
            keep = bool(stripped) and (
                since_marker <= _HEAD_LINES
                or (len(compact) <= _TITLE_MAX and compact)      # 표제성 짧은 줄
                or _MONEY.search(stripped)
                or _FORMULA.search(stripped)
                or any(kw in compact for kw in signals)
            )
        if not keep:
            dropped += 1
            continue
        if kept_len + len(ln) > cap:
            dropped += 1
            continue
        kept.append(ln)
        kept_len += len(ln) + 1
    if dropped:
        kept.append(f"…(발췌 검토 — 원문 {len(doc_text):,}자 중 {kept_len:,}자만 검토, "
                    f"금액·산식·표제·신호 줄 우선 보존)")
    return "\n".join(kept)
