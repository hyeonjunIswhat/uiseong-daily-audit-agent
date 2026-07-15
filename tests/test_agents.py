"""구현 3단계 검증: agents/base.py, agents/law_fetcher.py (모의 전송 주입).

실제 Ollama·법령 API 연동은 tests/smoke_step3.py로 별도 확인.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel

from audit_core.agents.base import (
    NUM_PREDICT_FLOOR,
    LLMUnavailable,
    OllamaClient,
    SchemaValidationError,
)
from audit_core.agents.law_fetcher import (
    KST,
    LawFetcher,
    LawFetchError,
    normalize_article,
)


class Verdict(BaseModel):
    verdict: str
    reason: str


def ollama_reply(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content}}


class TestOllamaClient(unittest.TestCase):
    def test_valid_first_try(self):
        calls = []

        def fake_post(url, payload, timeout):
            calls.append(payload)
            return ollama_reply('{"verdict": "OK", "reason": "이상 없음"}')

        c = OllamaClient(post_fn=fake_post)
        out = c.chat_json(model="m", prompt="p", schema=Verdict)
        self.assertEqual(out.verdict, "OK")
        p = calls[0]
        self.assertIs(p["think"], False)                     # qwen3 thinking 억제
        self.assertEqual(p["options"]["temperature"], 0.0)   # 재현성
        self.assertIn("seed", p["options"])
        self.assertEqual(p["format"]["type"], "object")      # 스키마 강제

    def test_num_predict_floor(self):
        captured = {}

        def fake_post(url, payload, timeout):
            captured.update(payload)
            return ollama_reply('{"verdict": "OK", "reason": "r"}')

        OllamaClient(post_fn=fake_post).chat_json(
            model="m", prompt="p", schema=Verdict, num_predict=8
        )
        self.assertEqual(captured["options"]["num_predict"], NUM_PREDICT_FLOOR)

    def test_retry_once_then_success(self):
        replies = [ollama_reply("스키마 아님"), ollama_reply('{"verdict": "FLAG", "reason": "r"}')]
        calls = []

        def fake_post(url, payload, timeout):
            calls.append(payload)
            return replies[len(calls) - 1]

        out = OllamaClient(post_fn=fake_post).chat_json(model="m", prompt="p", schema=Verdict)
        self.assertEqual(out.verdict, "FLAG")
        self.assertEqual(len(calls), 2)
        # 재시도 메시지에 원 출력과 오류 안내 포함
        self.assertIn("스키마", calls[1]["messages"][-1]["content"])

    def test_two_failures_raise(self):
        def fake_post(url, payload, timeout):
            return ollama_reply("여전히 스키마 아님")

        with self.assertRaises(SchemaValidationError):
            OllamaClient(post_fn=fake_post).chat_json(model="m", prompt="p", schema=Verdict)

    def test_think_unsupported_fallback(self):
        calls = []

        def fake_post(url, payload, timeout):
            calls.append(payload)
            if "think" in payload:
                raise LLMUnavailable('Ollama HTTP 400: "m" does not support thinking')
            return ollama_reply('{"verdict": "OK", "reason": "r"}')

        out = OllamaClient(post_fn=fake_post).chat_json(model="m", prompt="p", schema=Verdict)
        self.assertEqual(out.verdict, "OK")
        self.assertNotIn("think", calls[-1])


def make_law_api(effective="20240217", text22="물가 변동 조항 본문"):
    """lawSearch/lawService 모의 응답."""
    def fake_get(url, timeout):
        if "lawSearch.do" in url:
            return {"LawSearch": {"law": [{
                "법령명한글": "지방자치단체를 당사자로 하는 계약에 관한 법률",
                "현행연혁코드": "현행",
                "법령일련번호": "253973",
            }]}}
        return {"법령": {
            "기본정보": {"법령명_한글": "지방자치단체를 당사자로 하는 계약에 관한 법률", "시행일자": effective},
            "조문": {"조문단위": [
                {"조문여부": "전문", "조문번호": "1", "조문내용": "전문부 — 제외 대상"},
                {"조문여부": "조문", "조문번호": "22", "조문내용": "제22조(물가 변동 등)",
                 "항": [{"항번호": "①", "항내용": text22}]},
                {"조문여부": "조문", "조문번호": "22", "조문가지번호": "2",
                 "조문내용": "제22조의2(가지조문)"},
            ]},
        }}
    return fake_get


class TestLawFetcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, **kw):
        kw.setdefault("oc", "test_oc")
        kw.setdefault("cache_dir", self.tmp.name)
        kw.setdefault("get_fn", make_law_api())
        return LawFetcher(**kw)

    def test_normalize_article(self):
        self.assertEqual(normalize_article("22조"), ("제22조", "22", ""))
        self.assertEqual(normalize_article("제22조의2"), ("제22조의2", "22", "2"))
        with self.assertRaises(ValueError):
            normalize_article("조문아님")

    def test_fetch_api_and_cache(self):
        f = self.fetcher()
        a1 = f.fetch("지방계약법", "제22조")
        self.assertEqual(a1.source, "api")
        self.assertEqual(a1.effective_date, "2024-02-17")
        self.assertIn("물가 변동 조항 본문", a1.text)
        self.assertTrue((Path(self.tmp.name) / "지방계약법_제22조.json").exists())
        # 두 번째 조회는 캐시 히트 + hit_count 증가
        a2 = f.fetch("지방계약법", "제22조")
        self.assertEqual(a2.source, "cache")
        self.assertEqual(a2.hit_count, 2)

    def test_branch_article(self):
        a = self.fetcher().fetch("지방계약법", "제22조의2")
        self.assertIn("가지조문", a.text)

    def test_revision_archives_old(self):
        f = self.fetcher(ttl_days=0)  # 즉시 만료 → 매번 재검증
        f.fetch("지방계약법", "제22조")
        f2 = self.fetcher(ttl_days=0, get_fn=make_law_api(effective="20260101", text22="개정 본문"))
        a = f2.fetch("지방계약법", "제22조")
        self.assertEqual(a.effective_date, "2026-01-01")
        archived = list(Path(self.tmp.name).glob("*_archived_2024-02-17.json"))
        self.assertEqual(len(archived), 1)  # 구버전 보관

    def test_cache_only_mode(self):
        self.fetcher().fetch("지방계약법", "제22조")  # 캐시 적재
        offline = self.fetcher(oc="", get_fn=None)
        self.assertTrue(offline.cache_only)
        a = offline.fetch("지방계약법", "제22조")
        self.assertIn(a.source, ("cache", "cache_stale"))
        with self.assertRaises(LawFetchError):
            offline.fetch("지방계약법", "제99조")  # 캐시에 없으면 실패

    def test_api_failure_degrades_to_stale_cache(self):
        f = self.fetcher(ttl_days=0)
        f.fetch("지방계약법", "제22조")

        def broken(url, timeout):
            raise LawFetchError("네트워크 차단")

        f2 = self.fetcher(ttl_days=0, get_fn=broken)
        a = f2.fetch("지방계약법", "제22조")
        self.assertEqual(a.source, "cache_stale")

    def test_exists_for_verifier(self):
        f = self.fetcher()
        self.assertTrue(f.exists("지방계약법-제22조"))
        self.assertFalse(f.exists("지방계약법-제999조"))
        self.assertFalse(f.exists("형식오류"))

    def test_reproducibility_3x(self):
        f = self.fetcher()
        texts = {f.fetch("지방계약법", "제22조").text for _ in range(3)}
        self.assertEqual(len(texts), 1)


class TestRubricIntegrity(unittest.TestCase):
    def test_rubric_loads_and_ids_unique(self):
        p = PROJECT_ROOT / "audit_core/rubric/rubric_v0_1.json"
        rubric = json.loads(p.read_text(encoding="utf-8"))
        self.assertTrue(rubric["provisional"])
        axes = rubric["axes"]
        self.assertEqual([a["axis"] for a in axes], ["A", "B", "C", "D", "E"])
        ids = [i["item_id"] for a in axes for i in a["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for a in axes:
            for i in a["items"]:
                self.assertIn(i["weight"], (1, 2, 3))
                self.assertTrue(i["item_id"].startswith(a["axis"]))


if __name__ == "__main__":
    unittest.main()
