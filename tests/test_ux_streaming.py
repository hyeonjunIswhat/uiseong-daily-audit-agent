"""2026-07-15 종합 개선 테스트 — 15초 근거 진행·PII 마스킹·xlsx 단독·요약 화면·합성 생략.

성능 관련 검증은 전부 모의 지연(mock latency)으로 한다 — 실제 모델 속도에 의존 금지.
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "pipelines"))

_TMP_HOME = tempfile.mkdtemp(prefix="audit_home_")
os.environ["AUDIT_CORE_HOME"] = _TMP_HOME  # agent.log가 실경로를 요구 — 임시 홈

_spec = importlib.util.spec_from_file_location(
    "daily_audit_function", PROJECT_ROOT / "functions" / "daily_audit_function.py")
fmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fmod)

from audit_core.agents.schemas import AxisResult  # noqa: E402
from audit_core.agents.synthesizer import Finding  # noqa: E402
from audit_core.agents.verifier import NumericCheck  # noqa: E402
from audit_core.orchestrator import (  # noqa: E402
    Orchestrator, ReviewReport, WrittenReview, format_user_summary,
)
from audit_core.rules.sensitivity import mask_pii  # noqa: E402


def _collect(gen, until=None, limit=200):
    """async generator 소비 헬퍼 — until(부분 문자열)을 만나면 중단하고 gen을 반환."""
    chunks = []

    async def run():
        async for c in gen:
            chunks.append(c)
            if until and until in c:
                return
            if len(chunks) >= limit:
                return

    asyncio.run(run())
    return chunks


class TestMaskPii(unittest.TestCase):
    def test_pii_masked_amounts_kept(self):
        src = ("담당 김OO 010-1234-5678, kim@uiseong.go.kr, 주민 900101-1234567, "
               "계좌번호: 356-0000-1234-56 / 사업비 79,860,000원")
        out = mask_pii(src)
        for leak in ["010-1234-5678", "kim@uiseong.go.kr", "900101-1234567", "356-0000-1234-56"]:
            self.assertNotIn(leak, out)
        self.assertIn("79,860,000원", out)  # 검토에 필요한 금액은 보존


class TestIdleNotice(unittest.TestCase):
    def test_notice_has_evidence_and_no_dots(self):
        state = {"last": "🔍 검토 에이전트가 3개 축을 살펴보고 있습니다", "t0": time.time(),
                 "evidence": "📄 [원문 확인] `의뢰서.hwpx` — 본문 2,163자를 읽었습니다",
                 "last_notice": "", "cancelled": False}
        n1 = fmod._idle_notice(state, 180)
        self.assertIn("지금:", n1)
        self.assertIn("마지막 확인:", n1)
        self.assertIn("의뢰서.hwpx", n1)       # 추상 문구가 아니라 실제 파일명
        self.assertNotEqual(n1.strip(), "·")   # 구식 점 펄스 금지
        self.assertNotIn("LLM 응답 대기 중", n1)  # 추상 대기 문구 금지
        n2 = fmod._idle_notice(state, 180)     # 같은 작업이 이어질 때
        self.assertNotEqual(n1, n2)            # 동일 문장 반복 금지
        self.assertIn("확인 필요", n2)          # 자동 전환 기준 안내


class TestParseOneAttachment(unittest.TestCase):
    def test_unsupported_ext_says_why(self):
        r = fmod._parse_one_attachment("사진.png", "/tmp/사진.png")
        self.assertEqual(r["kind"], "skip")
        self.assertIn("사진.png", r["evidence"])
        self.assertIn("확인 필요", r["evidence"])

    def test_text_evidence_has_name_and_charcount(self):
        fake = SimpleNamespace(text="일상감사 의뢰서\n사업명: 정보화 용역\n" * 30)
        with mock.patch("audit_core.parsers.hwpx.parse_hwpx", return_value=fake):
            r = fmod._parse_one_attachment("의뢰서.hwpx", "/tmp/의뢰서.hwpx")
        self.assertEqual(r["kind"], "text")
        self.assertIn("의뢰서.hwpx", r["evidence"])
        self.assertIn(f"{len(fake.text):,}자", r["evidence"])
        self.assertIn("원문 확인", r["evidence"])

    def test_xlsx_evidence_has_sheet_and_mismatch(self):
        bad = NumericCheck("sum", "소계+부가세=합계", 79_860_000, 85_000_000, False)
        fake = SimpleNamespace(sheet="개발비산정", checks=[bad], notes=[])
        with mock.patch("audit_core.parsers.cost_xlsx.check_cost_sheet", return_value=fake):
            r = fmod._parse_one_attachment("내역서.xlsx", "/tmp/내역서.xlsx")
        self.assertEqual(r["kind"], "cost")
        self.assertIn("개발비산정", r["evidence"])
        self.assertIn("불일치 1건", r["evidence"])
        self.assertIn("자동 계산", r["evidence"])


class TestXlsxOnlyReview(unittest.TestCase):
    def test_xlsx_only_returns_checks_not_ask_for_doc(self):
        cost = {"kind": "cost", "name": "내역서.xlsx", "total": None,
                "cost_lines": ["- 내역서.xlsx: 검산 5건 중 불일치 1건 — 소계+부가세=합계"],
                "evidence": "🧮 [자동 계산] `내역서.xlsx` 시트 '개발비산정' — 산식 5건을 검산해 불일치 1건을 확인했습니다"}
        with mock.patch.object(fmod, "_attachment_paths", return_value=[("내역서.xlsx", "/tmp/x.xlsx")]), \
             mock.patch.object(fmod, "_parse_one_attachment", return_value=cost):
            chunks = _collect(fmod.Pipe()._review(
                "", [], None, auto=True, banner="", files=[{"id": "f1"}]))
        out = "".join(chunks)
        self.assertIn("자동 계산", out)
        self.assertIn("불일치 1건", out)
        self.assertIn("AI 검토는 생략", out)             # 생략을 명시(침묵 금지)
        self.assertNotIn("검토할 문서를 함께 넣어", out)  # 구 결함: 문서를 요구하며 종료


class TestUserSummary(unittest.TestCase):
    @staticmethod
    def _wr(**kw):
        return WrittenReview(report=kw.pop("report", ReviewReport(biz_type="용역")), **kw)

    def test_clean_is_proceed(self):
        text = format_user_summary(self._wr())
        self.assertIn("검토 결과: 진행 가능", text)
        self.assertIn("확인된 문제가 없습니다", text)

    def test_det_issue_is_fix_first(self):
        rep = ReviewReport(biz_type="용역", numeric_flags=[
            NumericCheck("sum", "소계+부가세=합계", 79_860_000, 85_000_000, False)])
        text = format_user_summary(self._wr(report=rep))
        self.assertIn("보완 후 진행 권고", text)
        self.assertIn("먼저 조치할 사항", text)
        self.assertIn("79,860,000", text)
        self.assertIn("다음 조치", text)

    def test_unable_only_is_human_check(self):
        rep = ReviewReport(biz_type="용역", axis_results=[AxisResult.model_validate(
            {"axis": "1", "items": [{"item_id": "C1", "verdict": "UNABLE",
                                     "evidence": "근거 문서 미첨부", "severity": 1}]})])
        text = format_user_summary(self._wr(report=rep))
        self.assertIn("담당자 확인 필요", text)
        self.assertIn("확인하지 못한 사항", text)

    def test_missing_docs_and_finding(self):
        wr = self._wr(confirmed=[Finding("C1", "1", "계약방법 근거가 명시돼 있는가?",
                                         "근거 조항 미기재", 2)])
        text = format_user_summary(wr, missing_docs=["과업지시서"])
        self.assertIn("보완 후 진행 권고", text)
        self.assertIn("추가로 필요한 서류", text)
        self.assertIn("과업지시서", text)
        self.assertIn("검토 의견 1건", text)


class _Boom:
    """호출되면 실패 — 무지적 시 문맥검증·합성·탐색이 호출되지 않음을 증명."""
    def __getattr__(self, name):
        raise AssertionError(f"무지적 경로에서 {name} 호출 금지")


class _NoLaw:
    def fetch_ref(self, ref):
        raise RuntimeError("법령 조회 비활성(테스트)")


class TestSynthesisSkip(unittest.TestCase):
    def test_det_only_doc_never_says_no_issues(self):
        from audit_core.agents.axis_reviewer import AxisReviewer, Rubric
        from audit_core.agents.base import OllamaClient

        class AllOk(OllamaClient):
            def __init__(self):
                pass

            def chat_json(self, *, model, prompt, schema, **kw):
                import re
                ids = set(re.findall(r"^- ([A-Za-z0-9]\w*(?:-\d+)?): ", prompt, re.M))
                return schema.model_validate({"axis": "ALL", "items": [
                    {"item_id": i, "verdict": "OK", "evidence": "확인", "severity": 1}
                    for i in ids]})

        orch = Orchestrator(reviewer=AxisReviewer(client=AllOk()), rubric=Rubric(),
                            law_fetcher=_NoLaw(), context_verifier=_Boom(),
                            synthesizer=_Boom(),
                            law_search=SimpleNamespace(enabled=False))
        doc = "용역\n소계: 100원\n부가가치세: 10원\n합계: 999원"
        wr = orch.written_review("용역", doc)
        self.assertIsNotNone(wr.opinion)
        self.assertNotIn("지적사항 없음", wr.opinion.overall)  # 산식 오류가 있으므로 금지
        self.assertIn("자동 확인", wr.opinion.overall)


class _StubOrch:
    """모의 지연 오케스트레이터 — 침묵 구간을 만들어 15초 규칙(단축판)을 검증."""
    progress_ref = {}

    def __init__(self):
        from audit_core.rules.citation_tags import CitationTags
        self.tags = CitationTags()
        self.law = None

    def self_check(self, group, doc, progress=None, should_stop=None, **kw):
        _StubOrch.progress_ref["fn"] = progress
        progress("🔍 검토 에이전트가 축을 살펴보고 있습니다")
        time.sleep(0.6)   # 침묵 → 케이던스 알림이 나와야 함
        progress("↳ [C1] 여기서 찾았습니다 — 계약방법 근거 조항")
        time.sleep(0.4)
        return ReviewReport(biz_type="용역")


_DOC = ("정보화 전략계획 수립 용역 일상감사 의뢰서\n사업명: 정보화 전략계획 수립 용역\n"
        "용역기간: 6개월\n소계: 100,000,000원\n부가가치세: 10,000,000원\n"
        "합계: 110,000,000원\n" + "과업 내용 상세. " * 20)

_BP = SimpleNamespace(primary_type="용역", subtype=None, evidence=["'용역' 표제"],
                      confidence="high", mixed=False, mixed_notes="", contract_method="미상")


class TestStreamingCadence(unittest.TestCase):
    def _patched(self):
        import daily_audit_pipe
        import audit_core.orchestrator as om
        return (mock.patch.object(om, "Orchestrator", _StubOrch),
                mock.patch.object(daily_audit_pipe.Pipeline, "_classify_biz",
                                  lambda self, doc, allow_llm=False: (_BP, "용역")),
                mock.patch.object(fmod, "PROGRESS_GAP_S", 0.15))

    def test_partial_first_then_evidence_notices_no_dots(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3:
            chunks = _collect(fmod.Pipe()._review("점검 " + _DOC, [], None,
                                                  auto=False, banner="", files=None))
        out = "".join(chunks)
        self.assertIn("1차(자동 확인) 결과", out)           # 부분 결과 선반환
        self.assertIn("자가점검 완료", out)                  # 최종 렌더 도달
        notices = [c for c in chunks if "⏳" in c]
        self.assertGreaterEqual(len(notices), 1)            # 침묵 구간에 근거 알림
        self.assertTrue(all(c.strip() != "·" for c in chunks))   # 구식 점 펄스 청크 금지
        self.assertNotIn("LLM 응답 대기 중", out)                  # 추상 대기 문구 금지
        self.assertEqual(len(notices), len(set(notices)))    # 동일 문장 반복 금지
        joined = "\n".join(notices)
        self.assertTrue("지금:" in joined or "계속 검토" in joined)

    def test_cancel_discards_late_events(self):
        p1, p2, p3 = self._patched()

        async def run():
            gen = fmod.Pipe()._review("점검 " + _DOC, [], None,
                                      auto=False, banner="", files=None)
            async for c in gen:
                if "1차(자동 확인) 결과" in c:
                    break
            await asyncio.sleep(0.05)   # AI 단계 진입 대기
            await gen.aclose()          # 사용자 중단
            await asyncio.sleep(0.9)    # 스텁 스레드가 늦은 progress를 쏘는 시간

        with p1, p2, p3:
            asyncio.run(run())
        log = Path(_TMP_HOME, "agent.log")
        content = log.read_text(encoding="utf-8") if log.exists() else ""
        self.assertNotIn("여기서 찾았습니다", content)  # 취소 후 늦은 이벤트 폐기


if __name__ == "__main__":
    unittest.main()
