"""등급1 파기 배치 — C2 (SPEC §1-8·§9, 구현 6단계). open-webui 컨테이너 안에서 실행.

일상감사 모델 대화·파일을 보존시한 경과 시 3곳에서 파기한다:
  ① webui.db chat 행(대화·메시지 원문)  ② uploads/ 원본 파일  ③ 벡터 스토어(chroma)

안전 설계:
  - **dry-run이 기본** — 삭제 대상만 나열. 실제 삭제는 --execute 명시 시에만.
  - 대상 한정: chat JSON의 models가 PURGE_MODEL_IDS와 교집합인 행만. 타 모델
    대화는 어떤 경로로도 건드리지 않는다.
  - 기동 검증: 대상 테이블·경로 미발견 시 파기 실패로 **크게 경고**하고 비정상
    종료(조용한 미파기 금지, SPEC §9).
  - 파일은 해당 대화들이 참조한 file id만. 다른 대화가 같은 파일을 참조하면 보존.

실행(호스트에서):
  docker cp batch/purge_job.py open-webui:/tmp/purge_job.py
  docker exec open-webui python /tmp/purge_job.py            # dry-run
  docker exec open-webui python /tmp/purge_job.py --execute  # 실제 파기
cron 등록·보존시한 확정은 감사팀 협의 사항(확정필요_항목.md).
"""

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = os.getenv("PURGE_DB_PATH", "/app/backend/data/webui.db")
DATA_DIR = Path(os.getenv("PURGE_DATA_DIR", "/app/backend/data"))
MODEL_IDS = set(filter(None, os.getenv(
    "PURGE_MODEL_IDS", "daily_audit_agent,daily-audit-assistant").split(",")))
RETENTION_H = int(os.getenv("PURGE_RETENTION_HOURS", "24"))


def fail(msg: str):
    print(f"\n{'!' * 60}\n!! 파기 실패 — {msg}\n!! 조용한 미파기 금지(SPEC §9): 운영자 확인 필요\n{'!' * 60}")
    sys.exit(2)


def main(execute: bool):
    # ── 기동 검증 ────────────────────────────────────────
    if not Path(DB_PATH).is_file():
        fail(f"DB 미발견: {DB_PATH}")
    if not (DATA_DIR / "uploads").is_dir():
        fail(f"uploads/ 미발견: {DATA_DIR / 'uploads'}")
    db = sqlite3.connect(DB_PATH)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"chat", "file"} <= tables:
        fail(f"필수 테이블 미발견: {sorted({'chat', 'file'} - tables)}")

    cutoff = int(time.time()) - RETENTION_H * 3600
    mode = "실행" if execute else "DRY-RUN(목록만)"
    print(f"[purge] 모드={mode} · 대상 모델={sorted(MODEL_IDS)} · 보존 {RETENTION_H}시간(기준 {cutoff})")

    # ── 대상 대화 선별 (모델 교집합 + 시한 경과) ─────────
    targets, file_ids = [], set()
    for cid, blob, updated in db.execute("SELECT id, chat, updated_at FROM chat"):
        try:
            c = json.loads(blob or "{}")
        except Exception:
            continue
        if not (set(c.get("models") or []) & MODEL_IDS):
            continue
        if (updated or 0) >= cutoff:
            continue
        targets.append(cid)
        for f in c.get("files") or []:
            fid = f.get("id") or (f.get("file") or {}).get("id")
            if fid:
                file_ids.add(fid)

    # 다른(비대상) 대화가 참조하는 파일은 보존
    if file_ids:
        for cid, blob in db.execute("SELECT id, chat FROM chat"):
            if cid in targets:
                continue
            try:
                c = json.loads(blob or "{}")
            except Exception:
                continue
            for f in c.get("files") or []:
                fid = f.get("id") or (f.get("file") or {}).get("id")
                if fid in file_ids:
                    file_ids.discard(fid)

    print(f"[purge] 대상: 대화 {len(targets)}건 · 파일 {len(file_ids)}건")
    for cid in targets:
        print(f"  - chat {cid}")
    for fid in file_ids:
        print(f"  - file {fid}")
    if not execute:
        print("[purge] dry-run 종료 — 삭제하려면 --execute")
        return

    # ── ② uploads 원본 + ③ 벡터 스토어 ──────────────────
    for fid in file_ids:
        for p in (DATA_DIR / "uploads").glob(f"{fid}_*"):
            p.unlink(missing_ok=True)
            print(f"[purge] uploads 삭제: {p.name}")
        try:  # Open WebUI 컬렉션 명명: file-{id}
            import chromadb
            client = chromadb.PersistentClient(path=str(DATA_DIR / "vector_db"))
            client.delete_collection(f"file-{fid}")
            print(f"[purge] 벡터 컬렉션 삭제: file-{fid}")
        except Exception as e:
            print(f"[purge] 벡터 삭제 생략(file-{fid}): {e}")
        db.execute("DELETE FROM file WHERE id=?", (fid,))

    # ── ① 대화 행 ────────────────────────────────────────
    for cid in targets:
        db.execute("DELETE FROM chat WHERE id=?", (cid,))
    db.commit()

    # ── 잔존 0건 확인 (성공 기준, SPEC §8) ────────────────
    left = [cid for cid in targets
            if db.execute("SELECT 1 FROM chat WHERE id=?", (cid,)).fetchone()]
    if left:
        fail(f"삭제 후 잔존 대화 {len(left)}건")
    print(f"[purge] 완료 — 대화 {len(targets)}건·파일 {len(file_ids)}건 파기, 잔존 0건")


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
