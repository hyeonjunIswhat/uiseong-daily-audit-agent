"""에이전트 입출력 pydantic 스키마 (SPEC §3.2). Ollama format 강제·검증에 사용."""

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["NA", "OK", "FLAG", "UNABLE"]


class AxisItemResult(BaseModel):
    item_id: str
    verdict: Verdict
    evidence: str = Field(description="판정 근거. 문서에서 확인한 사실 또는 미비 내용")
    severity: int = Field(ge=1, le=3, default=1)


class AxisResult(BaseModel):
    axis: str
    items: list[AxisItemResult]


class SectionMap(BaseModel):
    section_id: str
    title: str
    char_len: int


# ── 5단계: 검증 2차 · 의견서 초안 (SPEC §3.2·3.3) ──────────────

Certainty = Literal["명확", "높음", "보통", "낮음"]


class ContextCheck(BaseModel):
    """verifier 2차(LLM 문맥검증) 출력. 1차(결정론) 통과 지적후보만 대상.

    supports=False는 인용 조문이 지적을 뒷받침하지 못한다는 뜻 — 해당 지적은
    '확인 필요'로 강등된다. 이 검증은 강등만 가능하고 1차 결정론 판정을
    번복하거나 새 지적을 만들지 못한다(flag-only, SPEC §3.3).
    """

    item_id: str
    supports: bool = Field(description="인용 조문이 지적 내용을 문맥상 뒷받침하는가")
    reason: str = Field(description="판단 근거 한 문장")


class OpinionIssue(BaseModel):
    """의견서 검토의견의 쟁점 1건 (IRAC 구조)."""

    title: str = Field(description="쟁점 제목")
    issue: str = Field(description="Issue — 법적 질문 형태의 쟁점")
    rule: str = Field(description="Rule — 관련 법령·규정 근거")
    application: str = Field(description="Application — 사실관계에 적용한 검토")
    conclusion: str = Field(description="Conclusion — 이 쟁점의 소결")
    certainty: Certainty = Field(description="이 소결의 확실성 수준")


class OpinionDraft(BaseModel):
    """synthesizer 출력 — 의견서 본문(면책·경고는 포매터가 결정론적으로 부가)."""

    query: str = Field(description="질의 요지 — 무엇을 검토했는가 한 문장")
    facts: str = Field(description="사실관계 — 문서에서 확인된 사실만. 미확인은 '~로 전제'")
    issues: list[OpinionIssue]
    overall: str = Field(description="종합 의견")
    recommendations: list[str] = Field(description="권고 사항(즉시/보완/확인)")


class ContextCheckBatch(BaseModel):
    """2차 문맥검증 일괄 출력(2026-07-15 성능 규율) — 지적후보 전체를 1콜로 판정.

    checks에 없는 item_id는 보수적으로 supports=True(지적 유지, 사람 확인)로
    처리된다 — 이 검증은 강등 전용이므로 미회신이 지적을 지우면 안 된다."""

    checks: list[ContextCheck]


class TriageResult(BaseModel):
    """검토 축 사전 선별(트리아지) — 경량 모델 1콜로 관련 축만 고른다.

    좁히기 전용: 여기 없는 축은 '미검토(사유 표시)'로 남는다(침묵 금지).
    확신이 없으면 포함하라고 지시하므로 미탐 방향으로 보수적이다."""

    axes: list[str] = Field(description="문서 내용으로 판단할 근거가 있는 축 번호 목록")
    reason: str = Field(description="선별 이유 한 문장")


class LawSearchHit(BaseModel):
    """law-mcp 탐색 레인 결과 1건 (조례·행정규칙·판례). 인용 전 재검증 대상."""

    target: str          # ordin | admrul | prec
    title: str
    ref: str = ""        # 조문·판례 식별자
    snippet: str = ""
    verified: bool = False  # law_fetcher 재검증 통과 여부(조례/법령만 해당)
