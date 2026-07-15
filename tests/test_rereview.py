"""회차 2 테스트 — 재심사 모드(SOP ②: 변경점만 검토, 전체 재검토 금지)."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.rules.cross_check import DocPart, split_bundle
from audit_core.rules.rereview import detect_rereview, diff_docs, format_rereview, has_marker

V1 = """제안요청서
사업명: AI 플랫폼 구축
직접경비: 클라우드 이용료 15,000,000원
여비: 150,000원*3인*10회
인쇄비: 1,200,000원
"""

V2 = """제안요청서
사업명: AI 플랫폼 구축
직접경비: SW라이선스 17,200,000원
여비: 150,000원*3인*5회
인쇄비: 1,200,000원
"""


class TestReReview(unittest.TestCase):
    def test_marker(self):
        self.assertTrue(has_marker("○○ 사업 재공고 안내"))
        self.assertTrue(has_marker("본문", filenames=["일상감사 조치결과 반영.pdf"]))
        self.assertFalse(has_marker("일반 검토 요청"))

    def test_diff_changed_only(self):
        rr = diff_docs(DocPart("제안요청서", V1), DocPart("제안요청서#2", V2))
        self.assertEqual(rr.n_changes, 4)  # 직접경비·여비 각 2줄(삭제+추가)
        added = " ".join(t for _n, t in rr.added)
        self.assertIn("SW라이선스", added)
        self.assertIn("5회", added)
        # 변경 조각은 원문보다 작고, 미변경 줄(인쇄비)은 앵커 목록에 없음
        self.assertLess(len(rr.changed_text), len(V2))
        self.assertNotIn("인쇄비", added)

    def test_detect_from_bundle_and_format(self):
        parts = split_bundle(V1 + V2)
        self.assertEqual(len(parts), 2)  # 제안요청서, 제안요청서#2
        rr = detect_rereview(parts)
        self.assertIsNotNone(rr)
        out = format_rereview(rr)
        self.assertIn("재심사 모드", out)
        self.assertIn("전체 재검토 금지", out)
        self.assertIn("행:", out)  # 발췌 앵커(줄번호)

    def test_no_pair_returns_none(self):
        parts = split_bundle("일상감사 요청서\n본문\n" + V1)
        self.assertIsNone(detect_rereview(parts))

    def test_identical_versions_not_rereview(self):
        self.assertIsNone(detect_rereview(split_bundle(V1 + V1)))


class TestChunkedReview(unittest.TestCase):
    """doc_sectioner v1 — 대형 문서 조각 검토·병합 (16K 컨텍스트 초과 대응)."""

    AXIS = {"axis": "A", "title": "테스트축",
            "items": [{"item_id": "A1", "question": "q1"}, {"item_id": "A2", "question": "q2"}]}

    def _reviewer(self, verdicts_per_chunk):
        """조각 i 호출에 verdicts_per_chunk[i]를 돌려주는 페이크."""
        from audit_core.agents.axis_reviewer import AxisReviewer
        from audit_core.agents.base import OllamaClient

        calls = []

        class Fake(OllamaClient):
            def __init__(self):
                pass

            def chat_json(self, *, model, prompt, schema, **kw):
                i = len(calls)
                calls.append(prompt)
                v = verdicts_per_chunk[min(i, len(verdicts_per_chunk) - 1)]
                return schema.model_validate({"axis": "A", "items": [
                    {"item_id": "A1", "verdict": v[0], "evidence": f"조각{i}", "severity": v[2]},
                    {"item_id": "A2", "verdict": v[1], "evidence": f"조각{i}", "severity": 1},
                ]})

        r = AxisReviewer(client=Fake(), model="m")
        r.CHUNK_CHARS = 100  # 테스트용 소형 임계
        return r, calls

    def test_split_and_merge_precedence(self):
        # 조각1: A1=UNABLE, A2=FLAG / 조각2 이후: A1=OK, A2=UNABLE
        r, calls = self._reviewer([("UNABLE", "FLAG", 1), ("OK", "UNABLE", 1)])
        doc = "\n".join(f"{i}줄 " + "값" * 20 for i in range(12))  # 100자 초과 → 분할
        res = r.review(self.AXIS, doc)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("문서 조각 1/", calls[0])
        by = {it.item_id: it for it in res.items}
        self.assertEqual(by["A1"].verdict, "OK")     # OK가 UNABLE보다 우선(오탐 억제)
        self.assertEqual(by["A2"].verdict, "FLAG")   # FLAG가 UNABLE보다 우선
        self.assertIn("조각 검토", by["A2"].evidence)  # 부분 문서 기준임을 명시

    def test_flag_takes_max_severity(self):
        r, _ = self._reviewer([("FLAG", "NA", 1), ("FLAG", "NA", 3)])
        doc = "\n".join("값" * 30 for _ in range(8))
        res = r.review(self.AXIS, doc)
        a1 = next(it for it in res.items if it.item_id == "A1")
        self.assertEqual(a1.severity, 3)

    def test_short_doc_single_call(self):
        r, calls = self._reviewer([("OK", "OK", 1)])
        r.review(self.AXIS, "짧은 문서")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("문서 조각", calls[0])


if __name__ == "__main__":
    unittest.main()
