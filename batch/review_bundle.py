"""번들(zip/폴더) 에이전틱 검토 러너 — 검토요청 세트를 통째로 받아 처리하는 흐름.

파일 첨부 개방(C2, 파기 보장) 전의 호스트 사이드 구현이자, 개방 후 파이프에 연결될
"첨부 세트 인입" 흐름의 청사진. 단계마다 산출물을 다음 단계로 넘긴다:

  [1 인입]   포맷별 파서 자동 선택 (hwpx=구조 / hwp=텍스트 / xlsx=검산 / pdf=공문표지 스킵)
  [2 분류]   문서 역할 감지 (요청서/제안요청서/검토서/기타) + doc_type 프로파일
  [3 판정]   요청서에서 유형·금액 추출 → 대상판정 규칙엔진
  [4 검토]   LLM 축별 서면검토 — 컨텍스트 한도 내 문서만 결합(초과분은 명시 스킵)
  [5 검산]   산출내역서(xlsx) 결정론 검산 — LLM 무관, 항상 수행
  [6 출력]   의견서 초안 + 검산 결과 + 스킵 내역 통합 리포트

사용: OLLAMA_BASE_URL=http://127.0.0.1:11434 LAW_API_OC=... \
      .venv/bin/python batch/review_bundle.py <번들.zip|폴더> [...]
"""

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit_core.parsers.cost_xlsx import CostSheetError, check_cost_sheet
from audit_core.parsers.hwp import HwpParseError, parse_hwp
from audit_core.parsers.hwpx import HwpxParseError, parse_hwpx

# 로컬 LLM 컨텍스트(16K 토큰) 안전선 — doc_sectioner 구현 전 잠정 한도
# (조각 검토 도입으로 크기 제한 제거 — AxisReviewer.CHUNK_CHARS가 분할 담당)


def collect(bundle: Path) -> list[Path]:
    if bundle.is_dir():
        return sorted(p for p in bundle.rglob("*") if p.is_file())
    tmp = Path(tempfile.mkdtemp(prefix="bundle_"))
    with zipfile.ZipFile(bundle) as zf:
        for info in zf.infolist():
            fn = info.filename
            try:
                fn = fn.encode("cp437").decode("cp949")  # 공문 zip 파일명 복원
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            if info.is_dir():
                continue
            target = tmp / fn
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
    return sorted(p for p in tmp.rglob("*") if p.is_file())


def ingest(path: Path) -> tuple[str, str]:
    """(종류, 텍스트) — 종류: text|cost|skip"""
    low = path.suffix.lower()
    try:
        if low == ".hwpx":
            return "text", parse_hwpx(path).text
        if low == ".hwp":
            return "text", parse_hwp(path).text
        if low == ".xlsx":
            return "cost", str(path)
        if low == ".pdf":
            try:
                return "text", parse_pdf(path).text
            except PdfParseError as e:
                return "skip", f"PDF 인입 불가 — {e}"
        return "skip", f"미지원 포맷({low}) — 참고자료로 간주"
    except (HwpxParseError, HwpParseError) as e:
        return "skip", f"파싱 실패: {e}"


def classify(name: str, text: str) -> str:
    if "요청서" in name or "일 상 감 사 요 청 서" in text[:200]:
        return "요청서"
    if "제안요청" in name or "제안요청서" in text[:300]:
        return "제안요청서"
    if "검토서" in name:
        return "검토서"
    if "규격서" in name or "납품조건" in name:
        return "규격서"
    return "기타"


def run_bundle(bundle: Path) -> None:
    from audit_core.orchestrator import Orchestrator, format_written_review
    from audit_core.rules.doc_type import detect_doc_type
    from audit_core.rules.target_check import TargetInput, TargetRuleSet, check_target, rubric_group

    print(f"\n{'█' * 70}\n■ 번들: {bundle.name}\n{'█' * 70}")

    # ── [1 인입] ─────────────────────────────────────────
    files = collect(bundle)
    texts: dict[str, str] = {}
    cost_sheets: list[str] = []
    for f in files:
        kind, payload = ingest(f)
        if kind == "text":
            texts[f.name] = payload
            print(f"[1 인입]  ✓ {f.name} ({len(payload):,}자)")
        elif kind == "cost":
            cost_sheets.append(payload)
            print(f"[1 인입]  ✓ {f.name} (산출내역서 → 검산 레인)")
        else:
            print(f"[1 인입]  ⏭ {f.name} — {payload}")

    # ── [2 분류] ─────────────────────────────────────────
    roles = {name: classify(name, t) for name, t in texts.items()}
    for name, role in roles.items():
        print(f"[2 분류]  {role}: {name}")

    # ── [3 판정] — 요청서 기반 대상판정 ───────────────────
    req_text = next((t for n, t in texts.items()
                     if roles[n] == "요청서" and "제안요청" not in n), "")
    rules = TargetRuleSet()
    biz_kw = rules.detect_type_keyword(req_text or " ".join(texts.values()))
    # 실물 교훈: 요청서에 유형 단어(용역/물품)가 없고 계약방법만 있는 경우 —
    # '협상에 의한 계약'은 대상판정 유형 '협상용역'으로 결정론 추론
    if not biz_kw and "협상에 의한 계약" in req_text:
        biz_kw = "협상용역"
    import re
    m = re.search(r"(?:추정금액|사업비|추정가격)\D*?([0-9][0-9,]{6,})\s*원", req_text)
    amount = int(m.group(1).replace(",", "")) if m else None
    if biz_kw and amount:
        d = check_target(TargetInput(biz_type=biz_kw, amount=amount), rules)
        print(f"[3 판정]  {d.label} — {d.reason}")
        group = rubric_group(rules.normalize_type(biz_kw))
    else:
        print("[3 판정]  유형·금액 미확정 — 확인 필요(REVIEW), 공통 축으로 검토")
        group = rubric_group(None)

    # ── [4 검토] — 전 문서 결합 (조각 검토 도입으로 크기 제한 없음, doc_sectioner v1) ──
    combined = [f"[문서: {name}]\n{t}" for name, t in sorted(texts.items(), key=lambda kv: len(kv[1]))]
    skipped_docs = []
    doc = "\n\n".join(combined)

    # ── [4.5 검산] — xlsx 결정론 (검토 전 수행 — 총액을 문서 간 대조에 참여시킴) ──
    from audit_core.rules.cross_check import DocPart
    cost_lines = []
    doc_parts = [DocPart(name, t) for name, t in texts.items()]
    for p in cost_sheets:
        try:
            r = check_cost_sheet(p)
            print(f"[검산]    {Path(p).name}: {len(r.checks)}건 중 불일치 {len(r.flags)}건")
            for c in r.flags:
                cost_lines.append(f"- ✗ [{c.kind}] {c.expr} → 재계산 {c.expected:,} vs 기재 {c.claimed:,}")
            cost_lines += [f"- ℹ {n}" for n in r.notes]
            # 산출내역서 최종 총액(부가세 포함 SW개발비)을 텍스트 문서들과 총액 대조
            total = next((c for c in r.checks if c.kind == "fp_total"), None)
            if total:
                doc_parts.append(DocPart(
                    f"{Path(p).name}(검산 총액)", f"사업비\n금{int(total.claimed):,}원"))
        except CostSheetError as e:
            print(f"[검산]    {Path(p).name}: 서식 인식 불가({e})")

    profile = detect_doc_type(doc)
    print(f"[4 검토]  문서유형={profile.label} · 루브릭 {group}축 · 투입 {len(doc):,}자 — LLM 축별 검토 시작")
    orch = Orchestrator()
    wr = orch.written_review(group, doc, progress=lambda m: print(f"          - {m}"),
                             doc_profile=profile,
                             doc_parts=doc_parts if len(doc_parts) >= 2 else None)

    # ── [6 출력] ─────────────────────────────────────────
    print(f"\n[6 출력]  의견서 초안 ↓\n{'─' * 70}")
    print(format_written_review(wr))
    if cost_lines:
        print("\n**[산출내역서 검산 — 결정론]**")
        print("\n".join(cost_lines))
    if skipped_docs:
        print(f"\n**[⏭ 미검토 문서]** {', '.join(skipped_docs)} — 섹션 분할(doc_sectioner) 구현 후 검토 가능")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for arg in sys.argv[1:]:
        run_bundle(Path(arg))
