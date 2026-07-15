"""고정 DAG 실행기 (SPEC §3.1, DESIGN.md 3.3). 자가점검·서면검토 파이프.

자가점검(#2): 활성축 검토 → 법령 첨부 → 산식 검증 → 리포트. (종합·의견서 비활성)
서면검토(#5·6): 위 + 종합·2차검증·의견서 초안 — 4단계 잔여(synthesizer 후속).

결정론: 동일 입력 → 동일 축 경로. 진행 상황은 콜백으로 중계(비동기 UX).
등급1 원문은 세션 메모리에만, 이력에는 판정 코드·메타만.
"""

from dataclasses import dataclass, field
from typing import Callable

from audit_core.agents.axis_reviewer import AxisReviewer, ContractMethodOverlay, Rubric
from audit_core.agents.context_verifier import ContextVerifier
from audit_core.agents.law_fetcher import LawFetcher
from audit_core.agents.law_search import LawSearchClient
from audit_core.agents.schemas import AxisResult, ContextCheck, LawSearchHit, OpinionDraft
from audit_core.agents.synthesizer import Finding, Synthesizer
from audit_core.agents.verifier import NumericCheck, arithmetic_flags
from audit_core.rules.citation_tags import CitationTags, format_tagged_refs
from audit_core.rules.closing import ClosingSuggestion, scan_forbidden, suggest
from audit_core.rules.cross_check import CrossFlag, DocPart, cross_check
from audit_core.rules.doc_type import DocTypeResult
from audit_core.rules.merit_gate import MeritExclusion, MeritGate, format_exclusions

ProgressFn = Callable[[str], None]


@dataclass
class ReviewReport:
    biz_type: str
    axis_results: list[AxisResult] = field(default_factory=list)
    numeric_flags: list[NumericCheck] = field(default_factory=list)
    law_context_used: list[str] = field(default_factory=list)
    provisional_rubric: bool = True
    # 문서유형 프로파일로 스킵한 축 — (축 표시명, 사유). 침묵 금지(제약 A2)
    skipped_axes: list[tuple[str, str]] = field(default_factory=list)
    # 계약방법 오버레이(REBUILD 회차 1) — 감지된 계약방법과 분야-방법 모순(결정론 지적)
    contract_method: str | None = None
    method_incompat: str | None = None
    # 문서 간 정합성 대조(A5.5, 회차 2) — 결정론, 앵커 포함
    cross_flags: list[CrossFlag] = field(default_factory=list)
    # Tier 2 분야(자체 사례 미보유) — 라벨 강제 노출(F7 방어)
    tier2: bool = False

    def flags(self) -> list[tuple[str, object]]:
        out = [(ar.axis, it) for ar in self.axis_results for it in ar.items if it.verdict == "FLAG"]
        out.sort(key=lambda x: -x[1].severity)
        return out

    def unable(self) -> list[tuple[str, object]]:
        return [(ar.axis, it) for ar in self.axis_results for it in ar.items if it.verdict == "UNABLE"]


@dataclass
class WrittenReview:
    """서면검토 산출물 — 자가점검 리포트 + 검증·의견서 초안 (구현 5단계)."""

    report: ReviewReport
    confirmed: list[Finding] = field(default_factory=list)     # 1·2차 통과 지적
    needs_review: list[tuple[str, str]] = field(default_factory=list)  # (item_id, 강등 사유)
    context_checks: list[ContextCheck] = field(default_factory=list)
    search_hits: list[LawSearchHit] = field(default_factory=list)
    opinion: OpinionDraft | None = None
    # REBUILD 회차 1 — 실익 게이트 제외분(검증 가능하게 사유 보존)·종결구 제안·근거 태그
    merit_excluded: list[MeritExclusion] = field(default_factory=list)
    closings: list[ClosingSuggestion] = field(default_factory=list)
    ref_tags: dict[str, str] = field(default_factory=dict)  # 인용·참조 법령 전체의 태그


class Orchestrator:
    def __init__(
        self,
        reviewer: AxisReviewer | None = None,
        rubric: Rubric | None = None,
        law_fetcher: LawFetcher | None = None,
        context_verifier: ContextVerifier | None = None,
        synthesizer: Synthesizer | None = None,
        law_search: LawSearchClient | None = None,
        overlay: ContractMethodOverlay | None = None,
        citation_tags: CitationTags | None = None,
        merit_gate: MeritGate | None = None,
    ):
        self.rubric = rubric or Rubric()
        self.reviewer = reviewer or AxisReviewer()
        self.law = law_fetcher or LawFetcher()
        # 서면검토 전용 — 지연 생성 대신 주입 가능(테스트). 기본값은 실호출 클라이언트.
        self.context_verifier = context_verifier or ContextVerifier()
        self.synthesizer = synthesizer or Synthesizer()
        self.law_search = law_search or LawSearchClient()
        # REBUILD 회차 1 — 계약방법 오버레이·인용 태그 규율·실익 게이트(전부 결정론)
        self.overlay = overlay or ContractMethodOverlay()
        self.tags = citation_tags or CitationTags()
        self.merit_gate = merit_gate or MeritGate()

    _AXIS_SIGNALS: dict | None = None  # axis_signals.yaml 캐시(클래스 공유)

    def _triage_axes(self, axes: list[dict], doc_text: str, progress: ProgressFn) -> tuple[list[dict], list[dict]]:
        """축 사전 선별 — 규칙(키워드 신호)만으로 관련 축을 남긴다. LLM 0회.

        성능 규율(2026-07-15): 선별에 8b 1콜을 쓰던 구조를 폐지 — 축 선별은
        원칙적으로 LLM을 쓰지 않는다. 안전장치는 유지: 좁히기 전용(새 축 추가
        불가), 신호 매칭이 2축 미만이면 전축 폴백, 신호 사전에 없는 축(오버레이
        M1·M2 등)은 항상 유지, 스킵 축은 사유와 함께 미검토 표시(침묵 금지).
        """
        from audit_core.config import get_settings
        st = get_settings()
        if not st.AUDIT_TRIAGE or len(axes) <= 3:
            return axes, []
        if Orchestrator._AXIS_SIGNALS is None:
            import yaml
            from pathlib import Path
            path = Path(__file__).parent / "rules" / "axis_signals.yaml"
            Orchestrator._AXIS_SIGNALS = (yaml.safe_load(path.read_text(encoding="utf-8"))
                                          or {}).get("axes", {})
        signals = Orchestrator._AXIS_SIGNALS
        compact = doc_text.replace(" ", "")
        keep, skipped = [], []
        matched = 0
        for a in axes:
            kws = signals.get(a["axis"])
            if kws is None:           # 신호 사전 밖(오버레이 등) → 항상 유지
                keep.append(a)
                continue
            hits = [kw for kw in kws if kw in compact]
            if hits:
                keep.append(a)
                matched += 1
            else:
                skipped.append(a)
        if matched < 2:               # 신호 부족 — 좁히지 않는다(전축 폴백)
            return axes, []
        if skipped:
            progress(f"🧭 판정 에이전트가 규칙 선별로 {len(skipped)}개 축을 미검토 처리했습니다"
                     f"({', '.join(a['axis'] for a in skipped)}축) — 문서에 관련 신호 없음")
        return keep, skipped

    def _law_context(self, refs: list[str], progress: ProgressFn) -> tuple[str, list[str]]:
        blocks, used = [], []
        for ref in refs:
            try:
                art = self.law.fetch_ref(ref)
                blocks.append(f"[{ref}] {art.law_name} {art.article}\n{art.text}")
                used.append(ref)
            except Exception:
                continue
        if used:
            progress(f"📚 관련 법령 {len(used)}건의 원문을 확보했습니다")
        elif refs:
            progress("📚 법령 원문을 가져오지 못했습니다(캐시 없음·OC 미설정) — 발췌 없이 검토를 계속합니다")
        return "\n\n".join(blocks), used

    def self_check(
        self,
        biz_type: str,
        doc_text: str,
        progress: ProgressFn | None = None,
        doc_profile: DocTypeResult | None = None,
        contract_method: str | None = None,
        doc_parts: list[DocPart] | None = None,
        llm_doc_text: str | None = None,
        should_stop=None,
    ) -> ReviewReport:
        """자가점검 DAG (LLM: 축별 검토 / 규칙: 산식 검증).

        doc_profile이 좁혀진 유형(공고문·계산서·추진계획서)이면 판단 가능한 축만
        검토하고, 스킵 축은 사유와 함께 리포트에 남긴다. 산식 검산은 항상 수행.
        contract_method가 있으면 계약방법 오버레이 축을 겹치고(SOP 제3부 공통모듈),
        분야-방법 모순(공사+협상계약)은 결정론 지적으로 기록한다.
        """
        progress = progress or (lambda _: None)
        axes = self.rubric.active_axes(biz_type)

        report = ReviewReport(
            biz_type=biz_type, provisional_rubric=self.rubric.provisional,
            contract_method=contract_method,
        )
        report.tier2 = biz_type in getattr(self.rubric, "tier2", set())
        report.method_incompat = self.overlay.incompatibility(contract_method, biz_type)
        if report.method_incompat:
            progress(f"🧭 판정 에이전트가 계약방법-분야 모순을 발견했습니다: {report.method_incompat}")

        if doc_profile and doc_profile.narrows and axes:
            allowed = set(doc_profile.axes)
            skipped = [a for a in axes if a["axis"] not in allowed]
            axes = [a for a in axes if a["axis"] in allowed]
            report.skipped_axes = [
                (f"{a['axis']}. {a['title']}", doc_profile.skip_note) for a in skipped
            ]
            if skipped:
                progress(
                    f"문서유형 '{doc_profile.label}' — {len(skipped)}개 축 미검토"
                    f"({', '.join(a['axis'] for a in skipped)}축): {doc_profile.skip_note}"
                )

        # 축 트리아지(사전 선별) — 문서와 무관한 축은 미검토로(사유 표시).
        # 계약방법 오버레이 축은 선별 대상이 아니라 항상 유지된다.
        axes, triage_skipped = self._triage_axes(axes, llm_doc_text or doc_text, progress)
        report.skipped_axes += [
            (f"{a['axis']}. {a['title']}", "규칙 선별 — 문서에 관련 신호 없음(필요 서류 첨부 시 재검토)")
            for a in triage_skipped
        ]

        # 계약방법 오버레이 축은 문서유형 좁히기와 무관하게 겹친다 — 계약방법 서류
        # 자체를 보는 체크리스트라서(모순 건은 위 결정론 지적으로 갈음, 축 미추가)
        overlay_axes = self.overlay.overlay_axes(contract_method, biz_type)
        if overlay_axes:
            axes = axes + overlay_axes
            progress(f"🧭 판정 에이전트가 체크리스트를 겹칩니다 — {overlay_axes[0]['title']}")

        if not axes:
            if doc_profile and doc_profile.narrows:
                progress("🧭 이 문서유형으로 판단할 수 있는 검토 축이 없습니다 — 산식 검산만 수행합니다")
            else:
                progress(f"🧭 '{biz_type}'에 해당하는 검토 축을 찾지 못했습니다 — 사업유형을 확인해 주세요")
            report.numeric_flags = arithmetic_flags(doc_text)
            return report

        progress(f"🧭 판정 에이전트가 검토 계획을 세웠습니다 — {len(axes)}개 축({', '.join(a['axis'] for a in axes)})을 차례로 살펴봅니다")

        # 결정론적 산식 검증 먼저 (LLM 무관, 즉시)
        report.numeric_flags = arithmetic_flags(doc_text)
        # 문서 간 정합성 대조(A5.5) — 번들이 문서 2건 이상으로 분리됐을 때만
        if doc_parts and len(doc_parts) >= 2:
            progress(f"🔎 대조 에이전트가 문서 {len(doc_parts)}건의 사업명·금액·배점·기간을 맞대보고 있습니다…")
            report.cross_flags = cross_check(doc_parts)
            if report.cross_flags:
                progress(f"🔎 문서 간 불일치 {len(report.cross_flags)}건을 찾았습니다")
            else:
                progress("🔎 문서끼리 맞대봤습니다 — 이상 없습니다 ✓")
        if report.numeric_flags:
            progress(f"🔢 검증 에이전트가 산식을 재계산해 불일치 {len(report.numeric_flags)}건을 찾았습니다")

        # 축별 검토 — 활성 축 전체를 **단일 구조화 호출**로(2026-07-15 개선:
        # 같은 본문을 축마다 반복 전송하던 병목 제거, 축별 스키마·항목 추적은
        # review_all이 회신을 재그룹해 유지). 법령 발췌도 1회만 수집해 재사용.
        # 재심사 모드면 LLM에는 변경 조각만(llm_doc_text) — 결정론 검산은 전체.
        # 긴 원문은 결정론 다이제스트로 축약(성능 규율 — 원문 전체 반복 투입 금지).
        from audit_core.rules.digest import build_review_digest
        llm_doc = build_review_digest(llm_doc_text or doc_text)
        if len(llm_doc) < len(llm_doc_text or doc_text):
            progress(f"✂️ 긴 문서라 금액·산식·표제 중심으로 발췌해 검토합니다"
                     f"(원문 {len(llm_doc_text or doc_text):,}자 → 발췌 {len(llm_doc):,}자)")
        titles = " · ".join(a["title"] for a in axes)
        progress(f"🔍 검토 에이전트가 {len(axes)}개 축({titles[:60]}…)을 한 번에 살펴보고 있습니다…"
                 if len(titles) > 60 else
                 f"🔍 검토 에이전트가 {len(axes)}개 축({titles})을 한 번에 살펴보고 있습니다…")
        refs = self.rubric.all_law_refs(axes)
        law_ctx, used = self._law_context(refs, progress)
        report.law_context_used.extend(used)
        results = self.reviewer.review_all(axes, llm_doc, law_ctx, should_stop=should_stop)
        for i, result in enumerate(results, 1):
            report.axis_results.append(result)
            axis = axes[i - 1]
            n_flag = sum(1 for it in result.items if it.verdict == "FLAG")
            progress(f"🔍 '{axis['title']}' 검토를 마쳤습니다 ({i}/{len(axes)}) — "
                     + ("이상 없습니다 ✓" if n_flag == 0 else f"검토 의견 후보 {n_flag}건 🚩"))
            for it in result.items:  # 발견 위치·문항 즉시 보고("여기서 찾았습니다")
                if it.verdict == "FLAG":
                    progress(f"   ↳ [{it.item_id}] 여기서 찾았습니다 — {it.evidence[:80]}")

        return report

    # ── 서면검토 (자가점검 + 검증2차 + 탐색 + 의견서 초안) ──────

    def _fetch_refs(self, refs: list[str]) -> tuple[str, list[str]]:
        """참조 조문들을 조회 → (발췌 텍스트, 실존 확인된 refs). 1차 결정론 검증 겸용."""
        blocks, existing = [], []
        for ref in refs:
            try:
                art = self.law.fetch_ref(ref)
                blocks.append(f"[{ref}] {art.law_name} {art.article}\n{art.text}")
                existing.append(ref)
            except Exception:
                continue
        return "\n\n".join(blocks), existing

    def _search_context(self, biz_type: str) -> list[LawSearchHit]:
        """탐색 레인(선택적) — 관련 자치법규·행정규칙·판례 후보. 실패 시 빈 리스트."""
        if not self.law_search.enabled:
            return []
        hits: list[LawSearchHit] = []
        # 실측(2026-07-14): 의성군 자치법규에는 '일상감사' 명칭이 없음(0건) — 근거는
        # 「의성군 자체감사 규칙」(§22 일상감사, §36 보칙 위임)이므로 '자체감사'로 검색
        hits += self.law_search.search_ordinance("자체감사")
        hits += self.law_search.search_admrule("일상감사 실시")
        hits += self.law_search.search_precedent(f"{biz_type} 계약 감사")
        # 제목 기준 중복 제거(순서 유지)
        seen, uniq = set(), []
        for h in hits:
            if h.title not in seen:
                seen.add(h.title)
                uniq.append(h)
        return uniq

    def _item_refs_question(self, item_id: str) -> tuple[list[str], str]:
        """항목 정의 조회 — 루브릭 우선, 없으면 계약방법 오버레이(M축)."""
        refs = self.rubric.item_law_refs(item_id) or self.overlay.item_law_refs(item_id)
        question = self.rubric.item_question(item_id) or self.overlay.item_question(item_id)
        return refs, question

    def written_review(
        self,
        biz_type: str,
        doc_text: str,
        progress: ProgressFn | None = None,
        doc_profile: DocTypeResult | None = None,
        contract_method: str | None = None,
        doc_parts: list[DocPart] | None = None,
        llm_doc_text: str | None = None,
        should_stop=None,
    ) -> WrittenReview:
        """서면검토 DAG: 축별검토 → 1차(조문실존·산식) → 2차(문맥) → 인용 태그 규율
        → 실익 게이트 → 탐색 → 의견서 초안 + 종결구 제안.

        검증은 강등만 한다 — 1·2차는 지적을 '확인 필요'로 내릴 수만 있고, 없던 지적을
        만들거나 결정론 판정을 번복하지 않는다(flag-only). 산식 불일치는 결정론이라
        의견서와 별개로 항상 표시된다. 실익 게이트 제외분도 사유와 함께 보존된다.
        """
        progress = progress or (lambda _: None)
        report = self.self_check(
            biz_type, doc_text, progress=progress, doc_profile=doc_profile,
            contract_method=contract_method, doc_parts=doc_parts,
            llm_doc_text=llm_doc_text, should_stop=should_stop,
        )
        wr = WrittenReview(report=report)
        if not report.axis_results and not report.method_incompat:
            return wr

        confirmed: list[Finding] = []
        n_flags = len(report.flags())
        if n_flags:
            progress(f"⚖️ 검증 에이전트가 지적후보 {n_flags}건의 인용 조문을 한 번에 대조하고 있습니다(실존 → 문맥 → 태그)…")

        # 1차(결정론) + 태그 규율을 먼저 통과시킨 후보만 모아 2차(LLM 문맥)를
        # 전체 1콜로 판정한다(2026-07-15 성능 규율 — 후보당 1콜 폐지).
        pending: list[dict] = []   # 2차 검증 대상(문맥검증용 메타 포함)
        for axis, it in report.flags():
            refs, question = self._item_refs_question(it.item_id)
            law_text, existing = self._fetch_refs(refs)

            # 1차(결정론): 조문을 인용했는데 하나도 실존하지 않으면 강등.
            if refs and not existing:
                wr.needs_review.append((it.item_id, "인용 조문 실존 확인 불가(1차)"))
                continue
            # 인용 태그 규율(SOP 제2부): 결론 근거로 쓸 [직접적용] 규정이 없으면 강등.
            demote = self.tags.demotion_reason(existing)
            if demote:
                wr.needs_review.append((it.item_id, demote))
                continue
            law_texts = {}
            if existing:
                for block in law_text.split("\n\n"):
                    if block.startswith("["):
                        ref = block[1:block.index("]")]
                        law_texts[ref] = block[block.index("]") + 1:].strip()
            pending.append({
                "axis": axis, "item": it, "question": question,
                "law_text": law_text, "existing": existing,
                "item_id": it.item_id, "evidence": it.evidence,
                "law_texts": law_texts,
            })

        # 2차(LLM 문맥, 일괄 1콜): supports=False만 강등. 시간 예산 초과 시 검증을
        # 생략하고 지적을 유지한다(강등 전용 원칙 — 미검증은 사람 몫으로 표시).
        checks: list[ContextCheck] = []
        if pending and any(p["existing"] for p in pending):
            if should_stop and should_stop():
                checks = [ContextCheck(item_id=p["item_id"], supports=True,
                                       reason="시간 예산 초과 — 문맥검증 생략(지적 유지, 사람 확인)")
                          for p in pending]
            else:
                checks = self.context_verifier.check_all(pending)
        by_id = {c.item_id: c for c in checks}
        for p in pending:
            cc = by_id.get(p["item_id"])
            if cc is not None and p["existing"]:
                wr.context_checks.append(cc)
                if not cc.supports:
                    wr.needs_review.append((p["item_id"], f"조문-지적 문맥 부적합(2차): {cc.reason}"))
                    continue
            it = p["item"]
            confirmed.append(
                Finding(
                    item_id=it.item_id,
                    axis=p["axis"],
                    question=p["question"],
                    evidence=it.evidence,
                    severity=it.severity,
                    law_refs=p["existing"],
                    law_text=p["law_text"],
                    ref_tags=self.tags.classify_all(p["existing"]),
                )
            )
        progress(f"⚖️ 검증을 마쳤습니다 — 지적 {len(confirmed)}건 유지, {len(wr.needs_review)}건은 '확인 필요'로 내렸습니다")

        # 실익 게이트(SOP ⑥) — 본문 제외 + 말미 목록화. 결정론, 사유 보존.
        confirmed, wr.merit_excluded = self.merit_gate.apply(confirmed, doc_text)
        if wr.merit_excluded:
            progress(f"⚖️ 실익 게이트가 {len(wr.merit_excluded)}건을 본문에서 덜어냈습니다(말미에 사유 보존)")
        wr.confirmed = confirmed

        # 종결구 3단 제안(SOP ⑦) — 기본값은 한 단계 낮춤, 확정은 담당자.
        wr.closings = [suggest(f.item_id, f.severity) for f in confirmed]
        wr.closings += [
            suggest(f"산식#{i}", 3, deterministic=True)
            for i, _c in enumerate(report.numeric_flags, 1)
        ]
        wr.closings += [
            suggest(f"대조#{i}", 3, deterministic=True)
            for i, _c in enumerate(report.cross_flags, 1)
        ]
        # 인용·참조 법령 전체 태그(푸터 표기용)
        wr.ref_tags = self.tags.classify_all(sorted(set(report.law_context_used)))

        wr.search_hits = self._search_context(biz_type)
        if wr.search_hits:
            progress(f"📚 관련 규정·판례 후보 {len(wr.search_hits)}건을 더 찾아뒀습니다(참고용)")
        elif self.law_search.enabled:
            progress("📚 추가 규정·판례 탐색 결과는 없었습니다")

        numeric_notes = [
            f"{c.expr} → 재계산 {c.expected:,} vs 기재 {c.claimed:,}" for c in report.numeric_flags
        ]
        rule_notes = [report.method_incompat] if report.method_incompat else []
        rule_notes += [c.note for c in report.cross_flags]

        # AI 검토 의견이 없으면 무거운 합성 호출을 생략(2026-07-15) — 자동 확인
        # 사항(산식·대조·계약방법)만으로 결정론 초안을 조립한다. 자동 확인 사항이
        # 있으면 '지적사항 없음'이라고 쓰지 않는다(모순 수정).
        if not confirmed:
            det_issues = ([f"산식 불일치 — {n}" for n in numeric_notes]
                          + [f"자동 확인 — {n}" for n in rule_notes])
            if det_issues:
                overall = (f"AI 검토 의견은 없으나 자동 확인 사항 {len(det_issues)}건이 "
                           f"발견되어 보완이 필요함.")
                recs = ["자동 확인 사항의 기재 내용을 확인·정정한 뒤 재제출"]
                progress("📝 AI 검토 의견이 없어 합성을 생략하고 자동 확인 사항만 정리했습니다")
            else:
                overall = "검토 결과 확인된 문제가 없음(자동 확인·AI 검토 모두)."
                recs = []
                progress("📝 확인된 문제가 없어 의견서 합성을 생략했습니다")
            from audit_core.agents.schemas import OpinionIssue
            wr.opinion = OpinionDraft(
                query=f"{biz_type} 사업 계약·집행의 적정성 서면검토",
                facts="문서에서 확인된 사실을 전제로 한다. 세부 사실관계는 원 문서를 따른다.",
                issues=[OpinionIssue(title=d.split(" — ", 1)[0], issue=d,
                                     rule="자동 확인 사항(코드 검산·대조 — 사람 판단 아님)",
                                     application=d.split(" — ", 1)[-1],
                                     conclusion="기재 내용을 확인·정정할 필요가 있음",
                                     certainty="명확") for d in det_issues],
                overall=overall, recommendations=recs,
            )
            return wr

        over_budget = bool(should_stop and should_stop())
        progress("📝 의견서 에이전트가 검증 통과분으로 초안을 쓰고 있습니다…" if not over_budget
                 else "📝 시간 예산을 넘겨 의견서는 자동 초안(결정론)으로 정리합니다")
        wr.opinion = self.synthesizer.draft(
            biz_type, confirmed, numeric_notes, wr.search_hits, rule_notes=rule_notes,
            skip_llm=over_budget,
        )
        return wr


def format_self_check(report: ReviewReport) -> str:
    """자가점검 리포트 → 사용자 표시 텍스트. 화면 고지 포함(자동화 편향 대응)."""
    lines = ["## 자가점검 결과", ""]
    if report.tier2:
        lines.append(f"> ⚠ 이 분야는 유사 검토 사례가 부족해 **보수적으로 판단했습니다** — 확인 중심으로 읽어주세요.\n")
    if report.provisional_rubric:
        lines.append("> ⚠ 점검 기준은 협의 전 초안이며, 기준금액은 의성군 별표(감사팀 확인 전) 기반입니다. 실제 감사의견과 다를 수 있습니다.\n")

    flags = report.flags()
    if report.method_incompat:
        lines.append("### ⚖️ 계약방법 자동 확인")
        lines.append(f"- {report.method_incompat}")
        lines.append("")
    if report.numeric_flags:
        lines.append("### 🔢 산식 불일치 (자동 검산)")
        for c in report.numeric_flags:
            lines.append(f"- `{c.expr}` → 재계산 **{c.expected:,}** vs 기재 **{c.claimed:,}**")
        lines.append("")
    if report.cross_flags:
        lines.append("### 🔎 문서 간 불일치 (자동 대조)")
        for cf in report.cross_flags:
            lines.append(f"- **[{cf.kind}]** {cf.note}")
        lines.append("")

    if flags:
        lines.append(f"### 🚩 검토 의견 후보 {len(flags)}건 (심각도순)")
        for axis, it in flags:
            sev = "●" * it.severity
            lines.append(f"- **[{it.item_id}]** {sev} {it.evidence}")
        lines.append("")
    else:
        lines.append("### 검토 의견: 없음\n")

    unable = report.unable()
    if unable:
        total_items = sum(len(ar.items) for ar in report.axis_results)
        lines.append(f"### ❔ 판단 불가 {len(unable)}건 (담당자 확인 필요)")
        # 대부분이 판단 불가면 나열 대신 원인 요약(제출 서류 부족·문서 불일치가 원인)
        if total_items and len(unable) >= max(8, int(total_items * 0.7)):
            lines.append(
                f"> 점검 항목 {total_items}개 중 {len(unable)}개를 이 문서만으로는 판단할 수 "
                f"없었습니다. 대개 **문서가 검토 대상 서류가 아니거나, 판단에 필요한 서류"
                f"(산출내역서·사업계획서 등)가 빠진 경우**입니다 — 위 '서류 완결성 확인'의 "
                f"누락 목록을 보완해 다시 올려주세요. (항목별 상세는 생략)")
            lines.append("")
        else:
            for axis, it in unable:
                lines.append(f"- [{it.item_id}] {it.evidence}")
            lines.append("")

    if report.skipped_axes:
        lines.append(f"### 자료 부족 등으로 확인하지 못한 항목: {', '.join(n for n, _ in report.skipped_axes)}")
        lines.append(f"> {report.skipped_axes[0][1]}\n")

    if report.law_context_used:
        uniq = sorted(set(report.law_context_used))
        lines.append(f"*참조 법령: {', '.join(uniq)}*")
    lines.append("\n*예상 검토항목이며 실제 감사의견과 다를 수 있습니다.*")
    return "\n".join(lines)


_CERTAINTY_MARK = {"명확": "●●●", "높음": "●●○", "보통": "●○○", "낮음": "○○○"}




def format_user_summary(wr: WrittenReview, missing_docs: list[str] | None = None) -> str:
    """결과 최상단 요약(2026-07-15 화면 단순화) — 결론 → 건수 → 먼저 조치할 사항.

    처음 보는 공무원이 결론과 다음 행동을 바로 이해하도록, 내부 용어 없이 쓴다.
    상세 근거·법령은 아래 상세 영역이 담당한다(같은 문제를 반복 서술하지 않도록
    여기서는 상위 3건까지만).
    """
    report = wr.report
    missing_docs = missing_docs or []
    n_op = len(wr.confirmed)
    n_auto = len(report.numeric_flags) + len(report.cross_flags) + (1 if report.method_incompat else 0)
    n_check = len(wr.needs_review) + len(report.unable())

    if n_op or n_auto or missing_docs:
        verdict = "보완 후 진행 권고"
    elif n_check:
        verdict = "담당자 확인 필요"
    else:
        verdict = "진행 가능"
    lines = [f"## 검토 결과: {verdict}", ""]
    counts = []
    if n_op: counts.append(f"검토 의견 {n_op}건")
    if n_auto: counts.append(f"자동 확인 사항 {n_auto}건")
    if n_check: counts.append(f"확인 필요 {n_check}건")
    if missing_docs: counts.append(f"누락 서류 {len(missing_docs)}건")
    lines.append(("**" + " · ".join(counts) + "**이 있습니다.") if counts
                 else "확인된 문제가 없습니다. 아래 상세 근거를 참고해 다음 절차를 진행하세요.")

    actions = []
    for c in report.numeric_flags[:2]:
        actions.append(("산식 금액이 서로 맞지 않습니다",
                        f"확인 내용: `{c.expr}` → 재계산 {c.expected:,}원 vs 기재 {c.claimed:,}원",
                        "다음 조치: 산출내역을 수정하고 총액을 다시 맞춰 주세요."))
    for cf in report.cross_flags[:2]:
        actions.append((f"{cf.kind}이(가) 문서마다 다르게 기재되어 있습니다",
                        f"확인 내용: {cf.note[:120]}",
                        "다음 조치: 어느 쪽이 맞는지 확인해 문서를 일치시켜 주세요."))
    if report.method_incompat:
        actions.append(("계약방법이 이 사업 유형에 맞지 않습니다",
                        f"확인 내용: {report.method_incompat[:120]}",
                        "다음 조치: 계약방법 선택 근거를 재검토해 주세요."))
    closing_by_id = {c.item_id: c.proposal for c in wr.closings}
    for f in wr.confirmed[:3]:
        actions.append((f.question[:60],
                        f"확인 내용: {f.evidence[:100]}",
                        f"다음 조치: 관련 근거를 보완해 주세요 (제안 강도: {closing_by_id.get(f.item_id, '검토')})"))
    if actions:
        lines.append("\n### 먼저 조치할 사항")
        for i, (title, detail, todo) in enumerate(actions[:5], 1):
            lines += [f"\n{i}. **{title}**", f"   - {detail}", f"   - {todo}"]

    if missing_docs:
        lines.append("\n### 추가로 필요한 서류")
        lines += [f"- {d}" for d in missing_docs]
    unable = report.unable()
    if unable or report.skipped_axes:
        lines.append("\n### 확인하지 못한 사항")
        for _a, it in unable[:3]:
            lines.append(f"- {it.evidence[:80]}")
        extra = len(unable) - 3
        if extra > 0:
            lines.append(f"- …외 {extra}건 (아래 상세 참고)")
        if report.skipped_axes:
            lines.append(f"- 자료가 없어 살펴보지 않은 영역 {len(report.skipped_axes)}곳 (필요 서류 첨부 시 재검토)")
    lines.append("\n### 상세 근거와 참고 법령은 아래에 있습니다.")
    lines.append("---")
    return "\n".join(lines)


def format_written_review(wr: WrittenReview) -> str:
    """서면검토 → 의견서 초안 텍스트. 면책·경고·'AI 초안' 고지는 여기서 결정론적으로
    부가하므로 LLM이 누락할 수 없다. 점수·등급은 표시하지 않는다(제약 B2)."""
    report, op = wr.report, wr.opinion
    lines: list[str] = []

    # ── 결정론 고지(항상, 최상단) ──
    lines.append("## 일상감사 의견서 (초안)")
    if report.tier2:
        lines.append(f"> ⚠ 이 분야는 유사 검토 사례가 부족해 보수적으로 판단했습니다 — 의견 강도는 담당자가 확정하세요.")
    lines.append("> ⚠ **AI가 생성한 검토 초안입니다. 확정 감사의견이 아니며, 감사인의 검토·판단이 필요합니다.**")
    if report.provisional_rubric:
        lines.append("> ⚠ 점검 기준은 협의 전 초안이며, 기준금액은 의성군 별표(감사팀 확인 전) 기반입니다.")
    lines.append("")

    if op is None:
        lines.append("_의견서 초안을 생성하지 못했습니다(활성 축 없음 또는 검토 불가)._")
        return "\n".join(lines)

    # 결정론 지적(계약방법-분야 모순) — LLM 산출과 무관하게 항상 표시
    if report.method_incompat:
        lines.append("**[자동 확인 — 계약방법]**")
        lines.append(f"- {report.method_incompat}")
        lines.append("")

    lines.append(f"**Ⅰ. 질의 요지** — {op.query}\n")
    lines.append(f"**Ⅱ. 사실관계**\n{op.facts}\n")

    lines.append("**Ⅲ. 검토 의견**")
    if op.issues:
        for i, iss in enumerate(op.issues, 1):
            mark = _CERTAINTY_MARK.get(iss.certainty, "○○○")
            lines.append(f"\n**{i}. {iss.title}**  _(확실성 {mark} {iss.certainty})_")
            lines.append(f"- 쟁점: {iss.issue}")
            lines.append(f"- 근거: {iss.rule}")
            lines.append(f"- 검토: {iss.application}")
            lines.append(f"- 소결: {iss.conclusion}")
    else:
        lines.append("\n검토 결과 자동 지적사항은 없습니다.")
    lines.append("")

    lines.append(f"**Ⅳ. 종합 의견**\n{op.overall}\n")

    if op.recommendations:
        lines.append("**Ⅴ. 권고 사항**")
        for r in op.recommendations:
            lines.append(f"- {r}")
        lines.append("")

    # ── 종결구 3단 제안(SOP ⑦) — 기본값은 한 단계 낮춤, 확정은 담당자(P1) ──
    if wr.closings:
        lines.append("**[종결구 제안 — 확정은 감사담당자]**")
        for c in wr.closings:
            up = f" (후보: '{c.candidate}')" if c.candidate != c.proposal else ""
            lines.append(f"- [{c.item_id}] **'{c.proposal}'**{up} — {c.reason}")
        lines.append("")

    # ── 금칙어 점검(코드 검증, flag-only) — 명백성 판단은 담당자 몫 ──
    _op_text = "\n".join(
        [op.overall] + [f"{i.rule} {i.application} {i.conclusion}" for i in op.issues]
        + op.recommendations
    )
    forbidden = scan_forbidden(_op_text)
    if forbidden:
        lines.append(f"**[🚧 금칙어 점검 — {len(forbidden)}건 검출(명백한 기준 저촉인지 확인 후 유지·순화)]**")
        for h in forbidden:
            lines.append(f"- '{h.term}' ({h.rule}): {h.excerpt}")
        lines.append("")

    # ── 결정론 부가 정보(의견서 본문과 분리) ──
    if report.numeric_flags:
        lines.append("**[자동 확인 — 산식 검산]**")
        for c in report.numeric_flags:
            lines.append(f"- `{c.expr}` → 재계산 **{c.expected:,}** vs 기재 **{c.claimed:,}**")
        lines.append("")

    if report.cross_flags:
        lines.append("**[자동 확인 — 문서 간 대조]**")
        for cf in report.cross_flags:
            lines.append(f"- **[{cf.kind}]** {cf.note}")
        lines.append("")

    if wr.needs_review:
        lines.append(f"**[⚠ 확인 필요 — 자동 인용에서 제외된 {len(wr.needs_review)}건]**")
        for item_id, reason in wr.needs_review:
            lines.append(f"- [{item_id}] {reason}")
        lines.append("")

    unable = report.unable()
    if unable:
        lines.append(f"**[❔ 판단 불가 {len(unable)}건 — 담당자 확인]**")
        for _axis, it in unable:
            lines.append(f"- [{it.item_id}] {it.evidence}")
        lines.append("")

    if report.skipped_axes:
        lines.append(f"**[자료 부족 등으로 확인하지 못한 항목]** {', '.join(n for n, _ in report.skipped_axes)}")
        lines.append(f"_{report.skipped_axes[0][1]}_")
        lines.append("")

    # ── 실익 게이트 제외분(SOP ⑥) — 본문 제외 사유를 말미에 보존(게이트 검증용) ──
    if wr.merit_excluded:
        for line in format_exclusions(wr.merit_excluded):
            lines.append(line)
        lines.append("")

    if report.law_context_used:
        if wr.ref_tags:
            lines.append(f"*인용·참조 법령: {format_tagged_refs(wr.ref_tags)}*")
        else:
            uniq = sorted(set(report.law_context_used))
            lines.append(f"*인용·참조 법령: {', '.join(uniq)}*")
    if wr.search_hits:
        lines.append("*관련 규정·판례 후보(탐색, 미검증 — 인용 전 확인 요망):*")
        for h in wr.search_hits:
            lines.append(f"  - ({h.target}) {h.title} {h.ref}".rstrip())

    lines.append("\n---")
    lines.append("*본 초안은 국가법령정보 API로 조문 실존을 검증했으나, 항·호 수준 정합과 "
                 "최종 판단은 감사인 몫입니다. 확정 감사의견을 대체하지 않습니다.*")
    return "\n".join(lines)
