"""일상감사 공용 모듈 + 레거시 Pipelines 어댑터 (SPEC §3.1).

운영 진입점은 functions/daily_audit_function.py(Open WebUI in-app Function)다.
이 파일은 (1) BANNER·GUIDE·결정론 모드(_mode_qa/_mode_law/_mode_target/
_mode_biztype 등) 공용 헬퍼의 원본이고, (2) 구 Pipelines 서버용 어댑터를
겸한다(스트리밍·첨부 인입은 Function 쪽에만 있음). 배포 시 audit_core/
패키지를 함께 반입해야 한다(SPEC §9).
"""

import os
import re
import sys
from datetime import date
from pathlib import Path

from pydantic import BaseModel

# 배포 환경(named volume)에서 audit_core를 찾도록 자기 위치 기준 경로 추가 (SPEC §9)
_here = Path(__file__).resolve().parent
for cand in (_here, _here.parent):
    if (cand / "audit_core").is_dir():
        sys.path.insert(0, str(cand))
        break

from audit_core.ledger.ledger import Ledger  # noqa: E402
from audit_core.rules.deadline import HolidayCalendar, audit_deadlines  # noqa: E402
from audit_core.rules.target_check import TargetInput, TargetRuleSet, check_target  # noqa: E402

# 첫 턴(대화 시작) 안내 — 어떤 입력이든 응답 맨 앞에 1회 표시.
# 문체 원칙(2026-07-15 사용자 지시): 내부 약어('5관문'·'역산') 금지, 날짜가
# 무엇의 날짜인지 명시, 처음 보는 사람이 읽고 바로 쓸 수 있게.
BANNER = """👋 **효규가영(効規可寧)** — 일상감사 AI 도우미입니다. *(첫 대화 안내 — 다음부터는 표시되지 않습니다)*
효율적으로 · 규정에 맞게 · 편안하게. 계약을 집행하기 **전에** 감사부서가 미리 검토하는 절차(일상감사)를 도와드립니다.

📎 **파일(hwp·hwpx·xlsx·pdf)을 첨부하거나 본문을 붙여넣으세요** — 대상 여부·빠진 서류·지적될 부분을 짚어 드립니다. 여러 파일을 올리면 문서끼리 금액·사업명이 맞는지도 맞대봅니다.
💬 "수의계약도 대상인가요?" 같은 절차 질문·사업유형 상담도 그냥 물어보세요. 대상 기준금액 등 자세한 안내는 `도움말`.

※ 결과는 AI 초안이며 최종 판단은 담당자의 몫입니다.

---
"""

GUIDE = """## 효규가영 사용법 — 이렇게 쓰시면 됩니다

### 📎 파일 첨부 또는 본문 붙여넣기 (제일 쉬운 방법)
의뢰서·공고문·계산서(hwp·hwpx·xlsx·pdf)를 **첨부하거나 본문을 붙여넣으면** 자동으로 점검합니다.
산출내역서(xlsx)는 산식 검산까지 자동으로 돌립니다.
여러 문서를 이어 붙여넣으면 문서끼리 금액·사업명이 맞는지도 맞대봅니다.
같은 문서의 예전 버전과 새 버전을 같이 붙여넣으면 **바뀐 부분만** 골라 검토합니다.

### 명령어 여섯 가지
| 이렇게 입력 | 무엇을 해주나 |
|---|---|
| `검토 <본문 붙여넣기>` | 감사팀용 **의견서 초안**까지 작성 (질의요지→사실관계→검토의견→종합→권고) |
| `대상? 협상용역 3억1천만원` | 이 사업이 일상감사 **대상인지 한 줄 판별** (유형과 금액만 있으면 됩니다) |
| `기한 2026-07-06` | **접수일**을 넣으면 → 감사의견 통보기한(7일)·조치결과 통보기한(14일)을 공휴일 반영해 계산 |
| `대장 2026` | 해당 **연도**의 일상감사 처리대장 조회 + 기한 지난 건 경고 |
| `법령 지방계약법 제22조` | **조문 원문**을 바로 보여줍니다(인용 가능 여부 태그 포함). `수의계약 법령 찾아줘`처럼 키워드로도 법률·자치법규·행정규칙을 검색합니다 |
| `관문 2026-10-01` | 정보화사업은 공고 전에 **사전협의·보안성 검토 등 5가지 사전절차**를 거쳐야 합니다. **공고 목표일**을 넣으면 그 날짜에 맞추기 위해 **각 절차를 언제까지 시작해야 하는지** 거꾸로 계산해 드립니다. 날짜 없이 `관문`만 치면 절차 5가지를 한눈에 보여줍니다 |

### 📏 대상 기준금액 (의성군 일상감사 규정 별표)
**용역 7천만 원↑ · 물품 2천만 원↑ · 종합공사 3억 원↑ · 비종합공사 2억 원↑ · 민간보조/위탁 1억 원↑** — 기준은 '추정금액'(추정가격+부가세+관급재료)입니다.

### 💬 자유 질문 · 유형 상담
"물품 1,500만 원인데 감사 받아야 하나요?", "조치결과는 언제까지 내야 하나요?" —
규정과 기준금액 안에서 답해 드리고, 애매하면 감사팀 확인을 안내합니다.
"이 사업은 용역이야?", "서버 구매+설치는 물품이야 공사야?" — 사업 내용을 말씀하시면
**어떤 유형(용역·물품·공사)에 해당하는지** 근거와 함께 짚어 드립니다.

---
※ 첨부 파일은 파기 운영 기준 확정 전까지 서버에 보관될 수 있습니다 — 민감 문서는 유의하세요. 스캔 이미지 PDF는 OCR 미지원(안내 후 원본 요청).
※ 기준금액은 의성군 일상감사 규정 별표 원문 기반(감사팀 최종 확인 전)입니다. 결과는 AI 초안이며 최종 판단은 담당자의 몫입니다."""

_AMOUNT_RE = re.compile(r"(?:(\d+(?:[.,]\d+)?)\s*억)?\s*(?:(\d+(?:,\d{3})*(?:\.\d+)?)\s*(만원|천만원|원))?")


def parse_amount(text: str) -> int | None:
    """'8500만원' '2억' '1억 5천만원' '850,000,000원' → 원 단위 정수."""
    text = text.replace(" ", "")
    total = 0
    m = re.search(r"(\d+(?:\.\d+)?)억", text)
    if m:
        total += int(float(m.group(1)) * 100_000_000)
        text = text[m.end():]
    m = re.search(r"(\d+(?:,\d{3})*(?:\.\d+)?)천만원?", text)
    if m:
        total += int(float(m.group(1).replace(",", "")) * 10_000_000)
    else:
        m = re.search(r"(\d+(?:,\d{3})*(?:\.\d+)?)만원?", text)
        if m:
            total += int(float(m.group(1).replace(",", "")) * 10_000)
        else:
            m = re.search(r"(\d+(?:,\d{3})*)원", text)
            if m:
                total += int(m.group(1).replace(",", ""))
    return total or None


class Pipeline:
    class Valves(BaseModel):
        OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        AUDIT_MODEL_REVIEW: str = os.getenv("AUDIT_MODEL_REVIEW", "qwen3:14b")
        AUDIT_MODEL_LIGHT: str = os.getenv("AUDIT_MODEL_LIGHT", "qwen3:8b")
        LAW_API_OC: str = os.getenv("LAW_API_OC", "")  # 공란이면 법령조회 캐시 전용 모드
        RUBRIC_PATH: str = os.getenv("RUBRIC_PATH", "./audit_core/rubric/rubric_v0_2.json")
        DEADLINE_NOTIFY: str = os.getenv("DEADLINE_NOTIFY", "7,7,14")
        # AUDIT_TRAIL_LEVEL, MASK_REVIEW_REQUIRED는 Valve로 노출하지 않음(운영 중 완화 방지)

    def __init__(self):
        self.id = "daily-audit-assistant"
        self.name = "일상감사 멀티 에이전트"
        self.valves = self.Valves()

    @staticmethod
    def _is_first_turn(messages: list) -> bool:
        """이전 assistant 응답이 없으면 첫 턴 — 시작 안내 배너 대상."""
        return not any(
            isinstance(m, dict) and m.get("role") == "assistant" and m.get("content")
            for m in (messages or [])
        )

    def pipe(self, user_message: str, model_id: str, messages: list, body: dict):
        msg = (user_message or "").strip()
        banner = BANNER if self._is_first_turn(messages) else ""

        try:
            # 관문은 의도 목록 밖의 부가 기능 — 접두 명령 유지
            if msg.startswith("관문"):
                return banner + self._mode_gates(msg)

            # 의도 분류(결정론) — 길이가 아니라 신호로 라우팅한다
            from audit_core.rules.intent import classify_intent
            it = classify_intent(msg)
            if it.intent == "document_review":
                explicit = msg.startswith("점검") or msg.startswith("검토")
                return self._mode_review(msg, messages, body, auto=not explicit, banner=banner)
            if it.intent == "ledger":
                return banner + self._mode_ledger(msg)
            if it.intent == "deadline":
                return banner + self._mode_deadline(msg)
            if it.intent == "law_lookup":
                return banner + self._mode_law(msg)
            if it.intent == "target_check":
                return banner + self._mode_target(msg)
            if it.intent == "business_type_question":
                return banner + self._mode_biztype(msg)
            if it.intent == "help":
                return (banner + GUIDE) if banner else GUIDE
            if it.intent == "greeting":
                return (banner + "👋 안녕하세요, 효규가영입니다. 문서 점검(파일 첨부·본문 붙여넣기), "
                        "대상 판별(`대상? 유형 금액`), 법령 조회(`법령 …`)를 도와드립니다.")
            if it.intent == "out_of_scope":
                return (banner + "😅 그 주제는 제 소관이 아니에요 — 저는 일상감사·계약 지원 전용입니다. "
                        "문서 점검이나 대상 판별이 필요하시면 말씀해 주세요.")
            # audit_question(기본) — 규정 요지 Q&A
            return banner + self._mode_qa(msg)
        except Exception as e:  # 규칙엔진 오류가 세션을 죽이면 안 됨
            return f"⚠ 처리 중 오류: {e}\n\n{GUIDE}"

    # ── 문서 검토 (LLM 축별 검토 DAG) ────────────────

    def _extract_doc(self, msg: str, messages: list, body: dict) -> str:
        """검토 대상 문서 텍스트 확보: 붙여넣기 본문 우선, 없으면 첨부/이전 메시지."""
        body_text = msg.split(None, 1)[1] if len(msg.split(None, 1)) > 1 else ""
        if len(body_text.strip()) >= 100:
            return body_text.strip()
        # Open WebUI가 첨부/RAG 컨텍스트를 이전 메시지에 주입한 경우 회수.
        # 반드시 user 역할만 — AI의 직전 답변(사용법 안내 등)을 '문서'로 오인해
        # 전축 판단불가 30건을 쏟아내던 실장애(2026-07-15) 방지.
        for m in reversed(messages or []):
            if not (isinstance(m, dict) and m.get("role") == "user"):
                continue
            content = m.get("content")
            if isinstance(content, str) and len(content.strip()) >= 100 and content.strip() != msg:
                return content.strip()
        return ""

    def _classify_biz(self, doc_text: str, allow_llm: bool = False):
        """사업성격 해석(BusinessClassifier) 우선 → 명시 라벨(detect_type_keyword) 폴백.

        분류기는 유형 '후보와 근거'만 낸다 — 대상판정은 결정론 룰엔진(check_target).
        결정론 신호가 빈약(low)하고 라벨도 없을 때만 LLM 보조(allow_llm).
        반환: (BusinessProfile, 정규화 유형 or None)
        """
        from audit_core.rules.biz_classifier import BusinessClassifier, to_target_type
        clf = BusinessClassifier()
        bp = clf.classify(doc_text)
        biz = to_target_type(bp) or self._detect_biz_type(doc_text)
        if biz is None and bp.confidence == "low" and allow_llm:
            bp = clf.classify_with_llm(doc_text, bp)
            biz = to_target_type(bp)
        return bp, biz

    def _detect_biz_type(self, doc_text: str) -> str | None:
        from audit_core.rules.target_check import TargetRuleSet
        rules = TargetRuleSet()
        kw = rules.detect_type_keyword(doc_text)  # 최장일치
        norm = rules.normalize_type(kw) if kw else None
        if norm and not norm.startswith("__EXCLUDED__:"):
            return norm
        return None

    # 계약방법 감지(오버레이·완결성용) — 구체 표현 우선, 공백 무시
    _METHOD_PATTERNS = (
        ("협상에 의한 계약", ("협상에의한계약", "협상계약")),
        ("수의계약", ("수의계약", "수의시담")),
        ("긴급입찰", ("긴급입찰",)),
    )

    def _detect_method(self, doc_text: str) -> str | None:
        compact = doc_text.replace(" ", "")
        for label, pats in self._METHOD_PATTERNS:
            if any(p in compact for p in pats):
                return label
        return None

    # 대상판정 프리체크용 — 금액이 이 라벨과 같은 줄에 있을 때만 신뢰(문서 내 임의
    # 숫자 오인 방지)
    _AMOUNT_LABEL = re.compile(r"추정\s*금액|추정\s*가격|총\s*사업비|사업비|계약금액|용역비|예산액")  # 추정금액=별표 기준 용어

    def _target_preface(self, doc: str, biz_type: str | None = None) -> str:
        """문서에서 유형·금액을 찾아 대상판정 한 줄 안내(참고). 불확실하면 침묵.

        biz_type이 오면(사업성격 분류기 결과) 그것으로 판정 — 라벨 없는 실문서 대응."""
        rules = TargetRuleSet()
        kw = biz_type or rules.detect_type_keyword(doc)
        if not kw:
            return ""
        amount = None
        for line in doc.splitlines():
            if self._AMOUNT_LABEL.search(line):
                amount = parse_amount(line)
                if amount:
                    break
        if not amount:
            return ""
        d = check_target(TargetInput(biz_type=kw, amount=amount), rules)
        icon = {"TARGET": "✅", "NOT_TARGET": "➖", "REVIEW": "🔎", "EXCLUDED": "🚫"}[d.decision]
        return (f"{icon} **대상판정(참고)** — {d.reason}\n"
                f"  _문서에서 추출한 금액 기준입니다. 정식 판정은 `대상? {kw} 금액`으로 확인하세요._")

    def _mode_review(self, msg: str, messages: list, body: dict,
                     auto: bool = False, banner: str = ""):
        doc = msg if auto else self._extract_doc(msg, messages, body)
        if not doc:
            yield (banner + "검토할 문서를 함께 넣어 주세요.\n"
                   "문서 본문(의뢰서·공고문·계산서·추진계획서)을 그대로 붙여넣으면 됩니다.\n"
                   "(문서 파일 자동 파싱은 파싱 관문 검증 후 연결 예정)")
            return

        # 지연 import: LLM 의존 모듈은 검토 모드에서만 로딩
        from audit_core.orchestrator import (
            Orchestrator,
            format_self_check,
            format_written_review,
        )
        from audit_core.rules.cross_check import split_bundle
        from audit_core.rules.rereview import detect_rereview, format_rereview
        from audit_core.rules.doc_type import detect_doc_type
        from audit_core.rules.target_check import rubric_group

        profile = detect_doc_type(doc)  # 공고문/계산서/추진계획서/의뢰서(전축)
        # 사업성격 해석(현실 표현→법정 유형) 우선, 명시 라벨은 폴백 — "플랫폼 구축"
        # 같은 문서가 '유형 미감지'로 빠지던 초입 한계 보완(2026-07-15)
        bp, biz_type = self._classify_biz(doc, allow_llm=True)
        group = rubric_group(biz_type)  # 미감지(None) → '기타'(공통 축)
        method = (bp.contract_method if bp.contract_method != "미상" else None) or self._detect_method(doc)
        doc_parts = split_bundle(doc)      # A5.5 문서 간 대조(표제 줄 분리, 2건 미만이면 미적용)
        if len(doc_parts) < 2:
            doc_parts = None
        rereview = detect_rereview(doc_parts) if doc_parts else None  # SOP ②: 변경점만 검토
        llm_doc = rereview.changed_text if rereview else None
        is_full = (not auto) and msg.startswith("검토")  # 검토=서면검토(감사팀), 그 외=자가점검
        label = "서면검토" if is_full else "자가점검"

        # 문서성 게이트(결정론): 공문서 신호가 하나도 없으면 7축을 돌리지 않는다
        # — 잡담·질문·비문서 장문에 전 에이전트가 출동해 '판단 불가 30건'을
        # 나열하던 실장애(2026-07-15) 차단. 신호: 문서 표지/사업유형/계약방법/
        # 서류 표제/라벨 금액/산식 라벨.
        _comp_probe = None
        try:
            from audit_core.rules.completeness import RequiredDocs as _RD
            _comp_probe = _RD().check(doc)
        except Exception:
            pass
        doc_signals = sum([
            bool(profile.reason and "미감지" not in profile.reason and "복합" not in profile.reason),
            bool(biz_type), bool(method),
            bool(_comp_probe and _comp_probe.recognized),
            bool(self._target_preface(doc)),
            bool(re.search(r"소\s*계|합\s*계|부가가치세|산\s*출|추정\s*가격|사업비", doc)),
        ])
        if doc_signals == 0:
            yield (banner + "🤔 붙여넣으신 내용에서 공문서 표지·사업유형·금액 같은 신호를 찾지 "
                   "못해 **검토를 시작하지 않았습니다** (문서가 아니라 질문·일반 글로 보입니다).\n"
                   "- 문서를 점검하려면: 의뢰서·공고문·계산서 **본문**을 그대로 붙여넣어 주세요.\n"
                   "- 질문이라면 그냥 물어보세요 — 아래에 우선 답해 드립니다.\n\n")
            yield self._mode_qa(doc[:300])
            return

        header = (banner
                  + f"📋 {label} 시작 — 문서유형: **{profile.label}** · "
                  f"사업유형: {biz_type or '미감지'} → 루브릭 **{group}**축"
                  + (f" · 계약방법: **{method}**(오버레이)" if method else "")
                  + "\n")
        if auto:
            header += "\n> 문서로 자동 인식했습니다. 감사팀용 의견서 초안이 필요하면 `검토` 뒤에 본문을 붙여넣으세요.\n"
        if not biz_type:
            header += ("\n> 사업유형(공사/용역/물품/민간보조)을 문서에서 찾지 못해 공통 축 기준으로 "
                       "검토합니다. 본문에 유형을 명시하면 정확도가 올라갑니다.\n")
        header += "\n"
        yield header

        if rereview:
            yield format_rereview(rereview) + "\n\n"
        if biz_type:
            ev = " ".join(bp.evidence[:3])
            yield (f"🧭 사업성격: **{bp.primary_type}"
                   + (f"({bp.subtype})" if bp.subtype else "") + f"** 후보 — 근거 {ev} "
                   + f"(신뢰 {bp.confidence})" + "\n")
            if bp.mixed:
                yield f"> ⚠ 혼합 요소: {bp.mixed_notes} — 담당자 확인 필요\n"
            yield "\n"
        preface = self._target_preface(doc, biz_type=biz_type)
        if preface:
            yield preface + "\n\n"

        # A2 서류 완결성 — 첫 번째 산출물(설계서 §6). 누락이 있어도 차단하지 않고
        # 안내만 하고 검토를 계속한다.
        try:
            from audit_core.rules.completeness import RequiredDocs, format_completeness
            comp = RequiredDocs().check(doc, method=method, biz_type=biz_type or group)
            yield format_completeness(comp) + "\n\n"
        except Exception:
            pass  # 완결성 확인 실패가 검토를 막으면 안 됨

        # 진행 상황 실시간 스트리밍 — 검토는 백그라운드 스레드, 진행 로그는 큐로 중계
        import queue
        import threading

        q: queue.Queue = queue.Queue()
        _done = object()
        holder: dict = {}

        def progress(m: str):
            q.put(m)

        def work():
            try:
                orch = Orchestrator()
                if is_full:
                    holder["render"] = format_written_review(
                        orch.written_review(group, doc, progress=progress, doc_profile=profile,
                                            contract_method=method, doc_parts=doc_parts,
                                            llm_doc_text=llm_doc))
                else:
                    holder["render"] = format_self_check(
                        orch.self_check(group, doc, progress=progress, doc_profile=profile,
                                        contract_method=method, doc_parts=doc_parts,
                                        llm_doc_text=llm_doc))
            except Exception as e:
                holder["error"] = e
            finally:
                q.put(_done)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        yield "진행 상황:\n```\n"
        while True:
            item = q.get()
            if item is _done:
                break
            yield f"- {item}\n"
        yield "```\n\n"
        t.join()

        if "error" in holder:
            yield f"⚠ 검토 중 오류: {holder['error']}\n(다시 시도하거나 담당자에게 문의하세요)"
            return
        yield holder["render"]

    # ── LLM 미관여 모드 ──────────────────────────────

    # ── 법령 조회·탐색 (`법령 …` / "○○ 법령 찾아줘") ─────────────

    _ARTICLE_RE = re.compile(r"^(.*?)\s*(제\s*\d+\s*조(?:의\s*\d+)?)\s*$")
    _LAW_STRIP = re.compile(r"법령|조문|찾아줘?|검색해?줘?|알려줘?|보여줘?|조회해?줘?|해줘|좀|요$")

    def _mode_law(self, msg: str) -> str:
        """① '지방계약법 제22조' 꼴 → 조문 원문(국가법령정보 실존 검증분) 조회
        ② 그 외 키워드 → 법률·자치법규·행정규칙(+'판례' 포함 시 판례) 탐색.
        결과에는 근거 태그([직접적용]/[취지참고])를 붙여 인용 권한을 안내한다."""
        from audit_core.agents.law_fetcher import LawFetcher
        from audit_core.agents.law_search import LawSearchClient
        from audit_core.rules.citation_tags import CitationTags

        query = self._LAW_STRIP.sub(" ", msg).strip(" ?.,~!")
        if not query:
            return "찾을 법령명이나 키워드를 함께 적어주세요. 예: `법령 지방계약법 제22조`, `수의계약 법령 찾아줘`"
        tags = CitationTags()

        # ① 조문 직접 조회 — 문장 속에서도 '…법/령/규칙 제N조'를 찾아낸다
        m = re.search(r"([가-힣A-Za-z0-9·\s]*?(?:법|령|규칙|조례|규정)[가-힣\s]*?)제\s*(\d+)\s*조(?:\s*의\s*(\d+))?", query)
        if m and m.group(1).strip():
            law_name = m.group(1).replace(" ", "")
            article = f"제{m.group(2)}조" + (f"의{m.group(3)}" if m.group(3) else "")
            try:
                art = LawFetcher().fetch_ref(f"{law_name}-{article}")
                tag = tags.classify(art.law_name)
                body = art.text if len(art.text) <= 2400 else art.text[:2400] + " …(이하 생략)"
                sub = ("\n> ℹ 항·호·목 단위는 아래 조 전문에서 해당 부분을 확인하세요"
                       "(자동 인용은 조 단위까지 검증됩니다)." if re.search(r"\d\s*항|\d\s*호|[가-하]\s*목", msg) else "")
                return (f"## 📚 {art.law_name} {art.article}\n"
                        f"> 근거 태그 **[{tag}]** — {tags.desc(tag)}{sub}\n\n{body}\n\n"
                        f"_국가법령정보 실존 검증분(시행 {getattr(art, 'effective_date', '') or '—'})._")
            except Exception:
                pass  # 조회 실패 → 아래 키워드 탐색으로 폴백

        # ② 키워드 탐색 (법률 + 자치법규 + 행정규칙, '판례' 명시 시 판례도)
        c = LawSearchClient()
        if not c.enabled:
            return "법령 탐색 서비스(law-mcp)가 꺼져 있습니다 — 관리자에게 LAW_MCP_URL 설정을 문의하세요."
        buckets = [("법률·시행령", c.search_law(query)),
                   ("자치법규", c.search_ordinance(query)),
                   ("행정규칙(훈령·예규)", c.search_admrule(query))]
        if "판례" in msg:
            buckets.append(("판례", c.search_precedent(query)))

        lines = [f"## 📚 '{query}' 법령 탐색 결과"]
        total = 0
        for label, hits in buckets:
            if not hits:
                continue
            lines.append(f"\n**{label}**")
            for h in hits:
                tag = tags.classify(f"{h.title} {h.snippet}")  # 소관 부처(조달청 등)까지 분류
                lines.append(f"- {h.title} ({h.ref}) — {h.snippet}  `[{tag}]`")
                total += 1
        if total == 0:
            return (f"'{query}'로 찾은 법령이 없습니다. 다른 표현으로 다시 시도하거나, "
                    f"조문을 아신다면 `법령 지방계약법 제22조`처럼 정확히 입력해 보세요.")
        lines.append("\n_[직접적용]=결론 근거 인용 가능 · [취지참고]=취지 참고만(결론 근거 금지). "
                     "조문 원문은 `법령 <법령명> 제N조`로 확인하세요._")
        return "\n".join(lines)

    def _mode_biztype(self, msg: str) -> str:
        """사업유형 상담 — "서버 구매+설치는 물품이야 공사야?" 류. 결정론 분류기로 답한다."""
        from audit_core.rules.biz_classifier import BusinessClassifier
        bp = BusinessClassifier().classify(msg)
        if bp.primary_type in ("확인필요", ""):
            return self._mode_qa(msg)  # 신호가 없으면 일반 Q&A로
        lines = [f"🧭 말씀하신 사업은 **{bp.primary_type}"
                 + (f"({bp.subtype})" if bp.subtype else "") + "** 후보입니다."]
        lines.append(f"- 근거: {' '.join(bp.evidence[:4])} — {bp.reason}")
        if bp.mixed:
            lines.append(f"- ⚠ 혼합 요소: {bp.mixed_notes}")
        if "설치" in msg.replace(" ", "") and "공사" not in bp.primary_type:
            lines.append("- '설치'는 부수 행위라 그것만으로 공사가 되지 않습니다 — "
                         "전기·정보통신 등 공종과 시공 내용이 주가 될 때 공사로 봅니다.")
        if bp.contract_method != "미상":
            lines.append(f"- 계약방법 '{bp.contract_method}'은 사업유형이 아니라 별도 확인 항목입니다.")
        th = {"용역": "7천만 원", "물품": "2천만 원", "종합공사": "3억 원", "비종합공사": "2억 원",
              "민간자본보조": "1억 원", "민간위탁": "1억 원"}.get(bp.primary_type)
        if th:
            lines.append(f"- 의성군 기준 **{bp.primary_type} {th} 이상**이면 일상감사 대상입니다 — "
                         f"금액과 함께 `대상? {bp.primary_type} 금액`으로 판정해 드립니다.")
        lines.append("\n_(유형 후보 안내 — 확정은 감사팀 판단)_")
        return "\n".join(lines)

    # ── 자유 질문 (Q&A — 닫힌 과제: 주입한 규정 요지 밖은 답하지 않음) ──

    _QA_TRIGGER = re.compile(r"\?|인가요|한가요|하나요|할까요|까요\b|어떻게|알려|뭐예요|무엇|왜")

    def _qa_context(self) -> str:
        """LLM에 주입할 근거 — 별표 기준(룰 파일에서 동적 생성) + 규정 절차 요지."""
        rules = TargetRuleSet()
        lines = [f"[일상감사 대상 기준 — 의성군 일상감사 규정 별표, 금액은 {rules.amount_basis}]"]
        for name, spec in rules.categories.items():
            if spec.get("kind") == "always":
                lines.append(f"- {name}: 금액 무관 {spec.get('decision', '')} ({spec.get('condition', '')})")
            else:
                inc = "이상" if spec.get("inclusive", True) else "초과"
                lines.append(f"- {name}: {int(spec['min_amount']):,}원 {inc}")
        lines.append(
            "\n[중요] 수의계약·협상에 의한 계약 같은 '계약방법'과 무관하게, 사업의 "
            "목적물 유형(공사·용역·물품 등)과 금액으로 대상을 판정한다 — 예: 수의계약이라도 "
            "물품 2천만 원 이상이면 일상감사 대상이다.")
        # 판정 선결 규칙(설계변경·채무부담·긴급·사후 등)
        lines.append("\n[선결 판정 규칙]")
        for pre in rules.preconditions:
            if pre.get("reason"):
                lines.append(f"- {pre['reason']}")
        # 수의계약 근거 조문 원문(캐시) — 금액 한도·여성기업 특례 등 실질 답변용
        try:
            from audit_core.agents.law_fetcher import LawFetcher
            art = LawFetcher().fetch_ref("지방계약법시행령-제25조")
            lines.append("\n[지방계약법 시행령 제25조(수의계약에 의할 수 있는 경우) 원문 발췌]\n"
                         + art.text[:3800])  # 금액 기준(2천만·5천만)·특례 열거가 2,100자 이후에 있음
        except Exception:
            pass
        lines.append(
            "\n[자주 묻는 기준·시스템 안내]\n"
            "- 협상에 의한 계약: 시행령 제43조 — 물품·용역만 가능(공사 불가), 전문성·기술성 등 사유 소명 필요. 절차는 제안서 평가 → 협상 → 계약. 구체 적정성은 이 시스템에 문서를 붙여넣거나 첨부하면 검토해 준다.\n"
            "- 분할 발주: 동일 목적 사업을 나눠 수의계약 기준금액을 회피하면 지적 대상이다.\n"
            "- 특정 상표·모델 지정: 호환성 등 사유가 있어도 규격서에 '또는 동등 이상'(증빙 제시) 단서를 함께 적어야 한다.\n"
            "- 원가심사와의 관계: 일상감사를 받은 사업은 원가심사를 받은 것으로 본다(규정 제10조④ — 일상감사→원가심사 방향). 원가심사를 먼저 받은 경우의 일상감사 생략 여부는 규정에 명시가 없다(단정하지 말 것).\n"
            "- 재공고·재심사: 직전 버전과 개정본을 함께 붙여넣거나 파일로 첨부하면 바뀐 부분만 골라 검토한다. 다시 받아야 하는지는 감사팀과 협의.\n"
            "- 서식: 일상감사 요청서(별지 제1호)·의견서(제2호)·조치결과 통보서(제3호)·재검토 요청서(제4호)·긴급입찰 사유서 양식을 감사팀(기획예산과)이 보유 — 파일은 감사팀에 요청하라고 안내. 긴급입찰 사유서는 재해·긴급 수요로 입찰 기간을 단축할 때 사유 소명용.\n"
            "- 과거 지적 사례 자동 조회는 준비 중 — 감사팀 문의 안내.")
        lines.append(
            "\n[절차 요지 — 의성군 일상감사 규정]\n"
            "- 일상감사는 최종 결재 전에 실시가 원칙(제5조). 의견서를 받기 전에는 집행행위 불가(제10조).\n"
            "- 감사관은 요청받은 날부터 7일 이내 의견서 통보(제8조① — 협의로 7일 이내 1회 연장 가능).\n"
            "- 집행부서는 의견서를 받은 날부터 14일 이내 조치결과 통보(제8조② — 적정 의견은 생략 가능).\n"
            "- 의견에 이의가 있으면 7일 이내 재검토 요청 가능(제9조).\n"
            "- 의뢰는 별지 제1호 일상감사 요청서 + 관련 서류 첨부(제6조).\n"
            "- 일상감사를 받은 사업은 원가심사를 받은 것으로 본다(제10조④)."
        )
        return "\n".join(lines)

    def _mode_qa(self, msg: str) -> str:
        """짧은 자유 질문 응답. LLM 장애·불명 시 GUIDE 폴백(중단 금지)."""
        try:
            from audit_core.agents.base import OllamaClient
            from audit_core.config import get_settings
            client = OllamaClient()
            answer = client.chat_text(
                model=get_settings().AUDIT_MODEL_LIGHT,
                system=(
                    "너는 의성군 일상감사 안내 도우미 효규가영이다. 공무원의 질문에 "
                    "동료처럼 자연스럽고 짧게(2~5문장) 답한다.\n"
                    "- 업무 질문: 아래 [근거]를 우선해 답한다. 근거에 답이 없으면 거절하지 "
                    "말고, 규정에 명시가 없다는 사실을 문장 속에 자연스럽게 언급한 뒤 "
                    "일반적인 지방계약 실무 방향을 참고로 설명한다. 이 지시문의 표현을 "
                    "그대로 옮겨 적지 않는다.\n"
                    "- 수치 규율: 금액·비율·기한·조문 항호 번호는 근거에 있는 그대로만. "
                    "예외·특례가 함께 적혀 있으면 같이 언급한다. 없으면 만들지 않는다.\n"
                    "- 문체 규율: 답 끝에 상용구·꼬리 안내문·정해진 마무리 문장을 붙이지 않는다. "
                    "누구에게 확인하라는 말로 끝맺지 않는다. 감사팀 문의나 "
                    "명령어(`대상? 유형 금액`, `법령 …`) 안내는 그 답에 실제로 도움이 될 때만 "
                    "문장 속에 자연스럽게 **한 번만** 넣고, 같은 말을 반복하지 않는다.\n"
                    "- 인사·잡담: 한 문장으로 받고 무엇을 도울 수 있는지만 짧게. 날씨·뉴스 등 "
                    "소관 밖은 사실을 지어내지 말고 '저는 일상감사·계약 관련만 도울 수 있어요'로."
                ),
                prompt=f"[근거]\n{self._qa_context()}\n\n[질문]\n{msg}",
            )
            answer = answer.strip()
            # 고정 마무리 문구 제거(2026-07-15 사용자 지시) — "감사팀 … 확인이
            # 필요합니다"류 꼬리 문장은 프롬프트만으로 안 사라져(8b) 코드로 자른다.
            canned = re.compile(
                r"\s*[^.!?\n]*감사팀[^.!?\n]*(?:확인|문의)[^.!?\n]*"
                r"(?:필요합니다|필요하다|바랍니다|권합니다|권장합니다|받으세요|해보세요)[.!?]?\s*$")
            for _ in range(2):  # 겹으로 붙는 경우까지
                answer = canned.sub("", answer).strip()
            return answer
        except Exception:
            # LLM 장애 — 사용법 전체를 덤프하지 않고 짧게 안내(2026-07-15 명세)
            return ("지금은 답변 생성이 어렵습니다. 급하시면 이렇게 해보세요 — "
                    "`대상? 유형 금액`(대상 판별) · `법령 지방계약법 제22조`(조문) · "
                    "`도움말`(전체 사용법) · 감사팀(기획예산과) ☎ 문의")

    def _mode_gates(self, msg: str) -> str:
        """`관문` = 5관문 현황 카드 / `관문 2026-10-01` = 공고 목표일 역산."""
        from audit_core.rules.gates import GateSet, format_backplan, format_overview
        gs = GateSet()
        m = re.search(r"(\d{4}-\d{2}-\d{2})", msg)
        if m:
            return format_backplan(gs, date.fromisoformat(m.group(1)))
        return format_overview(gs)

    def _mode_target(self, msg: str) -> str:
        rules = TargetRuleSet()
        biz_type = rules.detect_type_keyword(msg)  # 최장일치(전기공사>공사, 학술용역>용역)
        amount = parse_amount(msg)
        if amount is None:
            return ("금액을 함께 입력해 주세요. 예: `대상? 종합공사 25억원`\n"
                    "선택: `수의계약`/`지명경쟁`, `긴급`, `변경계약`, `계약후` 등을 덧붙이면 반영됩니다.")
        if not biz_type:
            return (f"사업유형을 인식하지 못했습니다. 인식 유형: {', '.join(rules.types)}.\n"
                    "이 중 하나로 다시 입력하시거나, 해당 없으면 감사팀(기획예산과)에 확인하세요.")

        # 메시지에서 계약방식·단계·플래그 파싱
        method = next((m for m in rules.method_aliases if m in msg), None)
        stage = "사후" if any(k in msg for k in ("계약후", "체결후", "사후")) else "사전"
        flags = {f for f in ("긴급", "재해복구", "재난", "변경계약", "실지감사대상", "복무감사") if f in msg}

        inp = TargetInput(
            biz_type=biz_type, amount=amount,
            method=method, stage=stage, flags=frozenset(flags),
            cumulative_amount=amount if "변경계약" in flags else None,
        )
        d = check_target(inp, rules)
        icon = {"TARGET": "✅", "NOT_TARGET": "➖", "REVIEW": "🔎", "EXCLUDED": "🚫"}[d.decision]
        lines = [f"{icon} **{d.label}**  ({d.rule_id})",
                 f"- 판정: {d.reason}",
                 f"- 근거: {', '.join(d.basis)}"]
        if d.provisional:
            lines.append("- ⚠ 규칙·기준금액은 세부규정 확정 전 잠정값입니다.")
        lines += [f"- 참고: {n}" for n in d.notes]
        if d.decision == "TARGET":
            lines.append("→ 다음 절차: 계약 체결 전 일상감사 의뢰서를 감사팀(기획예산과)에 제출")
        elif d.decision == "REVIEW":
            lines.append("→ 감사팀(기획예산과)에 대상 여부를 문의하세요.")
        return "\n".join(lines)

    def _mode_deadline(self, msg: str) -> str:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", msg)
        anchor = date.fromisoformat(m.group(1)) if m else date.today()
        cal = HolidayCalendar()
        r = audit_deadlines(anchor, cal)
        out = [
            f"접수일 {anchor.isoformat()} 기준 (잠정 {self.valves.DEADLINE_NOTIFY}일):",
            f"- 감사의견 통보기한: **{r['notify'].due.isoformat()}**" + (" (휴일 이월)" if r["notify"].rolled else ""),
            f"- 재검토 기한(통보기한 기산 가정): {r['recheck'].due.isoformat()}",
            f"- 조치결과 통보기한: {r['action_report'].due.isoformat()}",
        ]
        if not r["notify"].calendar_covered:
            out.append("⚠ 해당 연도 공휴일 데이터 미등록 — 주말만 반영됨")
        return "\n".join(out)

    def _mode_ledger(self, msg: str) -> str:
        m = re.search(r"(\d{4})", msg)
        year = int(m.group(1)) if m else date.today().year
        ledger = Ledger()
        entries = ledger.list_year(year)
        if not entries:
            return f"{year}년 처리대장에 등록된 건이 없습니다."
        lines = [f"**{year}년 일상감사처리대장** ({len(entries)}건)", "",
                 "| 접수번호 | 의뢰부서 | 건명 | 유형/금액대 | 접수일 | 통보기한 | 처리결과 |",
                 "|---|---|---|---|---|---|---|"]
        for e in entries:
            lines.append(
                f"| {e.entry_no} | {e.dept} | {e.title} | {e.biz_type}/{e.amount_band} "
                f"| {e.receipt_date} | {e.notify_due} | {e.result_code or '—'} |"
            )
        overdue = ledger.overdue(year, date.today())
        if overdue:
            lines.append(f"\n⚠ 통보기한 경과·미통보: {', '.join(e.entry_no for e in overdue)}")
        return "\n".join(lines)
