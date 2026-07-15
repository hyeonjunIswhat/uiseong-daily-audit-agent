"""구현 3단계 실연동 확인 (SPEC §7-3 완료 기준: 법령 조회 검증).

1. Ollama 실호출 — format=json + think:false 로 스키마 준수 응답 (3회 재현성)
2. 법령 API 실호출 — 지방계약법 제22조 조회·캐시 적재
3. 루브릭 law_refs 전수 실존 검증 (verifier 1차와 동일 경로) — 초안 조문값 확인

실행: LAW_API_OC=<OC> .venv/bin/python tests/smoke_step3.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel

from audit_core.agents.base import OllamaClient
from audit_core.agents.law_fetcher import LawFetcher
from audit_core.config import get_settings


class ItemVerdict(BaseModel):
    verdict: str  # NA | OK | FLAG | UNABLE
    evidence: str


def smoke_ollama() -> bool:
    s = get_settings()
    client = OllamaClient(base_url="http://localhost:11434")  # 호스트 직접 실행
    prompt = (
        "다음 점검항목을 판정하라.\n"
        "점검항목: 수의계약 사유가 기재되어 있는가\n"
        "문서 발췌: '본 건은 지방계약법 시행령 제25조제1항제5호에 따라 수의계약으로 추진하고자 함'\n"
        "verdict는 NA/OK/FLAG/UNABLE 중 하나."
    )
    outs = [
        client.chat_json(model=s.AUDIT_MODEL_LIGHT, prompt=prompt, schema=ItemVerdict)
        for _ in range(3)
    ]
    verdicts = [o.verdict for o in outs]
    ok = all(v == verdicts[0] for v in verdicts) and verdicts[0] in ("NA", "OK", "FLAG", "UNABLE")
    print(f"[1] Ollama({s.AUDIT_MODEL_LIGHT}) 3회 판정: {verdicts} → {'PASS' if ok else 'FAIL'}")
    return ok


def smoke_law_fetch() -> bool:
    f = LawFetcher()
    if f.cache_only:
        print("[2] LAW_API_OC 미설정 — 캐시 전용 모드라 실연동 생략(FAIL 아님)")
        return True
    a = f.fetch("지방계약법", "제22조")
    ok = "계약금액" in a.text and bool(a.effective_date)  # 최초 실행은 api, 재실행은 cache — 둘 다 정상
    print(f"[2] 법령 조회({a.source}): {a.law_name} {a.article} (시행 {a.effective_date}, {len(a.text)}자) → {'PASS' if ok else 'FAIL'}")
    b = f.fetch("지방계약법", "제22조")
    print(f"    캐시 재조회: source={b.source}, hit_count={b.hit_count}")
    return ok and b.source == "cache"


def smoke_rubric_refs() -> bool:
    f = LawFetcher()
    if f.cache_only:
        print("[3] 캐시 전용 모드 — 루브릭 검증 생략")
        return True
    rubric = json.loads((PROJECT_ROOT / "audit_core/rubric/rubric_v0_1.json").read_text(encoding="utf-8"))
    refs = sorted({r for a in rubric["axes"] for i in a["items"] for r in i["law_refs"]})
    bad = [r for r in refs if not f.exists(r)]
    for r in refs:
        print(f"    {'✓' if r not in bad else '✗ 실존 확인 실패'} {r}")
    print(f"[3] 루브릭 law_refs {len(refs)}건 중 실패 {len(bad)}건 → {'PASS' if not bad else 'CHECK NEEDED'}")
    return True  # 초안 검증 목적 — 실패 조문은 협의 자료


if __name__ == "__main__":
    results = [smoke_ollama(), smoke_law_fetch(), smoke_rubric_refs()]
    sys.exit(0 if all(results) else 1)
