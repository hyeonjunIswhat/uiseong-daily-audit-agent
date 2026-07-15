"""일상감사 기한 계산 (기능 #8). LLM 미관여.

잠정 기한: 결과통보 7일 / 재검토 7일 / 조치결과 통보 14일 (행안부 매뉴얼 기준,
세부규정 확정 시 DEADLINE_NOTIFY 값만 교체 — DESIGN.md 9장 미결 1).

계산 방식 (DEADLINE_MODE):
- calendar_roll (기본): 역일로 N일 가산, 기한 말일이 토·일·공휴일이면 다음
  근무일로 이월 (민법 제161조 방식)
- business: 토·일·공휴일을 제외한 근무일 N일 가산
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from audit_core.config import get_settings


class HolidayCalendar:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().HOLIDAYS_PATH)
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.provisional: bool = bool(raw.get("provisional", True))
        self._holidays: set[date] = set()
        self._years: set[int] = set()
        for year, days in (raw.get("holidays") or {}).items():
            self._years.add(int(year))
            for d in days or []:
                self._holidays.add(d if isinstance(d, date) else date.fromisoformat(str(d)))

    def is_holiday(self, d: date) -> bool:
        return d.weekday() >= 5 or d in self._holidays

    def covers(self, d: date) -> bool:
        """해당 일자의 연도 공휴일 데이터가 존재하는지 (미존재 시 주말만 반영됨)."""
        return d.year in self._years

    def next_business_day(self, d: date) -> date:
        while self.is_holiday(d):
            d += timedelta(days=1)
        return d

    def add_business_days(self, start: date, n: int) -> date:
        d = start
        remaining = n
        while remaining > 0:
            d += timedelta(days=1)
            if not self.is_holiday(d):
                remaining -= 1
        return d


@dataclass(frozen=True)
class DeadlineResult:
    anchor: date          # 기산일 (접수일 등)
    days: int             # 가산 일수
    mode: str             # calendar_roll | business
    due: date             # 기한 말일
    rolled: bool          # 휴일 이월 발생 여부 (calendar_roll)
    calendar_covered: bool  # 공휴일 데이터가 해당 연도를 커버하는지
    provisional: bool     # 기한 일수·공휴일 데이터가 잠정인지


def compute_deadline(
    anchor: date,
    days: int,
    mode: str | None = None,
    calendar: HolidayCalendar | None = None,
) -> DeadlineResult:
    if days <= 0:
        raise ValueError(f"가산 일수는 1 이상이어야 함: {days}")
    settings = get_settings()
    mode = mode or settings.DEADLINE_MODE
    calendar = calendar or HolidayCalendar()

    if mode == "business":
        due = calendar.add_business_days(anchor, days)
        rolled = False
    elif mode == "calendar_roll":
        raw_due = anchor + timedelta(days=days)
        due = calendar.next_business_day(raw_due)
        rolled = due != raw_due
    else:
        raise ValueError(f"알 수 없는 DEADLINE_MODE: {mode!r} (calendar_roll | business)")

    return DeadlineResult(
        anchor=anchor,
        days=days,
        mode=mode,
        due=due,
        rolled=rolled,
        calendar_covered=calendar.covers(due),
        provisional=calendar.provisional,
    )


def audit_deadlines(receipt_date: date, calendar: HolidayCalendar | None = None) -> dict[str, DeadlineResult]:
    """접수일 기준 일상감사 3종 기한.

    - notify: 접수일 → 감사의견 결과통보 기한
    - recheck: (재검토 요청 시) 요청일 = 통보기한으로 가정한 재검토 기한 — 실제
      요청일 확정 시 compute_deadline으로 재산출
    - action_report: 통보기한 기준 조치결과 통보 기한
    """
    calendar = calendar or HolidayCalendar()
    d_notify, d_recheck, d_action = get_settings().deadline_days()
    notify = compute_deadline(receipt_date, d_notify, calendar=calendar)
    recheck = compute_deadline(notify.due, d_recheck, calendar=calendar)
    action = compute_deadline(notify.due, d_action, calendar=calendar)
    return {"notify": notify, "recheck": recheck, "action_report": action}
