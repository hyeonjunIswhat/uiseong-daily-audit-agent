"""골든셋 러너 — 실물 사건 자동 회귀 (결정론 전용, LLM 불필요).

golden/manifest.yaml의 사건마다:
  ① 대상판정 재현 — check_target(유형, 금액) == expected_target
  ② 문서 존재 확인 — docs의 각 폴더가 실재하고 파싱 가능한 파일이 있는지
  ③ 결정론 검산 — xlsx가 있으면 산출내역 검산(불일치는 manifest의 기대와 대조)
  ④ 미등록 사건 감지 — source_dir에 있는데 manifest에 없는 폴더를 안내

새 사건 추가 규약(운영) — **zip을 그대로 밀어 넣으면 된다**:
  1) 사건 zip(공문 시스템에서 받은 그대로)을 `AI_Workspace/일상감사 데이터/`에 복사
  2) 이 러너 실행 → zip을 자동으로 풀고(한글 파일명 CP949 대응, __MACOSX 등 제외),
     manifest 미등록이면 '편입 후보'로 안내
  3) `golden/manifest.yaml`의 cases에 한 블록 추가(id·유형·금액·expected_target·docs)
     후 재실행 → 전부 PASS면 편입 완료. 실제 의견서의 지적은 actual_findings에
     기록(향후 LLM 축 판정 일치율 측정의 정답 데이터)
  ※ F10(골든셋 부패) 방지: 감사관 결재를 거친 실물만 골든으로 삼는다.

실행: .venv/bin/python batch/golden_run.py
"""

import sys
import unicodedata
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audit_core.parsers.cost_xlsx import CostSheetError, check_cost_sheet  # noqa: E402
from audit_core.rules.target_check import TargetInput, TargetRuleSet, check_target  # noqa: E402

MANIFEST = ROOT / "golden/manifest.yaml"
PARSABLE = {".hwp", ".hwpx", ".xlsx", ".pdf"}


def _zip_junk(name: str) -> bool:
    base = Path(name).name
    return name.startswith("__MACOSX/") or base == ".DS_Store" or base.startswith("._")


def ingest_zips(src: Path) -> list[str]:
    """source_dir의 zip을 폴더로 자동 해제(이미 풀린 것은 건너뜀). 해제 목록 반환.

    - zip 이름(확장자 제외) = 사건 폴더명
    - 한글 파일명: UTF-8 플래그 zip은 그대로, 아니면 CP949로 해석
    - __MACOSX·.DS_Store·'._' 리소스 포크는 제외
    """
    done = []
    for z in sorted(src.glob("*.zip")):
        dest = src / z.stem
        if dest.exists():
            continue
        dest.mkdir()
        with zipfile.ZipFile(z, metadata_encoding="cp949") as zf:
            members = [n for n in zf.namelist() if not _zip_junk(n)]
            zf.extractall(dest, members=members)
        done.append(f"{z.name} → {dest.name}/ ({len(members)}개 파일)")
    return done


def main() -> int:
    m = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    src = (MANIFEST.parent / m["source_dir"]).resolve()
    for line in ingest_zips(src):
        print(f"📦 압축 해제: {line}")
    rules = TargetRuleSet()
    fails = 0

    print(f"골든셋 회귀 — {MANIFEST.name} (사건 {len(m['cases'])}건, 원천 {src})\n")
    known_dirs: set[str] = set()
    for case in m["cases"]:
        cid = case["id"]
        # ① 대상판정 재현
        d = check_target(TargetInput(biz_type=case["biz_type"], amount=case["amount"]), rules)
        ok = d.decision == case["expected_target"]
        fails += 0 if ok else 1
        print(f"[{cid}] 판정 {'PASS' if ok else 'FAIL'} — {case['biz_type']} "
              f"{case['amount']:,}원 → {d.decision} (기대 {case['expected_target']})")

        # ② 문서 존재 + ③ xlsx 검산
        for stage, rel in (case.get("docs") or {}).items():
            # macOS 파일명은 NFD, YAML 문자열은 NFC — 정규화 후 대조
            known_dirs.add(unicodedata.normalize("NFC", rel.strip("/")))
            folder = src / rel
            if not folder.is_dir():
                fails += 1
                print(f"  ✗ {stage}: 폴더 없음 — {folder}")
                continue
            files = [f for f in folder.iterdir() if f.suffix.lower() in PARSABLE]
            print(f"  ✓ {stage}: {len(files)}개 문서")
            for x in (f for f in files if f.suffix.lower() == ".xlsx"):
                try:
                    r = check_cost_sheet(str(x))
                    bad = [c for c in r.checks if not c.match]
                    mark = "✓" if not bad else "✗"
                    if bad:
                        fails += 1
                    print(f"    {mark} 검산 {x.name}: {len(r.checks)}건 중 불일치 {len(bad)}건")
                except CostSheetError as e:
                    print(f"    ⚠ 검산 불가 {x.name}: {e}")

    # ④ 미등록 사건 감지
    unknown = [p.name for p in src.iterdir()
               if p.is_dir()
               and unicodedata.normalize("NFC", p.name) not in known_dirs]
    if unknown:
        print(f"\n📥 manifest 미등록 폴더 {len(unknown)}건 — 골든 편입 후보:")
        for name in unknown:
            print(f"  - {name}")
        print("  → golden/manifest.yaml cases에 블록을 추가하고 이 러너를 다시 실행하세요.")

    print(f"\n결과: {'전부 PASS ✅' if fails == 0 else f'FAIL {fails}건 ❌'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
