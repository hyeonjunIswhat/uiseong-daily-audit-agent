"""
title: 일상감사 멀티 에이전트
author: 의성군 AI데이터팀
version: 0.1.0
required_open_webui_version: 0.9.0
description: 일상감사 대상판정·자가점검·서면검토(의견서 초안). 진행 status·법령 인용 칩(citation) 지원 — Pipelines 버전의 in-app Function 이관본 (REBUILD 회차 1.5, UI_UX.md)
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field

# audit_core 반입 경로(도커 볼륨) — daily_audit_pipe.py(레거시 어댑터)와 .env 동거
AUDIT_HOME = os.getenv("AUDIT_CORE_HOME", "/app/backend/data/daily_audit")
if AUDIT_HOME not in sys.path:
    sys.path.insert(0, AUDIT_HOME)

_DEPLOY_STAMP = {"value": None}


def _agent_log(msg: str):
    """실행 로그(운영자용 tail 대상) — AUDIT_TRAIL_LEVEL=code_only 원칙:
    사용자 원문·문서 내용은 절대 기록하지 않는다(의도·모드·길이·소요·오류만)."""
    import logging
    lg = logging.getLogger("daily_audit")
    if not lg.handlers:
        h = logging.FileHandler(f"{AUDIT_HOME}/agent.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%m-%d %H:%M:%S"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    lg.info(msg)


def _ensure_perf_log():
    """LLM 호출 계측 로그(audit_perf) 핸들러 — 단계·모델·입출력 크기·소요·상태·req id만.
    문서 원문·프롬프트는 base._log_call이 아예 넘기지 않는다(코드 수준 보장)."""
    import logging
    lg = logging.getLogger("audit_perf")
    if not lg.handlers:
        h = logging.FileHandler(f"{AUDIT_HOME}/perf.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%m-%d %H:%M:%S"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False


def _ensure_fresh_modules(home: str):
    """배포 스탬프가 바뀌면 audit_core·daily_audit_pipe 모듈 캐시를 비운다.

    open-webui는 장수 프로세스라 docker cp로 파일을 바꿔도 sys.modules의 구 모듈이
    남는다(실장애 2026-07-15: 구 모듈에 _QA_TRIGGER가 없어 오류). 배포 시
    AUDIT_HOME/DEPLOY_STAMP를 갱신하면 다음 요청에서 전부 재임포트된다."""
    try:
        stamp = (Path(home) / "DEPLOY_STAMP").read_text().strip()
    except OSError:
        stamp = ""
    if _DEPLOY_STAMP["value"] == stamp:
        return
    _DEPLOY_STAMP["value"] = stamp
    for name in [k for k in sys.modules
                 if k == "daily_audit_pipe" or k == "audit_core" or k.startswith("audit_core.")]:
        del sys.modules[name]


def _fix_env_collisions():
    """open-webui 컨테이너의 OLLAMA_BASE_URL='/ollama'(자체 프록시 경로)를
    audit_core 설정이 물려받으면 LLM 호출이 "unknown url type"으로 죽는다
    (배포 검증 실측). 프로세스 env는 건드리지 않고 audit_core 설정 싱글턴만
    교정한다 — open-webui 본체 설정에 무영향."""
    from audit_core.config import get_settings
    s = get_settings()
    if not s.OLLAMA_BASE_URL.startswith("http"):
        s.OLLAMA_BASE_URL = os.getenv(
            "AUDIT_OLLAMA_BASE_URL", "http://host.docker.internal:11434"
        )


def _attachment_paths(files: list | None) -> list[tuple[str, str]]:
    """__files__ 항목 → (표시명, 로컬 경로). Open WebUI 업로드는
    /app/backend/data/uploads/{id}_{filename}에 저장되고 레코드에 path가 있다."""
    import glob as _glob
    out = []
    for e in files or []:
        if not isinstance(e, dict):
            continue
        f = e.get("file") if isinstance(e.get("file"), dict) else {}
        path = f.get("path") or e.get("path")
        fid = f.get("id") or e.get("id")
        name = (f.get("filename") or e.get("name") or e.get("filename") or "첨부파일")
        if not path and fid:
            hits = _glob.glob(f"/app/backend/data/uploads/{fid}_*")
            path = hits[0] if hits else None
        if path and Path(path).is_file():
            out.append((name, path))
    return out


def _parse_one_attachment(name: str, path: str) -> dict:
    """첨부 1건 인입 — 파일별 진행 근거를 화면에 바로 보이기 위한 단건 파서.

    반환: {"kind": "text"|"cost"|"skip", "evidence": 화면용 근거 한 줄, ...}
    evidence는 실제 확인한 내용(파일명·글자수·시트명·검산 결과)만 담는다 — 추상 문구 금지.
    """
    from audit_core.parsers.cost_xlsx import CostSheetError, check_cost_sheet
    from audit_core.parsers.hwp import parse_hwp
    from audit_core.parsers.hwpx import parse_hwpx
    from audit_core.parsers.pdf import PdfParseError, parse_pdf

    ext = Path(path).suffix.lower()
    try:
        if ext in (".hwpx", ".hwp", ".pdf"):
            text = {".hwpx": parse_hwpx, ".hwp": parse_hwp, ".pdf": parse_pdf}[ext](path).text
            first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:40]
            return {"kind": "text", "name": name, "text": text,
                    "evidence": f"📄 [원문 확인] `{name}` — 본문 {len(text):,}자를 읽었습니다"
                                + (f" (첫 줄: 「{first}」)" if first else "")}
        if ext == ".xlsx":
            r = check_cost_sheet(path)
            bad = [c for c in r.checks if not c.match]
            sheet = getattr(r, "sheet", None)
            line = (f"- {name}: 검산 {len(r.checks)}건 중 불일치 {len(bad)}건"
                    + (" — " + "; ".join(c.expr for c in bad[:2]) if bad else ""))
            notes = [f"  · ℹ {n}" for n in r.notes]
            total = next((c for c in r.checks if c.kind == "fp_total"), None)
            return {"kind": "cost", "name": name, "cost_lines": [line] + notes, "total": total,
                    "evidence": f"🧮 [자동 계산] `{name}`" + (f" 시트 '{sheet}'" if sheet else "")
                                + f" — 산식 {len(r.checks)}건을 검산해 불일치 {len(bad)}건을 확인했습니다"
                                + (f" (예: {bad[0].expr[:50]})" if bad else "")}
        return {"kind": "skip", "name": name,
                "note": f"{name} — {ext or '형식 불명'}은 자동 인입 미지원(hwp·hwpx·xlsx·pdf 지원)",
                "evidence": f"⏭ [확인 필요] `{name}` — 지원하지 않는 형식이라 읽지 못했습니다"}
    except PdfParseError as e:
        return {"kind": "skip", "name": name, "note": f"{name} — {e}",
                "evidence": f"⏭ [확인 필요] `{name}` — {e}"}
    except CostSheetError as e:
        return {"kind": "skip", "name": name, "note": f"{name} — 산출내역 서식 인식 불가({e})",
                "evidence": f"⏭ [확인 필요] `{name}` — 산출내역 서식을 인식하지 못했습니다"}
    except Exception as e:
        return {"kind": "skip", "name": name, "note": f"{name} — 읽기 실패({type(e).__name__})",
                "evidence": f"⏭ [확인 필요] `{name}` — 읽기 실패({type(e).__name__})"}


PROGRESS_GAP_S = 15  # 진행 메시지 최대 간격(2026-07-15 UX 규율) — 테스트에서 단축 가능


def _idle_notice(state: dict, llm_timeout_s: int) -> str:
    """15초 무소식일 때 내보낼 안내 줄 — '마지막 확인 근거 + 현재 작업'.

    점 펄스('·')나 '응답 대기 중' 같은 추상 문구 금지. 같은 작업이 이어지면
    동일 문장을 반복하지 않고 대기 사유(자동 '확인 필요' 전환 기준)를 안내한다.
    """
    core = state["last"][:60] + "|" + state["evidence"][:60]
    sec = int(time.time() - state["t0"])
    stamp = f" (경과 {sec // 60}분 {sec % 60:02d}초)"
    if core == state["last_notice"]:
        state["repeat"] = state.get("repeat", 1) + 1
        return (f"\n⏳ 같은 항목을 계속 검토하고 있습니다({state['repeat']}번째 알림) — "
                f"{state['last'][:60]}{stamp}"
                f" · {llm_timeout_s}초를 넘기면 해당 항목은 '확인 필요'로 넘기고 진행합니다")
    state["last_notice"] = core
    state["repeat"] = 1
    line = f"\n⏳ 지금: {state['last'][:60]}"
    if state["evidence"]:
        line += f" · 마지막 확인: {state['evidence'][:80]}"
    return line + stamp


def _last_user_text(body: dict) -> tuple[str, list]:
    """OpenAI 형식 messages에서 마지막 사용자 텍스트와 전체 이력을 회수."""
    messages = body.get("messages") or []
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c, messages
        if isinstance(c, list):  # 멀티모달 파트 배열
            txt = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
            return txt, messages
    return "", messages


class Pipe:
    class Valves(BaseModel):
        AUDIT_CORE_HOME: str = Field(
            default=AUDIT_HOME, description="audit_core 패키지·.env 반입 경로"
        )
        EMIT_CITATIONS: bool = Field(
            default=True, description="인용 법령을 citation 칩(클릭→원문)으로 표시"
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── 이벤트 헬퍼 ──────────────────────────────────────
    @staticmethod
    async def _status(emitter, text: str, done: bool = False):
        if emitter:
            await emitter({"type": "status", "data": {"description": text, "done": done}})

    async def _emit_citations(self, emitter, orch, ref_tags: dict):
        """검증된 인용 법령을 클릭→원문 칩으로. 설계서 §4 '인용 ID→원문 lookup'의
        표시 계층 — law_fetcher 캐시가 원문 스토어 역할(추가 조회 없음, 캐시 히트)."""
        if not (emitter and self.valves.EMIT_CITATIONS):
            return
        for ref, tag in ref_tags.items():
            try:
                art = orch.law.fetch_ref(ref)
            except Exception:
                continue
            label = f"{ref}" + (f" ({tag})" if tag != "직접적용" else "")
            await emitter({
                "type": "citation",
                "data": {
                    "source": {"name": label},
                    "document": [art.text],
                    "metadata": [{
                        "source": f"{art.law_name} {art.article} — 근거 태그 [{tag}], 국가법령정보 실존 검증분",
                    }],
                },
            })

    # ── 검토 모드 (이벤트 네이티브 구현) ─────────────────
    async def _review(self, msg: str, messages: list, emitter, auto: bool, banner: str,
                      files: list | None = None) -> str:
        from daily_audit_pipe import Pipeline as LegacyPipe
        from audit_core.orchestrator import (
            Orchestrator, format_self_check, format_user_summary, format_written_review,
        )
        from audit_core.rules.completeness import RequiredDocs, format_completeness
        from audit_core.rules.cross_check import DocPart, split_bundle
        from audit_core.rules.rereview import detect_rereview, format_rereview, has_marker
        from audit_core.rules.doc_type import detect_doc_type
        from audit_core.rules.sensitivity import mask_pii
        from audit_core.rules.target_check import rubric_group

        legacy = LegacyPipe()
        doc = msg if auto else legacy._extract_doc(msg, messages, {})
        if doc and len(doc) < 150 and files:
            doc = ""  # 짧은 지시문("이 문서 검토해줘")은 본문이 아니라 명령 — 첨부가 본문

        # ── 1차 즉시 사전점검(LLM 미관여) — 파일을 읽는 족족 실제 근거를 내보낸다
        # ("파일을 확인했습니다" 같은 추상 문구 금지 — 파일명·글자수·검산 결과로 말한다)
        filenames, cost_lines, extra_parts, skipped, attach_texts = [], [], [], [], []
        prep_evidence: list[str] = []   # 15초 안내용 '마지막 확인 근거'
        if files:
            paths = _attachment_paths(files)
            filenames = [n for n, _p in paths]
            if paths:
                yield banner + f"📎 첨부 {len(paths)}건을 읽고 있습니다…\n\n"
                banner = ""
            for name, path in paths:
                r = await asyncio.to_thread(_parse_one_attachment, name, path)
                yield mask_pii(r["evidence"]) + "\n"
                prep_evidence.append(r["evidence"])
                if r["kind"] == "text":
                    attach_texts.append((r["name"], r["text"]))
                elif r["kind"] == "cost":
                    cost_lines += r["cost_lines"]
                    if r["total"]:
                        extra_parts.append(DocPart(f"{name}(검산 총액)",
                                                   f"사업비\n금{int(r['total'].claimed):,}원"))
                else:
                    skipped.append(r["note"])
            if attach_texts:
                attach_doc = "\n\n".join(f"[문서: {n}]\n{t}" for n, t in attach_texts)
                doc = (doc + "\n\n" + attach_doc).strip() if doc else attach_doc

        # xlsx만 첨부한 경우(2026-07-15 정확성 A) — 검산 결과를 정상 반환하고 종료.
        # "문서를 넣어달라"로 끝내지 않는다: 검산은 코드가 이미 끝냈다.
        if not doc and cost_lines:
            parts = ["**[자동 계산 — 산출내역(xlsx) 검산]**\n" + "\n".join(cost_lines)]
            try:
                comp = RequiredDocs().check("", filenames=filenames)
                parts.append(format_completeness(comp))
            except Exception:
                pass
            parts.append("> ℹ 텍스트 문서(hwp·hwpx·pdf 또는 본문 붙여넣기)가 없어 "
                         "**AI 검토는 생략**했습니다 — 위 검산은 자동 계산 결과입니다. "
                         "의뢰서·계획서를 함께 첨부하면 서류 완결성·법령 검토까지 이어집니다.")
            yield "\n" + mask_pii("\n\n".join(parts))
            return
        if not doc:
            yield (banner + "검토할 문서를 함께 넣어 주세요.\n"
                   "hwpx·hwp·xlsx·pdf 파일을 첨부하거나, 문서 본문을 그대로 붙여넣으면 됩니다."
                   + ("\n> ⏭ " + " / ".join(skipped) if skipped else ""))
            return

        profile = detect_doc_type(doc)
        # 사업성격 해석(현실 표현→법정 유형) 우선 — 라벨 없는 실문서 대응(2026-07-15)
        bp, biz_type = await asyncio.to_thread(legacy._classify_biz, doc, True)
        group = rubric_group(biz_type)
        method = (bp.contract_method if bp.contract_method != "미상" else None) or legacy._detect_method(doc)

        # 문서성 게이트(첨부 없을 때만): 공문서 신호 0개면 7축을 돌리지 않는다 —
        # 잡담·질문 장문에 전 에이전트 출동 후 '판단 불가 30건' 방지(2026-07-15 실장애)
        if not files:
            import re as _re
            doc_signals = sum([
                bool(profile.reason and "미감지" not in profile.reason and "복합" not in profile.reason),
                bool(biz_type), bool(method),
                bool(legacy._target_preface(doc)),
                bool(_re.search(r"소\s*계|합\s*계|부가가치세|산\s*출|추정\s*가격|사업비", doc)),
            ])
            try:
                from audit_core.rules.completeness import RequiredDocs as _RD
                doc_signals += bool(_RD().check(doc).recognized)
            except Exception:
                pass
            if doc_signals == 0:
                yield (banner + "🤔 붙여넣으신 내용에서 공문서 표지·사업유형·금액 같은 신호를 "
                       "찾지 못해 **검토를 시작하지 않았습니다** (질문·일반 글로 보입니다).\n"
                       "- 문서 점검: 의뢰서·공고문·계산서 **본문 붙여넣기** 또는 **파일 첨부**\n"
                       "- 질문이라면 아래에 우선 답해 드립니다.\n\n")
                yield await asyncio.to_thread(legacy._mode_qa, doc[:300])
                return
        # A5.5 문서 간 대조 — 첨부가 있으면 '파일 단위'가 문서 경계(재분리 금지:
        # 공고문 속 '과업지시서' 언급 등으로 과분리·재심사 오인되던 실장애 방지).
        # 붙여넣기 본문만 있을 때는 표제 줄 기준 번들 분리.
        if files and filenames:
            doc_parts = [DocPart(n, t) for n, t in attach_texts] + extra_parts
        else:
            doc_parts = split_bundle(doc) + extra_parts
        if len(doc_parts) < 2:
            doc_parts = None
        # 재심사 모드(SOP ②): 같은 유형 문서 2벌이면 변경점만 LLM 검토 대상으로
        rereview = detect_rereview(doc_parts) if doc_parts else None
        llm_doc = rereview.changed_text if rereview else None
        is_full = (not auto) and msg.startswith("검토")
        label = "서면검토" if is_full else "자가점검"

        parts = [banner + f"📋 {label} — 문서유형: **{profile.label}** · "
                 f"사업유형: {biz_type or '미감지'} → 검토 분야 **{group}**"
                 + (f" · 계약방법: **{method}**" if method else "")
                 + (f" · 문서 {len(doc_parts)}건 인식(교차 대조)" if doc_parts else "")
                 + (f" · 🔁 재심사(변경 {rereview.n_changes}줄만 검토)" if rereview else "")]
        if filenames:
            parts.append("> ※ 첨부 파일은 파기 운영값 확정 전까지 서버에 보관될 수 있습니다 — 민감 문서는 유의하세요."
                         + ("\n> ⏭ " + " / ".join(skipped) if skipped else ""))
        if cost_lines:
            parts.append("**[자동 계산 — 산출내역(xlsx) 검산]**\n" + "\n".join(cost_lines))
        if rereview:
            parts.append(format_rereview(rereview))
        elif has_marker(doc):
            parts.append("> 🔁 재공고·재심사 표지가 보입니다. **직전 버전 문서를 함께 붙여넣으면** 변경점만 골라 검토합니다(전체 재검토 방지).")
        if auto:
            parts.append("> 문서로 자동 인식했습니다. 감사팀용 의견서 초안이 필요하면 `검토` 뒤에 본문을 붙여넣으세요.")

        if biz_type:
            parts.append(f"🧭 사업성격: **{bp.primary_type}"
                         + (f"({bp.subtype})" if bp.subtype else "")
                         + f"** 후보 — 근거 {' '.join(bp.evidence[:3])} (신뢰 {bp.confidence})"
                         + (f"\n> ⚠ 혼합 요소: {bp.mixed_notes} — 담당자 확인 필요" if bp.mixed else ""))
        preface = legacy._target_preface(doc, biz_type=biz_type)
        if preface:
            parts.append(preface)
        comp_missing: list[str] = []
        try:
            comp = RequiredDocs().check(doc, method=method, biz_type=biz_type or group,
                                        filenames=filenames)
            comp_missing = [lb for _k, lb, _h in comp.missing]
            parts.append(format_completeness(comp))
        except Exception:
            pass  # 완결성 확인 실패가 검토를 막으면 안 됨

        # 1차(자동 확인) 부분 결과를 즉시 스트리밍 — 이후 단계가 실패해도 이 결과는 남는다
        yield "\n" + mask_pii("\n\n".join(parts)) + "\n\n"
        yield ("— 여기까지가 **1차(자동 확인) 결과**입니다. 이어서 AI 검토를 시작합니다."
               " (목표 1~2분, 시간이 걸리면 완료된 결과부터 드립니다)\n\n")

        state = {
            "last": f"{label} 시작 — 활성 축 선별",
            "evidence": prep_evidence[-1] if prep_evidence else "",
            "last_notice": "", "cancelled": False, "t0": time.time(),
        }

        def _fmt() -> str:
            sec = int(time.time() - state["t0"])
            return f"{state['last']} · 경과 {sec // 60}분 {sec % 60:02d}초"

        await self._status(emitter, f"{label} 시작 (AI 검토는 통합 호출 — 목표 1~2분)")
        _ensure_perf_log()

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        def progress(m: str):  # 검토 스레드 → 말풍선 스트림 + status + 운영 로그
            if state["cancelled"]:   # 취소 후 도착한 늦은 이벤트는 폐기(화면·로그 오염 방지)
                return
            state["last"] = m
            if m.lstrip().startswith("↳"):  # 발견 서사는 문서 원문 인용 포함 — 로그엔 항목 id만
                state["evidence"] = m.lstrip()[1:].strip()  # '마지막 확인 근거'로 보관
                _agent_log("  · " + m.split("]")[0].lstrip() + "] (발견 — 내용은 화면 표시만)")
            else:
                _agent_log("  · " + m)
            loop.call_soon_threadsafe(q.put_nowait, m)
            if emitter:
                asyncio.run_coroutine_threadsafe(self._status(emitter, _fmt()), loop)

        async def heartbeat():
            while True:
                await asyncio.sleep(15)
                await self._status(emitter, _fmt())

        hb = asyncio.create_task(heartbeat()) if emitter else None

        # 단계별 시간 예산 — 초과 시 남은 LLM 항목은 '확인 필요'로 넘기고
        # 결정론 결과는 유지(부분 결과 우선). 취소 시에도 같은 경로로 멈춘다.
        from audit_core.config import get_settings as _gs
        budget_s = _gs().AUDIT_BUDGET_WRITTEN_S if is_full else _gs().AUDIT_BUDGET_SELF_S
        deadline = state["t0"] + budget_s

        def should_stop() -> bool:
            return state["cancelled"] or time.time() > deadline

        async def _run():
            orch = Orchestrator()
            if is_full:
                wr = await asyncio.to_thread(
                    orch.written_review, group, doc,
                    progress=progress, doc_profile=profile, contract_method=method,
                    doc_parts=doc_parts, llm_doc_text=llm_doc, should_stop=should_stop,
                )
                render = (format_user_summary(wr, missing_docs=comp_missing)
                          + "\n\n" + format_written_review(wr))
                return orch, render, wr.ref_tags
            report = await asyncio.to_thread(
                orch.self_check, group, doc,
                progress=progress, doc_profile=profile, contract_method=method,
                doc_parts=doc_parts, llm_doc_text=llm_doc, should_stop=should_stop,
            )
            return orch, format_self_check(report), orch.tags.classify_all(
                sorted(set(report.law_context_used)))

        task = asyncio.create_task(_run())
        task.add_done_callback(lambda _t: q.put_nowait(_DONE))
        HARD_LIMIT_S = 20 * 60   # 스톨 워치독 — 초과 시 중단·안내(무한 대기 금지)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=PROGRESS_GAP_S)
                except asyncio.TimeoutError:
                    # 15초 케이던스: 마지막 확인 근거 + 현재 작업(추상 문구·점 펄스 금지)
                    if time.time() - state["t0"] > HARD_LIMIT_S:
                        state["cancelled"] = True
                        task.cancel()
                        _agent_log("✗ 워치독: 20분 초과 — 검토 중단")
                        yield ("\n\n⛔ **검토를 중단했습니다** — 20분이 지나도록 응답이 없어 "
                               "안전하게 멈췄습니다. 위에 표시된 1차(자동 확인) 결과까지는 "
                               "유효합니다. 잠시 후 다시 시도해 주세요.")
                        return
                    yield mask_pii(_idle_notice(state, int(_gs().AUDIT_TIMEOUT_REVIEW_S)))
                    continue
                if item is _DONE:
                    break
                yield "\n" + mask_pii(item)
            err = task.exception()
            if err:
                await self._status(emitter, f"검토 중 오류: {err}", done=True)
                yield (f"\n⚠ AI 검토 중 오류: {err}\n위에 표시된 1차(자동 확인) 결과까지는 "
                       "유효합니다. 다시 시도하거나 담당자에게 문의하세요.")
                return
            orch, render, ref_tags = task.result()
        except GeneratorExit:
            # 사용자가 응답 생성을 중단 — 진행 스레드를 멈추고 늦은 이벤트를 폐기
            state["cancelled"] = True
            task.cancel()
            _agent_log("✗ 사용자 중단 — 검토 취소(잔여 이벤트 폐기)")
            raise
        finally:
            if hb:
                hb.cancel()

        await self._emit_citations(emitter, orch, ref_tags)
        total = int(time.time() - state["t0"])
        await self._status(
            emitter, f"{label} 완료 (소요 {total // 60}분 {total % 60:02d}초)", done=True
        )
        over = "" if time.time() <= deadline else (
            f"\n> ⏱ 시간 예산({budget_s // 60}분)을 넘겨 일부 항목은 '확인 필요'로 남겼습니다.")
        yield (f"\n✅ **{label} 완료** — 총 {total // 60}분 {total % 60:02d}초. "
               f"결과를 정리했습니다.{over}\n\n---\n\n")
        yield mask_pii(render)

    # ── 진입점 ───────────────────────────────────────────
    async def pipe(self, body: dict, __user__: dict | None = None,
                   __event_emitter__=None, __event_call__=None, __files__: list | None = None):
        home = self.valves.AUDIT_CORE_HOME
        if home not in sys.path:
            sys.path.insert(0, home)
        _ensure_fresh_modules(home)
        try:
            _fix_env_collisions()
            from daily_audit_pipe import BANNER, GUIDE, Pipeline as LegacyPipe, parse_amount
        except Exception as e:
            return (f"⚠ audit_core 반입 경로를 찾을 수 없습니다: {home}\n"
                    f"({e})\n관리자: docker cp로 audit_core·daily_audit_pipe.py를 반입하세요.")

        msg, messages = _last_user_text(body)
        msg = msg.strip()
        _t0 = time.time()
        legacy = LegacyPipe()
        banner = BANNER if legacy._is_first_turn(messages[:-1]) else ""

        try:
            if msg.startswith("관문"):  # 의도 목록 밖의 부가 기능 — 접두 명령 유지
                return banner + legacy._mode_gates(msg)

            from audit_core.rules.intent import classify_intent
            it = classify_intent(msg, has_files=bool(__files__))
            _agent_log(f"▶ intent={it.intent} ({it.reason}) len={len(msg)} files={len(__files__ or [])}")
            if it.intent == "document_review":
                explicit = msg.startswith("점검") or msg.startswith("검토")
                return self._review(msg, messages, __event_emitter__,
                                    auto=not explicit, banner=banner, files=__files__)
            if it.intent == "ledger":
                return banner + legacy._mode_ledger(msg)
            if it.intent == "deadline":
                return banner + legacy._mode_deadline(msg)
            if it.intent == "law_lookup":
                if __event_emitter__:
                    await self._status(__event_emitter__, "📚 법령을 찾고 있습니다…")
                out = await asyncio.to_thread(legacy._mode_law, msg)
                if __event_emitter__:
                    await self._status(__event_emitter__, "법령 조회 완료", done=True)
                return banner + out
            if it.intent == "target_check":
                return banner + legacy._mode_target(msg)
            if it.intent == "business_type_question":
                return banner + legacy._mode_biztype(msg)  # 결정론 분류기 — 즉답
            if it.intent == "help":
                return (banner + GUIDE) if banner else GUIDE
            if it.intent == "greeting":
                return (banner + "👋 안녕하세요, 효규가영입니다. 문서 점검(파일 첨부·본문 붙여넣기), "
                        "대상 판별(`대상? 유형 금액`), 법령 조회(`법령 …`)를 도와드립니다.")
            if it.intent == "out_of_scope":
                return (banner + "😅 그 주제는 제 소관이 아니에요 — 저는 일상감사·계약 지원 전용입니다. "
                        "문서 점검이나 대상 판별이 필요하시면 말씀해 주세요.")
            # audit_question(기본)
            if __event_emitter__:
                await self._status(__event_emitter__, "규정 요지에서 답을 찾고 있습니다…")
            answer = await asyncio.to_thread(legacy._mode_qa, msg)
            if __event_emitter__:
                await self._status(__event_emitter__, "안내 완료", done=True)
            return banner + answer
        except Exception as e:  # 규칙엔진 오류가 세션을 죽이면 안 됨
            _agent_log(f"✗ 오류 {type(e).__name__}: {e} (경과 {time.time()-_t0:.1f}초)")
            return (f"⚠ 처리 중 오류: {e}\n"
                    "다시 시도하시거나, 계속되면 감사팀(기획예산과)에 알려주세요. (`도움말` = 사용법)")
        finally:
            _agent_log(f"■ 완료 (총 {time.time()-_t0:.1f}초)")
