"""민감도 등급·마스킹 게이트 골격 — 설계서 §3.1 (REBUILD 회차 3 선행 골격).

등급(문서 단위, 보수 취급 — Red 경계 확정(미결 #9) 전):
  RED    유출 시 법적 문제(입찰 전 가격정보·개인정보) — 외부 반출 절대 금지
  YELLOW 일반 사건 문서 — 외부 상급 모델 허용(기관 승인 후)
  GREEN  공개 정보(법령·공개 사례)

현재 상태: 외부 LLM 미승인·미연동(EXTERNAL_LLM_ENABLED=False 고정 기본값).
이 모듈은 게이트 골격이며, 외부 연동(회차 5) 전에는 차단 로직만 존재한다 —
어떤 코드 경로도 외부로 페이로드를 보내지 않는다.

마스킹 게이트: 외부행 페이로드 전수 검사(F5). 위반 탐지 시 차단이 기본이며
'마스킹 후 통과'는 사람 검토 절차가 생기기 전까지 구현하지 않는다(보수 원칙).
"""

import re
from dataclasses import dataclass

RED = "RED"
YELLOW = "YELLOW"
GREEN = "GREEN"

# 문서유형(required_docs.yaml doc_types 키) → 등급. 미등재 유형은 YELLOW 보수 취급,
# 산출내역·원가 계열은 입찰 전 가격정보라 RED 고정(설계서 §3.1 표).
RED_DOC_TYPES = {"산출내역서"}  # 원가계산서·예정가격조서는 카탈로그상 산출내역서 키에 포함
GREEN_DOC_TYPES: set[str] = set()  # 사건 문서 중 공개 등급은 없음 — 법령·카드는 지식 스토어 소관


def classify_doc(doc_type_key: str | None) -> str:
    if doc_type_key in RED_DOC_TYPES:
        return RED
    if doc_type_key in GREEN_DOC_TYPES:
        return GREEN
    return YELLOW


@dataclass(frozen=True)
class MaskHit:
    rule: str      # 개인정보 | 가격정보
    excerpt: str   # 검출 문맥(마스킹된 발췌)


# 개인정보 식별자
_RRN_RE = re.compile(r"\d{6}\s*[-–]\s*[1-4]\d{6}")             # 주민등록번호
_FOREIGN_RE = re.compile(r"\d{6}\s*[-–]\s*[5-8]\d{6}")          # 외국인등록번호
# 입찰 전 가격정보 — 예정가격·기초금액 라벨 금액, 단가 열 나열(3회 이상)
_PRICE_LABEL_RE = re.compile(r"(?:예정\s*가격|기초\s*금액|예가)\s*[:：]?\s*[\d,]+\s*원")
_UNIT_PRICE_RE = re.compile(r"단가\s*[|:：]?\s*[\d,]{4,}\s*원?")


def mask_gate(text: str) -> list[MaskHit]:
    """외부행 페이로드 검사 — 검출 목록 반환(비어 있으면 통과 가능).

    호출 규약: 외부 전송 직전에 반드시 호출하고, 검출이 있으면 전송하지 않는다.
    (현재는 외부 경로 자체가 없으므로 이 함수는 테스트·회차 5 대비 골격이다.)
    """
    hits: list[MaskHit] = []
    for m in list(_RRN_RE.finditer(text)) + list(_FOREIGN_RE.finditer(text)):
        hits.append(MaskHit("개인정보", f"…{m.group()[:8]}******…"))
    for m in _PRICE_LABEL_RE.finditer(text):
        hits.append(MaskHit("가격정보", m.group()[:30]))
    unit_prices = _UNIT_PRICE_RE.findall(text)
    if len(unit_prices) >= 3:  # 단가 열 나열 = 단가표로 간주(문서 단위 Red 보수 취급)
        hits.append(MaskHit("가격정보", f"단가 표 패턴 {len(unit_prices)}건"))
    return hits


def egress_allowed(doc_type_key: str | None, text: str, external_enabled: bool) -> tuple[bool, str]:
    """외부 반출 허용 여부 — (허용, 사유). 순서: 스위치 → 등급 → 마스킹 게이트."""
    if not external_enabled:
        return False, "외부 LLM 미승인(EXTERNAL_LLM_ENABLED=False) — 전 등급 로컬 처리"
    level = classify_doc(doc_type_key)
    if level == RED:
        return False, f"RED 문서유형({doc_type_key}) — 외부 반출 절대 금지(F5)"
    hits = mask_gate(text)
    if hits:
        kinds = ", ".join(sorted({h.rule for h in hits}))
        return False, f"마스킹 게이트 검출({kinds} {len(hits)}건) — 차단"
    return True, f"{level} — 반출 허용"


# ── 화면 표시용 개인정보 마스킹(2026-07-15) — 진행 메시지·발췌·최종 화면에 적용 ──
_PII_RULES = [
    (re.compile(r"\d{6}\s*[-–]\s*[1-8]\d{6}"), "******-*******"),          # 주민·외국인
    (re.compile(r"01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}"), "01*-****-****"),  # 휴대전화
    (re.compile(r"(?<!\d)0\d{1,2}[-.)\s]\d{3,4}[-.\s]\d{4}(?!\d)"), "0**-****-****"),  # 유선
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***@***.***"),               # 이메일
    (re.compile(r"(계좌\s*(?:번호)?\s*[:：]?\s*)[\d-]{10,}"), r"\g<1>***-***-******"),  # 계좌
]


def mask_pii(text: str) -> str:
    """주민번호·전화·이메일·계좌 마스킹 — 검토에 필요한 금액·사업명은 건드리지 않는다."""
    for pat, rep in _PII_RULES:
        text = pat.sub(rep, text)
    return text
