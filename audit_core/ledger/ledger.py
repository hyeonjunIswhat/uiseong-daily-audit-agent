"""일상감사처리대장 (기능 #11, 군 규칙 §22⑥). LLM 미관여.

등급 1 원문은 기록하지 않는다 — 건명 수준·금액 구간값·판정 코드만 (SPEC §6).
저장: LEDGER_DIR/ledger_{연도}.json (해당 연도 행 전체를 담은 JSON 배열).
"""

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from audit_core.config import get_settings
from audit_core.rules.deadline import HolidayCalendar, compute_deadline

RESULT_CODES = ("의견통보", "이상없음", "재검토", "실지감사전환")

AMOUNT_BANDS = [
    (50_000_000, "5천만 미만"),
    (100_000_000, "5천만~1억"),
    (200_000_000, "1억~2억"),
    (500_000_000, "2억~5억"),
    (1_000_000_000, "5억~10억"),
]

FIELD_LABELS = {
    "entry_no": "접수번호",
    "dept": "의뢰부서",
    "title": "건명",
    "biz_type": "사업유형",
    "amount_band": "금액대",
    "receipt_date": "접수일",
    "notify_due": "통보기한",
    "notified_date": "통보일",
    "result_code": "처리결과",
    "action_report_date": "조치결과통보일",
}


def amount_band(amount: int) -> str:
    """정확 금액 미기록 원칙 — 구간값으로 변환 (SPEC §6)."""
    for upper, label in AMOUNT_BANDS:
        if amount < upper:
            return label
    return "10억 이상"


@dataclass
class LedgerEntry:
    entry_no: str                  # 2026-017
    dept: str
    title: str                     # 건명 수준만 (등급1 원문 금지)
    biz_type: str
    amount_band: str
    receipt_date: str              # ISO
    notify_due: str                # ISO, deadline.py 산출
    notified_date: str = ""
    result_code: str = ""
    action_report_date: str = ""
    meta: dict = field(default_factory=dict)  # 판정 코드 등 code_only 메타


class Ledger:
    def __init__(self, ledger_dir: str | Path | None = None):
        self.dir = Path(ledger_dir or get_settings().LEDGER_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, year: int) -> Path:
        return self.dir / f"ledger_{year}.json"

    def _load(self, year: int) -> list[dict]:
        p = self._path(year)
        if not p.exists():
            return []
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, year: int, rows: list[dict]) -> None:
        p = self._path(year)
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        tmp.replace(p)

    def create(
        self,
        dept: str,
        title: str,
        biz_type: str,
        amount: int,
        receipt_date: date,
        calendar: HolidayCalendar | None = None,
    ) -> LedgerEntry:
        """접수 행 생성 — 접수번호 자동 채번, 통보기한 자동 산출."""
        year = receipt_date.year
        rows = self._load(year)
        seq = max((int(r["entry_no"].split("-")[1]) for r in rows), default=0) + 1
        d_notify = get_settings().deadline_days()[0]
        due = compute_deadline(receipt_date, d_notify, calendar=calendar)
        entry = LedgerEntry(
            entry_no=f"{year}-{seq:03d}",
            dept=dept.strip(),
            title=title.strip(),
            biz_type=biz_type.strip(),
            amount_band=amount_band(amount),
            receipt_date=receipt_date.isoformat(),
            notify_due=due.due.isoformat(),
        )
        rows.append(asdict(entry))
        self._save(year, rows)
        return entry

    def update(self, entry_no: str, **fields) -> LedgerEntry:
        """통보일·처리결과·조치결과통보일 등 후속 기재."""
        allowed = {"notified_date", "result_code", "action_report_date", "meta"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"수정 불가 필드: {sorted(bad)} (허용: {sorted(allowed)})")
        code = fields.get("result_code")
        if code and code not in RESULT_CODES:
            raise ValueError(f"처리결과 코드는 {RESULT_CODES} 중 하나여야 함: {code!r}")

        year = int(entry_no.split("-")[0])
        rows = self._load(year)
        for r in rows:
            if r["entry_no"] == entry_no:
                r.update(fields)
                self._save(year, rows)
                return LedgerEntry(**r)
        raise KeyError(f"접수번호 없음: {entry_no}")

    def get(self, entry_no: str) -> LedgerEntry | None:
        year = int(entry_no.split("-")[0])
        for r in self._load(year):
            if r["entry_no"] == entry_no:
                return LedgerEntry(**r)
        return None

    def list_year(self, year: int) -> list[LedgerEntry]:
        return [LedgerEntry(**r) for r in self._load(year)]

    def overdue(self, year: int, today: date) -> list[LedgerEntry]:
        """통보기한 경과·미통보 건 (기한 관리 알림용)."""
        return [
            e for e in self.list_year(year)
            if not e.notified_date and date.fromisoformat(e.notify_due) < today
        ]

    # ── 출력 (감사팀 수기 대장 대체) ─────────────────────────

    def export_csv(self, year: int, out_path: str | Path) -> Path:
        out = Path(out_path)
        cols = list(FIELD_LABELS)
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([FIELD_LABELS[c] for c in cols])
            for e in self.list_year(year):
                row = asdict(e)
                w.writerow([row[c] for c in cols])
        return out

    def export_xlsx(self, year: int, out_path: str | Path) -> Path:
        """xlsx 출력. 서식(수기 대장 양식 매핑)은 감사팀 협의 후 확정 — SPEC §6."""
        from openpyxl import Workbook

        out = Path(out_path)
        wb = Workbook()
        ws = wb.active
        ws.title = f"일상감사처리대장 {year}"
        cols = list(FIELD_LABELS)
        ws.append([FIELD_LABELS[c] for c in cols])
        for e in self.list_year(year):
            row = asdict(e)
            ws.append([row[c] for c in cols])
        wb.save(out)
        return out
