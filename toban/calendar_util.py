"""年度・当番日の計算。"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger("toban")

try:  # 祝日判定（無くても動くが祝日が当番日になる）
    import jpholiday  # type: ignore
except ImportError:  # pragma: no cover
    jpholiday = None
    log.warning("jpholiday が未インストールのため祝日を除外できません")

DOW = "月火水木金土日"


def dow_label(d: date) -> str:
    return DOW[d.weekday()]


def fiscal_year_of(d: date, start_month: int = 4) -> int:
    return d.year if d.month >= start_month else d.year - 1


def fiscal_range(fy: int, start_month: int = 4) -> tuple[date, date]:
    start = date(fy, start_month, 1)
    end = date(fy + 1, start_month, 1) - timedelta(days=1)
    return start, end


def parse_date(text: str) -> date | None:
    """'2026-04-01' / '2026/4/1' / '2026年4月1日' を許容。"""
    t = str(text).strip()
    if not t:
        return None
    for ch in ("年", "月", "/", "."):
        t = t.replace(ch, "-")
    t = t.replace("日", "").strip("- ")
    parts = t.split("-")
    try:
        y, m, dd = (int(p) for p in parts[:3])
        return date(y, m, dd)
    except (ValueError, TypeError):
        return None


def is_year_end_break(d: date) -> bool:
    return (d.month == 12 and d.day >= 29) or (d.month == 1 and d.day <= 3)


def duty_days(
    start: date,
    end: date,
    closed: set[date] | None = None,
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4),
) -> list[date]:
    closed = closed or set()
    out: list[date] = []
    d = start
    while d <= end:
        if (
            d.weekday() in weekdays
            and d not in closed
            and not is_year_end_break(d)
            and not (jpholiday and jpholiday.is_holiday(d))
        ):
            out.append(d)
        d += timedelta(days=1)
    return out
