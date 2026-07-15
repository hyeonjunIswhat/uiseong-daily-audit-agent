"""정보화사업 관문 내비게이터 P1 (LLM 미관여, 결정론).

- gates.yaml의 관문 선언(근거·제출물·소요·사후의무)을 로딩
- backplan(): 공고 목표일에서 lead_weeks 역산 → 관문별 착수 마감일.
  착수마감이 휴일이면 앞당김(직전 근무일). 크리티컬 패스(최장 소요) 표시.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from audit_core.config import get_settings
from audit_core.rules.deadline import HolidayCalendar


@dataclass(frozen=True)
class Gate:
    key: str
    label: str
    basis: str
    channel: str
    lead_weeks: int
    requires: tuple[str, ...]
    output: str
    post_duty: str


@dataclass(frozen=True)
class GatePlan:
    gate: Gate
    start_by: date        # 착수 마감일(휴일 앞당김 반영)
    is_critical: bool     # 최장 소요 관문(크리티컬 패스)


class GateSet:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or get_settings().GATES_PATH)
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.provisional: bool = bool(raw.get("provisional", True))
        self.source: str = raw.get("source", "")
        self.gates: list[Gate] = [
            Gate(
                key=k,
                label=g["label"],
                basis=g.get("basis", ""),
                channel=g.get("channel", ""),
                lead_weeks=int(g["lead_weeks"]),
                requires=tuple(g.get("requires", [])),
                output=g.get("output", ""),
                post_duty=g.get("post_duty", ""),
            )
            for k, g in (raw.get("gates") or {}).items()
        ]
        if not self.gates:
            raise ValueError("gates.yaml에 관문 정의가 없음")

    def backplan(self, announce_date: date, cal: HolidayCalendar | None = None) -> list[GatePlan]:
        """공고 목표일 → 관문별 착수 마감일(오름차순). 휴일이면 직전 근무일로 앞당김."""
        cal = cal or HolidayCalendar()
        max_lead = max(g.lead_weeks for g in self.gates)
        plans = []
        for g in self.gates:
            d = announce_date - timedelta(weeks=g.lead_weeks)
            while cal.is_holiday(d):
                d -= timedelta(days=1)
            plans.append(GatePlan(gate=g, start_by=d, is_critical=g.lead_weeks == max_lead))
        plans.sort(key=lambda p: p.start_by)
        return plans


def format_overview(gs: GateSet) -> str:
    """`관문` — 관문 현황 카드(무엇을·어디에·뭘 내고·뭘 받는지 한 화면)."""
    lines = ["## 정보화사업 계약 전 관문 (5관문 한눈에)", ""]
    if gs.provisional:
        lines.append("> ⚠ 소요 기간은 2026 생성형AI플랫폼 실측 기반 **잠정값**입니다.\n")
    lines.append("| 관문 | 근거 | 제출처 | 소요(잠정) | 산출물 | 사후 의무 |")
    lines.append("|---|---|---|---|---|---|")
    for g in gs.gates:
        crit = " ★" if g.lead_weeks == max(x.lead_weeks for x in gs.gates) else ""
        lines.append(f"| **{g.label}**{crit} | {g.basis} | {g.channel} | 약 {g.lead_weeks}주 | {g.output} | {g.post_duty} |")
    lines.append("\n★ = 크리티컬 패스(가장 오래 걸림 — 최우선 착수)")
    lines.append("\n**제출물 체크리스트**")
    for g in gs.gates:
        lines.append(f"- {g.label}: " + ", ".join(g.requires))
    lines.append("\n`관문 2026-10-01` 처럼 공고 목표일을 주면 관문별 착수 마감일을 역산합니다.")
    return "\n".join(lines)


def format_backplan(gs: GateSet, announce_date: date, cal: HolidayCalendar | None = None) -> str:
    """`관문 <공고목표일>` — 착수 마감 역산표."""
    plans = gs.backplan(announce_date, cal)
    today = date.today()
    lines = [f"## 공고 목표 {announce_date.isoformat()} 기준 관문 착수 마감 (역산)", ""]
    if gs.provisional:
        lines.append("> ⚠ 소요 기간은 잠정값 — 건별 편차가 있으니 여유를 두세요.\n")
    lines.append("| 착수 마감 | 관문 | 소요 | 상태 |")
    lines.append("|---|---|---|---|")
    for p in plans:
        overdue = p.start_by < today
        status = "🔴 지금 즉시" if overdue else ("★ 최우선" if p.is_critical else "")
        lines.append(f"| **{p.start_by.isoformat()}** | {p.gate.label} | {p.gate.lead_weeks}주 | {status} |")
    crit = next(p for p in plans if p.is_critical)
    lines.append(f"\n크리티컬 패스: **{crit.gate.label}** — {crit.start_by.isoformat()}까지 착수하지 않으면 공고일이 밀립니다.")
    lines.append("사후 의무: " + " / ".join(f"{g.label}→{g.post_duty}" for g in gs.gates if g.post_duty))
    return "\n".join(lines)
