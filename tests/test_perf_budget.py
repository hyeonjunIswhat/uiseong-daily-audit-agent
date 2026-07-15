"""2026-07-15 성능 규율 테스트 — LLM 호출 예산·규칙 트리아지·일괄 검증·다이제스트.

핵심 계약:
  자가점검      = LLM 최대 1콜 (축 선별·산식·대조는 LLM 0회)
  서면검토 무지적 = 1콜 (검증·합성 생략)
  서면검토 지적  = 3콜 (통합검토 1 + 일괄 문맥검증 1 + 합성 1)
전부 모의 클라이언트(Meter) 계측 — 실제 모델 속도에 의존하지 않는다.
"""

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.agents.axis_reviewer import AxisReviewer, Rubric
from audit_core.agents.base import LLMUnavailable, OllamaClient
from audit_core.agents.context_verifier import ContextVerifier
from audit_core.agents.synthesizer import Synthesizer
from audit_core.orchestrator import Orchestrator
from audit_core.rules.digest import build_review_digest


class Meter(OllamaClient):
    """호출 수·스키마별 횟수를 세는 모의 클라이언트."""

    def __init__(self, flag_item=None):
        self.calls: list[str] = []
        self.flag_item = flag_item

    def chat_json(self, *, model, prompt, schema, **kw):
        name = schema.__name__
        self.calls.append(name)
        if name == "AxisResult":
            ids = re.findall(r"^- ([A-Za-z0-9]\w*(?:-\d+)?): ", prompt, re.M)
            return schema.model_validate({"axis": "ALL", "items": [
                {"item_id": i,
                 "verdict": "FLAG" if i == self.flag_item else "OK",
                 "evidence": "근거 미비" if i == self.flag_item else "확인",
                 "severity": 2 if i == self.flag_item else 1} for i in ids]})
        if name == "ContextCheckBatch":
            ids = re.findall(r"^- item_id: (\S+)", prompt, re.M)
            return schema.model_validate({"checks": [
                {"item_id": i, "supports": True, "reason": "모의"} for i in ids]})
        if name == "OpinionDraft":
            return schema.model_validate({
                "query": "q", "facts": "f", "issues": [],
                "overall": "모의 종합", "recommendations": []})
        raise AssertionError(f"예산 밖 호출: {name}")


class _Law:
    def fetch_ref(self, ref):
        return type("A", (), {"law_name": ref, "article": "(조문)", "text": "본문"})()


class _NoSearch:
    enabled = False


def _orch(meter):
    return Orchestrator(
        reviewer=AxisReviewer(client=meter), rubric=Rubric(), law_fetcher=_Law(),
        context_verifier=ContextVerifier(client=meter),
        synthesizer=Synthesizer(client=meter), law_search=_NoSearch())


# 축 신호가 두루 있는 표준 문서(트리아지 전축 폴백 방지)
DOC = ("정보화 용역 일상감사 요청서\n관련 법령·조례 근거 있음\n"
       "예산 산출: 소계 100원, 부가가치세 10원, 합계 110원\n"
       "사업기간 6개월, 착수 후 납품\n계약방법: 협상에 의한 계약, 입찰 공고 예정\n"
       "첨부 서류 및 요건, 검사·승인 절차\n")


class TestCallBudget(unittest.TestCase):
    def test_self_check_is_single_llm_call(self):
        m = Meter()
        _orch(m).self_check("용역", DOC)
        self.assertEqual(m.calls, ["AxisResult"])  # 트리아지 LLM 0회 + 통합검토 1회

    def test_written_no_finding_is_single_call(self):
        m = Meter()
        wr = _orch(m).written_review("용역", DOC)
        self.assertEqual(m.calls, ["AxisResult"])  # 검증·합성 전부 생략
        self.assertIsNotNone(wr.opinion)

    def test_written_with_finding_is_three_calls(self):
        m = Meter(flag_item="C1")  # C1은 [직접적용] 조문 보유 → 검증 대상
        wr = _orch(m).written_review("용역", DOC)
        self.assertEqual(m.calls, ["AxisResult", "ContextCheckBatch", "OpinionDraft"])
        self.assertEqual([f.item_id for f in wr.confirmed], ["C1"])

    def test_review_timeout_no_retry_partial_result(self):
        class TimeoutClient(Meter):
            def chat_json(self, *, model, prompt, schema, **kw):
                self.calls.append(schema.__name__)
                raise LLMUnavailable("The read operation timed out")

        m = TimeoutClient()
        report = _orch(m).self_check("용역", DOC)
        self.assertEqual(m.calls, ["AxisResult"])   # 타임아웃 재시도 없음
        self.assertTrue(report.axis_results)         # 부분 결과(전 항목 확인 필요)는 유지
        self.assertTrue(all(it.verdict == "UNABLE"
                            for ar in report.axis_results for it in ar.items))
        self.assertEqual(len(report.numeric_flags), 0)  # 결정론 레인은 정상 수행됨


class TestBudgetExceeded(unittest.TestCase):
    def test_over_budget_keeps_findings_skips_verify_and_synth_llm(self):
        """검토 후 예산 소진 → 문맥검증·합성 LLM 생략, 지적은 유지(결정론 초안)."""
        state = {"over": False}

        class FlipMeter(Meter):
            def chat_json(self, *, model, prompt, schema, **kw):
                r = super().chat_json(model=model, prompt=prompt, schema=schema, **kw)
                state["over"] = True   # 첫 콜(통합검토) 후 예산 소진 시뮬레이션
                return r

        m = FlipMeter(flag_item="C1")
        wr = _orch(m).written_review("용역", DOC, should_stop=lambda: state["over"])
        self.assertEqual(m.calls, ["AxisResult"])  # verify·synth LLM 0회
        self.assertEqual([f.item_id for f in wr.confirmed], ["C1"])  # 지적 유지
        self.assertIsNotNone(wr.opinion)           # 결정론 초안으로 즉시 반환
        self.assertTrue(any("시간 예산" in c.reason for c in wr.context_checks))


class TestRuleTriage(unittest.TestCase):
    def test_no_signal_axis_skipped_with_reason(self):
        m = Meter()
        doc = "용역 규정 근거\n예산 합계 100원\n계약 수의\n서류 요건"  # 일정 신호 없음
        report = _orch(m).self_check("용역", doc)
        skipped_names = " ".join(n for n, _ in report.skipped_axes)
        self.assertIn("3.", skipped_names)   # 일정 신호 없음 → 미검토
        self.assertIn("규칙 선별", report.skipped_axes[-1][1])

    def test_weak_signal_falls_back_to_all_axes(self):
        m = Meter()
        report = _orch(m).self_check("용역", "예산 합계 100원")  # 신호 1축뿐
        self.assertEqual(report.skipped_axes, [])  # 전축 폴백(좁히지 않음)


class TestDigest(unittest.TestCase):
    def test_short_doc_untouched(self):
        self.assertEqual(build_review_digest("짧은 문서", cap=100), "짧은 문서")

    def test_long_doc_keeps_money_and_markers(self):
        noise = "\n".join("이 줄은 검토와 무관한 서술형 잡문이며 신호가 전혀 없다구요 하하 호호" + str(i) * 30
                          for i in range(200))
        doc = ("[문서: 요청서.hwp]\n사업명: 플랫폼 구축\n" + noise
               + "\n합계: 310,000,000원\n[문서: 내역서.xlsx]\n소계: 100원")
        out = build_review_digest(doc, cap=2000)
        self.assertLess(len(out), len(doc))
        self.assertIn("[문서: 요청서.hwp]", out)
        self.assertIn("[문서: 내역서.xlsx]", out)
        self.assertIn("310,000,000원", out)
        self.assertIn("발췌 검토", out)          # 생략 명시(침묵 금지)


if __name__ == "__main__":
    unittest.main()
