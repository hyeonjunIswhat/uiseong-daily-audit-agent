"""IntentClassifier — 입력 의도 10종 분류 (결정론, LLM 미관여).

길이 기반 라우팅("짧으면 QA, 150자↑면 검토")의 오인을 계층으로 교체(2026-07-15):
  - '검토'로 시작해도 문서 본문 신호가 없으면 document_review로 보내지 않는다.
  - 150자 이상이어도 공문서 신호가 약하면 audit_question이다.
  - "서버 구매+설치는 물품이야 공사야?" 같은 유형 상담은 business_type_question.

의도: greeting | help | audit_question | business_type_question | document_review
      | target_check | deadline | ledger | law_lookup | out_of_scope

결정론인 이유: 라우팅은 재현·테스트 가능해야 하고(동일 입력=동일 경로),
장애 시에도 동작해야 한다. 의미 해석이 필요한 답변 생성은 각 모드가 담당한다.
"""

import re
from dataclasses import dataclass

INTENTS = ("greeting", "help", "audit_question", "business_type_question",
           "document_review", "target_check", "deadline", "ledger",
           "law_lookup", "out_of_scope")


@dataclass(frozen=True)
class Intent:
    intent: str
    reason: str            # 라우팅 근거(진행 서사·디버깅용)
    doc_score: int = 0     # 공문서 신호 개수(문서 경로 판단 근거)


_GREET_RE = re.compile(r"^(안녕|하이|헬로|반갑|수고|고마워|감사(합니다|해요)?|ㅎㅇ|ㅋㅋ+|좋은\s*(아침|하루))")
_HELP_RE = re.compile(r"도움말|사용법|어떻게\s*(써|쓰|사용)|뭘\s*할\s*수|기능이\s*뭐|help")
_OOS_RE = re.compile(r"날씨|점심|저녁|메뉴|맛집|주말|영화|드라마|주식|코인|로또|연애|게임|노래|뉴스\s*틀")
_QUESTION_RE = re.compile(r"\?|인가요|한가요|하나요|할까요|까요\b|어떻게|알려|뭐예요|무엇|왜|되나|맞나|가능한|주세요|보여|해줘|인지\b|이야\b|일까")
# 유형 상담: 유형 어휘 + 의문/선택형 ("물품이야 공사야", "용역으로 봐야 하나")
_BIZTYPE_Q_RE = re.compile(
    r"(용역|물품|공사|보조|위탁|유형)[은는이가]?\s*(이야|인가|일까|맞|인지|으로\s*봐야|에\s*해당|이에요|예요|인가요)"
    r"|(물품|용역|공사)\s*(이야|인가요?)?\s*(물품|용역|공사)\s*(이야|인가요?|일까요?)"
    r"|무슨\s*유형|어떤\s*유형|유형[이은]?\s*뭐|어디에\s*해당|(으로|로)\s*(봐야|보나요|분류)")
_LAW_RE = re.compile(r"법령|조문")
_ARTICLE_HINT_RE = re.compile(r"제\s*\d+\s*조")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CALC_LABEL_RE = re.compile(r"소\s*계|합\s*계|부가가치세|산\s*출\s*내\s*역")
_AMOUNT_LABEL_RE = re.compile(r"추정\s*금액|추정\s*가격|총?\s*사업비|계약금액|예산액")


def _doc_score(text: str) -> int:
    """공문서 신호 개수 — 표제 줄·라벨 금액·산식 라벨·서식 마커·사업성격 근거."""
    score = 0
    try:
        from audit_core.rules.completeness import RequiredDocs
        rd = RequiredDocs()
        score += any(rd.title_line_key(ln) for ln in text.splitlines()[:60])
    except Exception:
        pass
    score += bool(_AMOUNT_LABEL_RE.search(text))
    score += bool(_CALC_LABEL_RE.search(text))
    score += bool(re.search(r"\[별지|붙\s*임|과\s*업\s*지\s*시|제\s*안\s*요\s*청", text))
    try:
        from audit_core.rules.biz_classifier import BusinessClassifier
        bp = BusinessClassifier().classify(text)
        score += (bp.primary_type not in ("확인필요", "") and bp.confidence != "low")
    except Exception:
        pass
    return score


def classify_intent(msg: str, has_files: bool = False) -> Intent:
    m = (msg or "").strip()
    if has_files:
        return Intent("document_review", "파일 첨부 — 문서 검토", doc_score=9)
    if not m:
        return Intent("help", "빈 입력")

    # ── 명령 접두(명시 의도) ──
    if m.startswith("대장"):
        return Intent("ledger", "대장 명령")
    if m.startswith("기한") or ("기한" in m and _DATE_RE.search(m)):
        return Intent("deadline", "기한 명령")
    if m in ("도움말", "help", "사용법", "?") or _HELP_RE.search(m[:30]):
        return Intent("help", "사용법 문의")
    if m.startswith("법령") or (len(m) < 150 and (
            (_LAW_RE.search(m) and re.search(r"찾|검색|알려|보여|조회", m))
            or (_ARTICLE_HINT_RE.search(m) and re.search(r"법|조례|규칙|규정|예규|훈령", m)))):
        return Intent("law_lookup", "법령 조회·탐색")
    if m.startswith("대상") or "판별" in m:
        return Intent("target_check", "대상 판별 명령")

    # ── 문서 vs 질문 — 길이가 아니라 공문서 신호로 가른다 ──
    explicit_review = m.startswith("점검") or m.startswith("검토")
    body = m.split(None, 1)[1] if explicit_review and len(m.split(None, 1)) > 1 else m
    score = _doc_score(body) if (explicit_review or len(m) >= 150) else 0
    if explicit_review:
        if len(body) >= 100 and score >= 1:
            return Intent("document_review", f"검토 명령 + 문서 신호 {score}개", doc_score=score)
        if _QUESTION_RE.search(m):  # "검토 절차가 어떻게 되나요?" — 메타 질문
            return Intent("audit_question", "검토 관련 질문(문서 본문 없음)")
        # "검토"만 입력 — 명령 시도로 보고 문서 요청 안내(document_review의 빈손 경로)
        return Intent("document_review", "검토 명령(본문 없음) — 문서 요청 안내", doc_score=0)
    if len(m) >= 150 and score >= 2:
        return Intent("document_review", f"장문 + 문서 신호 {score}개", doc_score=score)

    # ── 유형 상담 ──
    if _BIZTYPE_Q_RE.search(m):
        return Intent("business_type_question", "사업유형 상담 패턴")

    # ── 인사·소관 밖 ──
    if len(m) <= 20 and _GREET_RE.search(m) and not _QUESTION_RE.search(m[2:]):
        return Intent("greeting", "인사")
    if _OOS_RE.search(m) and not re.search(r"감사|계약|용역|물품|공사|예산", m):
        return Intent("out_of_scope", "소관 밖 주제")

    return Intent("audit_question", "일반 업무 질문(기본)")
