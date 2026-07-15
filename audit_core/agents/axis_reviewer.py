"""공용 축 검토기 (SPEC §1-2). 축 정의는 rubric JSON에서 주입 — 코드 무수정 개정.

담당 축의 점검항목만 검토하여 항목별 [NA/OK/FLAG/UNABLE] + 근거를 낸다.
좁은 컨텍스트(해당 축 항목 + 관련 문서 조각)만 받는 소형 에이전트 — 로컬 LLM의
긴 복합 과업 정확도 급락을 보상하는 멀티에이전트 설계(DESIGN.md 3.3).
"""

import json
from pathlib import Path

from audit_core.agents.base import LLMUnavailable, OllamaClient, SchemaValidationError
from audit_core.agents.schemas import AxisItemResult, AxisResult
from audit_core.config import get_settings

SYSTEM = (
    "너는 지방자치단체 일상감사의 서면검토 보조자다. 주어진 '점검항목'만을 기준으로 "
    "'문서'를 검토한다. 추측하지 말고 문서에 드러난 사실만으로 판정한다.\n"
    "각 항목마다 verdict를 판정한다:\n"
    "- NA: 이 사업 유형에 해당 없음\n"
    "- OK: 문서에서 요건 충족을 확인함\n"
    "- FLAG: 요건 미충족·미비·근거 부족 (지적후보)\n"
    "- UNABLE: 문서만으로 판단 불가\n"
    "evidence에는 그렇게 판정한 근거를 문서 내용을 인용해 한 문장으로 적는다. "
    "severity는 FLAG일 때만 1~3(3이 가장 중대), 그 외에는 1."
)


class AxisReviewer:
    # 조각 검토(doc_sectioner v1, SPEC §1-7) — 문서가 이 길이를 넘으면 줄 경계로
    # 분할해 축별 순차 검토 후 항목별 병합한다. 값은 qwen3 num_ctx 16K 기준
    # 안전율(항목·법령 블록·응답 여유 감안). 실측 병목: RFP 72,310자.
    CHUNK_CHARS = 11000

    def __init__(self, client: OllamaClient | None = None, model: str | None = None):
        self.client = client or OllamaClient()
        self.model = model or get_settings().AUDIT_MODEL_REVIEW

    def _prompt(self, axis: dict, doc_text: str, law_context: str) -> str:
        items = "\n".join(
            f"- {it['item_id']}: {it['question']}"
            + (f" (관련법령 {', '.join(it['law_refs'])})" if it.get("law_refs") else "")
            for it in axis["items"]
        )
        law_block = f"\n[관련 법령 발췌]\n{law_context}\n" if law_context else ""
        return (
            f"[검토 축] {axis['axis']}. {axis['title']}\n\n"
            f"[점검항목]\n{items}\n"
            f"{law_block}\n"
            f"[문서]\n{doc_text}\n\n"
            f"위 점검항목 {len(axis['items'])}개 각각에 대해 판정하라. "
            f"item_id는 반드시 위 목록의 것을 그대로 사용한다."
        )

    def _split_chunks(self, doc_text: str) -> list[str]:
        """줄 경계 분할 — 조각당 CHUNK_CHARS 이하."""
        chunks, cur, size = [], [], 0
        for ln in doc_text.splitlines():
            if size + len(ln) > self.CHUNK_CHARS and cur:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(ln)
            size += len(ln) + 1
        if cur:
            chunks.append("\n".join(cur))
        return chunks

    @staticmethod
    def _merge_chunk_results(axis: dict, results: list[AxisResult]) -> AxisResult:
        """조각별 판정을 항목 단위로 병합.

        우선순위 OK > FLAG > UNABLE > NA — 조각에는 정보가 없어서 '미비'로 보이는
        부재형 오탐(F9)을 억제한다: 어느 조각에서든 충족이 확인되면 OK.
        (모순·불일치 같은 존재형 결함은 결정론 레인(산식·문서 간 대조)이 전체
        문서로 잡으므로 여기서 잃지 않는다.) FLAG는 최고 severity 조각을 채택.
        """
        rank = {"OK": 3, "FLAG": 2, "UNABLE": 1, "NA": 0}
        merged: list[AxisItemResult] = []
        for it in axis["items"]:
            cands = [r for res in results for r in res.items if r.item_id == it["item_id"]]
            if not cands:
                merged.append(AxisItemResult(item_id=it["item_id"], verdict="UNABLE",
                                             evidence="조각 검토 누락"))
                continue
            best = max(cands, key=lambda c: (rank.get(c.verdict, 0),
                                             c.severity if c.verdict == "FLAG" else 0))
            if best.verdict == "FLAG" and len(results) > 1:
                best = AxisItemResult(item_id=best.item_id, verdict="FLAG",
                                      evidence=f"{best.evidence} (조각 검토 — 문서 일부 기준)",
                                      severity=best.severity)
            merged.append(best)
        return AxisResult(axis=axis["axis"], items=merged)

    def review_all(self, axes: list[dict], doc_text: str, law_context: str = "",
                   should_stop=None) -> list[AxisResult]:
        """활성 축 전체를 **한 번의 구조화 호출**로 검토(2026-07-15 처리시간 개선).

        기존: 축마다 호출 → 같은 본문을 N번 전송. 개선: 항목 전체를 1콜.
        축별 결과 스키마(AxisResult)와 항목 추적성은 회신을 재그룹해 유지한다.
        긴 문서는 조각 순차 호출 후 항목 병합(기존 doc_sectioner 의미론).
        should_stop()이 참이 되면 남은 조각을 중단하고 지금까지 결과로 병합한다.
        """
        item_axis = {it["item_id"]: a["axis"] for a in axes for it in a["items"]}
        listing = []
        for a in axes:
            listing.append(f"[축 {a['axis']}. {a['title']}]")
            for it in a["items"]:
                refs = f" (관련법령 {', '.join(it['law_refs'])})" if it.get("law_refs") else ""
                listing.append(f"- {it['item_id']}: {it['question']}{refs}")
        items_txt = "\n".join(listing)
        law_block = f"\n[관련 법령 발췌]\n{law_context}\n" if law_context else ""

        # 전 항목 회신 강제(2026-07-15 실측 개선): 통합 1콜에서 모델이 일부
        # 항목만 내고 멈추는 현상(650자 회신 → 대부분 '판정 미회신') → 스키마에
        # minItems를 동적으로 걸어 제약 디코딩·검증 재시도(1회)가 전 항목을 요구.
        from pydantic import Field, create_model
        n_items = len(item_axis)
        BatchSchema = create_model(
            "AxisResult",  # 이름 유지 — 테스트 페이크가 schema.__name__으로 분기
            axis=(str, ...),
            items=(list[AxisItemResult], Field(min_length=n_items)),
        )

        def one(chunk: str, tag: str) -> list[AxisItemResult]:
            prompt = (f"[점검항목 — 축별로 묶여 있음, 전 항목 판정 필수]\n{items_txt}\n"
                      f"{law_block}\n[문서{tag}]\n{chunk}\n\n"
                      f"위 점검항목 {n_items}개 **전부**에 대해 items에 하나씩 판정을 "
                      f"넣어라(총 {n_items}개 — 빠뜨린 항목은 오답 처리). "
                      f"item_id는 반드시 위 목록의 것을 그대로 사용한다. "
                      f"문서에 관련 내용이 없으면 verdict를 UNABLE로 한다.")
            try:
                r = self.client.chat_json(model=self.model, system=SYSTEM, prompt=prompt,
                                          schema=BatchSchema, num_predict=4096,
                                          stage="review",
                                          timeout_s=get_settings().AUDIT_TIMEOUT_REVIEW_S)
                return [it for it in r.items if it.item_id in item_axis]
            except (SchemaValidationError, LLMUnavailable):
                # 타임아웃·장애는 재시도하지 않는다(성능 규율) — 이 조각의 항목은
                # 병합 단계에서 UNABLE('판정 미회신')로 남아 부분 결과로 반환된다.
                return []

        chunks = self._split_chunks(doc_text) if len(doc_text) > self.CHUNK_CHARS else [doc_text]
        per_chunk: list[list[AxisItemResult]] = []
        for i, c in enumerate(chunks, 1):
            if should_stop and should_stop():
                break
            tag = f" 조각 {i}/{len(chunks)} — 전체의 일부" if len(chunks) > 1 else ""
            per_chunk.append(one(c, tag))

        # 항목 병합(OK>FLAG>UNABLE>NA — 조각 의미론과 동일) 후 축별 재그룹
        rank = {"OK": 3, "FLAG": 2, "UNABLE": 1, "NA": 0}
        merged: dict[str, AxisItemResult] = {}
        for batch in per_chunk:
            for it in batch:
                cur = merged.get(it.item_id)
                if cur is None or (rank.get(it.verdict, 0), it.severity if it.verdict == "FLAG" else 0)                         > (rank.get(cur.verdict, 0), cur.severity if cur.verdict == "FLAG" else 0):
                    merged[it.item_id] = it
        results = []
        for a in axes:
            items = [merged.get(it["item_id"],
                                AxisItemResult(item_id=it["item_id"], verdict="UNABLE",
                                               evidence="판정 미회신(시간 제한·중단 포함) — 확인 필요"))
                     for it in a["items"]]
            results.append(AxisResult(axis=a["axis"], items=items))
        return results

    def review(self, axis: dict, doc_text: str, law_context: str = "") -> AxisResult:
        """축 1개 검토. LLM 스키마 검증 실패 시 전 항목 UNABLE로 폴백(파이프 중단 방지).

        문서가 CHUNK_CHARS를 넘으면 조각으로 나눠 순차 검토 후 병합한다
        (doc_sectioner v1 — 16K 컨텍스트 초과 실파일 대응)."""
        if len(doc_text) > self.CHUNK_CHARS:
            chunks = self._split_chunks(doc_text)
            results = []
            for i, chunk in enumerate(chunks, 1):
                header = f"[문서 조각 {i}/{len(chunks)} — 전체의 일부만 보고 있음]\n"
                results.append(self._review_once(axis, header + chunk, law_context))
            return self._merge_chunk_results(axis, results)
        return self._review_once(axis, doc_text, law_context)

    def _review_once(self, axis: dict, doc_text: str, law_context: str = "") -> AxisResult:
        try:
            result = self.client.chat_json(
                model=self.model,
                system=SYSTEM,
                prompt=self._prompt(axis, doc_text, law_context),
                schema=AxisResult,
                num_predict=2048,
            )
        except SchemaValidationError:
            return self._unable(axis, "LLM 응답 스키마 검증 실패")

        # LLM이 임의 item_id를 만들거나 누락하는 경우를 루브릭 기준으로 보정
        valid_ids = {it["item_id"] for it in axis["items"]}
        by_id = {r.item_id: r for r in result.items if r.item_id in valid_ids}
        items = [
            by_id.get(
                it["item_id"],
                AxisItemResult(item_id=it["item_id"], verdict="UNABLE", evidence="LLM이 판정을 누락함"),
            )
            for it in axis["items"]
        ]
        return AxisResult(axis=axis["axis"], items=items)

    def _unable(self, axis: dict, reason: str) -> AxisResult:
        return AxisResult(
            axis=axis["axis"],
            items=[
                AxisItemResult(item_id=it["item_id"], verdict="UNABLE", evidence=reason)
                for it in axis["items"]
            ],
        )


class Rubric:
    """루브릭 로딩 + 사업유형별 활성 축 선별 (오케스트레이터가 축 스킵에 사용)."""

    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().RUBRIC_PATH)
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.provisional = self.data.get("provisional", True)
        # Tier 2 = 자체 사례 미보유 분야(설계서 커버리지 티어) — 라벨 강제, 확인 중심
        self.tier2: set[str] = set(self.data.get("tier2", []))
        self._items: dict[str, dict] = {
            it["item_id"]: it for axis in self.data["axes"] for it in axis["items"]
        }

    def item(self, item_id: str) -> dict:
        """점검항목 정의(question·law_refs·weight)를 item_id로 조회."""
        return self._items.get(item_id, {})

    def item_law_refs(self, item_id: str) -> list[str]:
        return self._items.get(item_id, {}).get("law_refs", [])

    def item_question(self, item_id: str) -> str:
        return self._items.get(item_id, {}).get("question", "")

    def active_axes(self, biz_type: str) -> list[dict]:
        """해당 사업유형에 적용되는 축만. 축 내 항목도 applies_to로 2차 필터."""
        out = []
        for axis in self.data["axes"]:
            if biz_type not in axis.get("applies_to", []):
                continue
            items = [
                it for it in axis["items"]
                if "applies_to" not in it or biz_type in it["applies_to"]
            ]
            if items:
                out.append({**axis, "items": items})
        return out

    def all_law_refs(self, axes: list[dict]) -> list[str]:
        refs = {r for axis in axes for it in axis["items"] for r in it.get("law_refs", [])}
        return sorted(refs)


class ContractMethodOverlay:
    """계약방법 오버레이 — 분야가 아니라 활성 축 위에 '겹치는' 공통 체크리스트
    (마스터 SOP 제3부 공통모듈, 설계서 §4 매트릭스). 모듈 정의는
    overlay_contract_method.json 데이터로 외부화 — 코드 무수정 개정.
    """

    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().OVERLAY_PATH)
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.provisional = self.data.get("provisional", True)
        self.modules: dict[str, dict] = self.data.get("modules", {})
        self._items: dict[str, dict] = {
            it["item_id"]: it for mod in self.modules.values() for it in mod.get("items", [])
        }

    def item_law_refs(self, item_id: str) -> list[str]:
        return self._items.get(item_id, {}).get("law_refs", [])

    def item_question(self, item_id: str) -> str:
        return self._items.get(item_id, {}).get("question", "")

    def module_for(self, method: str | None) -> dict | None:
        """계약방법 문자열 → 해당 모듈(키워드 부분일치). 없으면 None."""
        if not method:
            return None
        m = method.replace(" ", "")
        for mod in self.modules.values():
            if any(kw in m for kw in mod.get("method_keywords", [])):
                return mod
        return None

    def incompatibility(self, method: str | None, biz_group: str) -> str | None:
        """분야-계약방법 자체 모순(예: 공사+협상계약) — 결정론 지적 사유.

        allowed_biz가 선언된 모듈만 검사하고, 미선언(null)은 전 분야 허용.
        보수 원칙: biz_group이 목록에 '명시적으로 없을 때'만 지적한다.
        """
        mod = self.module_for(method)
        if not mod:
            return None
        allowed = mod.get("allowed_biz")
        if allowed is not None and biz_group not in allowed:
            return mod.get("incompatible_note") or (
                f"{mod['title']} — '{biz_group}' 분야에는 적용할 수 없는 계약방법"
            )
        return None

    def overlay_axes(self, method: str | None, biz_group: str) -> list[dict]:
        """활성 축에 겹칠 오버레이 축(0~1개). 분야 모순 건은 축 검토 대신
        incompatibility()의 결정론 지적으로 처리하므로 여기서는 제외한다."""
        mod = self.module_for(method)
        if not mod or self.incompatibility(method, biz_group):
            return []
        return [{
            "axis": mod["axis"],
            "title": mod["title"],
            "items": mod["items"],
        }]
