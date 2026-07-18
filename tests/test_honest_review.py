"""2026-07-15 실장애 회귀 — 검토 실패를 '이상 없음'으로 은폐하던 경로들.

실사례: 스카이디펜스런 용역 제안요청서 + GPU 서버 구매(물품) 3건을 한 번에
첨부 → 한 사업으로 병합 검토, 전축 '이상 없습니다', 34개 전부 판단 불가,
모델 실패를 서류 부족으로 안내. 각 결함의 재발 방지 테스트.
"""

import re
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "pipelines"))

from audit_core.agents.axis_reviewer import AxisReviewer, Rubric
from audit_core.agents.base import LLMUnavailable, OllamaClient, SchemaValidationError
from audit_core.orchestrator import Orchestrator, format_self_check
from audit_core.rules.bundle import format_split_report, group_projects, profile_file
from audit_core.rules.completeness import RequiredDocs
from audit_core.rules.digest import build_review_digest

SKY = ("제 안 요 청 서\n1. 사 업 명: 의성 스카이 디펜스 런 행사 대행 용역\n"
       "2. 사업기간: 계약일로부터 5개월\n3. 소요예산: 190,000,000원\n"
       "협상에 의한 계약으로 낙찰자를 결정한다\n" + "과업 상세. " * 50)
GPU = ("납품조건 및 규격서\n1. 사 업 명 : 의성군 인공지능 GPU 서버 구매\n"
       "2. 규격: GPU 서버 1식(ASUS ESC8000-E12 동급)\n3. 소요예산: 140,000,000원\n"
       + "납품 조건 상세. " * 50)


class TestBundleSplit(unittest.TestCase):
    def test_two_projects_are_separated_with_evidence(self):
        profs = [profile_file("(제안요청서)스카이디펜스런.hwpx", SKY),
                 profile_file("납품조건 및 규격서.hwp", GPU),
                 profile_file("일상감사요청서(의성군 인공지능 GPU 서버 구매).hwp", "")]
        groups = group_projects(profs)
        self.assertEqual(len(groups), 2)
        report = format_split_report(groups)
        self.assertIn("검토를 시작하지 않았습니다", report)
        self.assertIn("스카이", report)
        self.assertIn("GPU", report)
        self.assertIn("행 「", report)          # 근거 위치(줄번호·원문) 포함
        self.assertIn("다음 조치", report)

    def test_single_project_absorbs_unnamed_files(self):
        profs = [profile_file("규격서.hwp", GPU),
                 profile_file("공문.pdf", "다시 도약하는 대한민국\n수신 내부결재")]
        groups = group_projects(profs)
        self.assertEqual(len(groups), 1)        # 사업명 미상 공문은 단일 사업에 흡수
        self.assertEqual(len(groups[0]), 2)

    def test_doc_type_word_in_paren_is_not_project_name(self):
        p = profile_file("3. (제안요청서)의성스카이디펜스런 행사 대행 용역.hwpx", SKY)
        self.assertNotIn("제안요청서", p.candidates)  # 유형 어휘는 사업명 후보 아님
        self.assertTrue(any("스카이" in c for c in p.candidates))


class _AllUnableClient(OllamaClient):
    """모델이 전 항목 판정을 회신하지 못하는 상황(타임아웃) 모의."""
    def __init__(self):
        self.calls = 0

    def chat_json(self, *, model, prompt, schema, **kw):
        self.calls += 1
        raise LLMUnavailable("The read operation timed out")


class _AllOkClient(OllamaClient):
    def __init__(self):
        pass

    def chat_json(self, *, model, prompt, schema, **kw):
        ids = re.findall(r"^- ([A-Za-z0-9]\w*(?:-\d+)?): ", prompt, re.M)
        return schema.model_validate({"axis": "ALL", "items": [
            {"item_id": i, "verdict": "OK", "evidence": "확인", "severity": 1} for i in ids]})


class _NoLaw:
    def fetch_ref(self, ref):
        raise RuntimeError("비활성")


DOC = ("정보화 용역 요청서\n관련 법령 근거\n예산 합계 100원\n사업기간 6개월\n"
       "계약방법 협상\n서류 요건 확인\n담당 부서 명시\n")


class TestHonestNarration(unittest.TestCase):
    def _run(self, client):
        msgs = []
        orch = Orchestrator(reviewer=AxisReviewer(client=client), rubric=Rubric(),
                            law_fetcher=_NoLaw())
        report = orch.self_check("용역", DOC, progress=msgs.append)
        return report, msgs

    def test_model_failure_never_says_clean(self):
        report, msgs = self._run(_AllUnableClient())
        axis_done = [m for m in msgs if "검토를 마쳤습니다" in m]
        self.assertTrue(axis_done)
        self.assertTrue(all("이상 없" not in m for m in axis_done))   # 은폐 금지
        self.assertTrue(all("근거를 찾지 못했습니다" in m for m in axis_done))
        # 리포트도 모델 실패를 서류 부족으로 설명하지 않는다
        text = format_self_check(report)
        self.assertIn("서류 문제가 아닙니다", text)
        self.assertIn("판정을 받지 못했습니다", text)
        self.assertIn("AI 미회신", text)

    def test_ok_verdicts_still_say_clean(self):
        _report, msgs = self._run(_AllOkClient())
        axis_done = [m for m in msgs if "검토를 마쳤습니다" in m]
        self.assertTrue(all("이상 없습니다 ✓" in m for m in axis_done))


class TestEvidenceMissing(unittest.TestCase):
    def test_axis_without_required_docs_is_premarked_not_sent_to_llm(self):
        sent_ids = []

        class Spy(_AllOkClient):
            def chat_json(self, *, model, prompt, schema, **kw):
                sent_ids.extend(re.findall(r"^- ([A-Za-z0-9]\w*(?:-\d+)?): ", prompt, re.M))
                return super().chat_json(model=model, prompt=prompt, schema=schema, **kw)

        orch = Orchestrator(reviewer=AxisReviewer(client=Spy()), rubric=Rubric(),
                            law_fetcher=_NoLaw())
        # 산출내역서 없음 → 축2(타당성·원가) 항목은 사전 분류·LLM 미투입
        report = orch.self_check("물품", DOC, evidence_docs={"규격서", "일상감사요청서"})
        from audit_core.orchestrator import unable_causes
        causes = unable_causes(report)
        ev = causes["EVIDENCE_MISSING"]
        self.assertTrue(ev)                       # 축2 항목들이 근거 미첨부로 분류
        ev_ids = {it.item_id for _a, it in ev}
        self.assertFalse(ev_ids & set(sent_ids))  # LLM에 보내지 않음
        text = format_self_check(report)
        self.assertIn("필요 서류 없음", text)
        self.assertIn("산출내역서", text)          # 무엇이 없는지 명시
        self.assertIn("단가·수량·산식", text)      # 무엇을 확인 못 하는지 명시

    def test_without_evidence_info_behavior_unchanged(self):
        orch = Orchestrator(reviewer=AxisReviewer(client=_AllOkClient()), rubric=Rubric(),
                            law_fetcher=_NoLaw())
        report = orch.self_check("물품", DOC)      # evidence_docs=None → 사전 분류 없음
        from audit_core.orchestrator import unable_causes
        self.assertFalse(unable_causes(report)["EVIDENCE_MISSING"])


class TestPerDocDigest(unittest.TestCase):
    def test_second_document_survives_global_cap(self):
        noise = "\n".join("서술형 잡문 줄입니다 신호 없음 요건도아님" + str(i) * 40 for i in range(300))
        doc = (f"[문서: 제안요청서.hwpx]\n{noise}\n"
               f"[문서: 규격서.hwp]\n사업명: GPU 서버 구매\n합계: 140,000,000원")
        out = build_review_digest(doc, cap=4000)
        self.assertIn("[문서: 규격서.hwp]", out)
        self.assertIn("140,000,000원", out)      # 뒤 문서의 금액이 살아남는다


class TestCompletenessFilename(unittest.TestCase):
    def test_attached_file_not_reported_as_missing(self):
        doc = "일상감사 요청서\n첨부: 제안요청서 1부, 산출내역서 1부\n" + "본문. " * 60
        hits = RequiredDocs().detect(doc, filenames=["3. (제안요청서)스카이디펜스런.hwpx"])
        rfp = next(h for h in hits if "제안요청서" in h.label)
        self.assertNotIn("첨부되지 않았습니다", rfp.source)  # 실제로 첨부됐다
        self.assertIn("첨부 확인", rfp.source)


class TestWallClockDeadline(unittest.TestCase):
    def test_retries_do_not_exceed_call_budget(self):
        calls = {"n": 0}

        def slow_bad_post(url, payload, timeout_s):
            calls["n"] += 1
            time.sleep(0.6)                      # 모의 지연 — 실모델 불필요
            return {"message": {"content": "스키마 불일치 응답"}}

        from pydantic import BaseModel

        class S(BaseModel):
            x: int

        client = OllamaClient(base_url="http://test", post_fn=slow_bad_post)
        t0 = time.time()
        with self.assertRaises(SchemaValidationError):
            client.chat_json(model="m", prompt="p", schema=S, timeout_s=1)
        self.assertEqual(calls["n"], 1)          # 예산(1초) 소진 → 재시도 없음
        self.assertLess(time.time() - t0, 2.0)   # 월클럭이 소켓이 아니라 총 시간을 제한


if __name__ == "__main__":
    unittest.main()
