"""IntentClassifier — 라우팅 명세 케이스 (2026-07-15 사용자 명세)."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.rules.intent import classify_intent


def intent_of(msg, **kw):
    return classify_intent(msg, **kw).intent


class TestIntent(unittest.TestCase):
    # ── 명세 규칙 1: '검토' 시작이어도 본문 신호 없으면 review 금지 ──
    def test_review_meta_question_not_review(self):
        self.assertEqual(intent_of("검토 절차가 어떻게 되나요?"), "audit_question")
        self.assertEqual(intent_of("검토 받으려면 뭘 준비해야 해요?"), "audit_question")

    def test_bare_review_command_asks_for_doc(self):
        # '검토' 단독은 명령 시도 — 문서 요청 안내 경로(document_review 빈손)
        it = classify_intent("검토")
        self.assertEqual(it.intent, "document_review")
        self.assertEqual(it.doc_score, 0)

    def test_review_with_real_doc(self):
        doc = "검토 일상감사 요청서\n사업명: 챗봇 구축\n추정금액: 85,000,000원\n" + "내용 " * 40
        it = classify_intent(doc)
        self.assertEqual(it.intent, "document_review")
        self.assertGreaterEqual(it.doc_score, 2)

    # ── 명세 규칙 2: 150자 이상이어도 문서 신호 약하면 질문 ──
    def test_long_general_question_is_question(self):
        msg = ("저희 부서에서 내년에 추진할 계획이 있는데 일상감사라는 것을 처음 들어봐서요. "
               "어떤 절차이고 우리가 뭘 준비해야 하는지, 기간은 얼마나 걸리는지 자세히 알려주실 수 "
               "있을까요? 부서장이 빨리 알아보라고 하셔서 문의드립니다. 처음이라 용어도 낯설고 어디부터 시작해야 할지 모르겠습니다. 잘 부탁드립니다.")
        self.assertGreaterEqual(len(msg), 150)
        self.assertEqual(intent_of(msg), "audit_question")

    def test_long_real_doc_is_review(self):
        doc = ("일상감사 요청서\n사업명: 스마트 안내 시스템 구축\n추정금액: 120,000,000원\n"
               "소계 100,000원 부가세 10,000원 합계 110,000원\n" + "과업 내용 " * 30)
        self.assertEqual(intent_of(doc), "document_review")

    # ── 명세 규칙 3: 유형 상담 → business_type_question ──
    def test_biztype_questions(self):
        self.assertEqual(intent_of("이 사업은 용역이야?"), "business_type_question")
        self.assertEqual(intent_of("서버 구매+설치는 물품이야 공사야?"), "business_type_question")
        self.assertEqual(intent_of("홈페이지 구축하는 건 무슨 유형이에요?"), "business_type_question")

    # ── 명세 규칙 4: 잡담·소관 밖 ──
    def test_greeting_and_out_of_scope(self):
        self.assertEqual(intent_of("안녕하세요"), "greeting")
        self.assertEqual(intent_of("오늘 점심 뭐 먹을까?"), "out_of_scope")
        self.assertEqual(intent_of("주말에 영화 볼 건데 추천해줘"), "out_of_scope")

    def test_scope_word_overrides_oos(self):
        # '계약' 등 업무 어휘가 있으면 소관 밖으로 빼지 않는다
        self.assertEqual(intent_of("행사 계약인데 날씨 때문에 연기되면 어떻게 해요?"), "audit_question")

    # ── 기존 명령·조회 경로 보존 ──
    def test_commands_preserved(self):
        self.assertEqual(intent_of("대장 2026"), "ledger")
        self.assertEqual(intent_of("기한 2026-07-06"), "deadline")
        self.assertEqual(intent_of("대상? 용역 3억"), "target_check")
        self.assertEqual(intent_of("법령 지방계약법 제22조"), "law_lookup")
        self.assertEqual(intent_of("수의계약 법령 찾아줘"), "law_lookup")
        self.assertEqual(intent_of("도움말"), "help")
        self.assertEqual(intent_of("이거 어떻게 써?"), "help")

    def test_files_always_review(self):
        self.assertEqual(intent_of("이 문서 봐줘", has_files=True), "document_review")

    def test_default_is_audit_question(self):
        self.assertEqual(intent_of("조치결과는 언제까지 내야 하나요?"), "audit_question")


if __name__ == "__main__":
    unittest.main()
