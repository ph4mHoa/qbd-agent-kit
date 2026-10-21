"""Calendar helpers on plain YYYY-MM-DD strings, so time zones and daylight saving never shift a date."""
from __future__ import annotations

import datetime as dt
import re

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def local_today() -> str:
    """Today on this PC's clock, which is the clock QuickBooks uses."""
    return dt.date.today().isoformat()


def _parse(date: str) -> dt.date:
    """A day or month past the end runs over, as JavaScript dates do: 2026-02-30 is 2026-03-02."""
    y, m, d = (int(x) for x in date.split("-"))
    return dt.date(y + (m - 1) // 12, (m - 1) % 12 + 1, 1) + dt.timedelta(days=d - 1)


def _ymd(y: int, m: int, d: int) -> str:
    """A date from a year, a 0-based month and a day that may run over: month 12 is January next year,
    day 0 is the last day of the month before."""
    y += m // 12
    m %= 12
    return (dt.date(y, m + 1, 1) + dt.timedelta(days=d - 1)).isoformat()


def add_days(date: str, days: int) -> str:
    return (_parse(date) + dt.timedelta(days=days)).isoformat()


def days_between(a: str, b: str) -> int:
    """Whole days from a to b (positive when b is later)."""
    return (_parse(b) - _parse(a)).days


def month_start(date: str, offset_months: int = 0) -> str:
    d = _parse(date)
    return _ymd(d.year, d.month - 1 + offset_months, 1)


def month_end(date: str, offset_months: int = 0) -> str:
    d = _parse(date)
    return _ymd(d.year, d.month - 1 + offset_months + 1, 0)


def is_date(s: object) -> bool:
    if not isinstance(s, str) or not _DATE.match(s):
        return False
    try:
        return _parse(s).isoformat() == s
    except (ValueError, OverflowError):
        return False


def add_months_clamped(date: str, months: int) -> str:
    """Same day n months away, clamped to the month's last day (Mar 31 - 1 month = Feb 28/29)."""
    d = _parse(date)
    last = _parse(_ymd(d.year, d.month - 1 + months + 1, 0)).day
    return _ymd(d.year, d.month - 1 + months, min(d.day, last))


def macro_range(macro: str, today: str, first_fiscal_month: int = 1) -> dict[str, str] | None:
    """The dates a QuickBooks date macro covers. Fiscal macros use first_fiscal_month (1 = January)."""
    t = _parse(today)
    y, m = t.year, t.month - 1  # m is 0-based
    dow = (t.weekday() + 1) % 7  # 0 = Sunday; QuickBooks weeks start on Sunday
    week_start = add_days(today, -dow)

    def rng(a: str, b: str) -> dict[str, str]:
        return {"from": a, "to": b}

    def quarter(offset: int) -> dict[str, str]:
        q0 = (m // 3) * 3 + offset * 3
        return rng(_ymd(y, q0, 1), _ymd(y, q0 + 3, 0))

    def fiscal(offset: int, months: int) -> dict[str, str]:
        fm = first_fiscal_month - 1
        fy_start_year = y if m >= fm else y - 1
        if months == 12:
            return rng(_ymd(fy_start_year + offset, fm, 1), _ymd(fy_start_year + offset + 1, fm, 0))
        months_in = (y - fy_start_year) * 12 + (m - fm)
        q_start = fm + (months_in // 3) * 3 + offset * 3
        return rng(_ymd(fy_start_year, q_start, 1), _ymd(fy_start_year, q_start + 3, 0))

    def to_date(r: dict[str, str], end: str = today) -> dict[str, str]:
        return rng(r["from"], end if end < r["to"] else r["to"])

    def last_year_to_date(r: dict[str, str]) -> dict[str, str]:
        return rng(r["from"], add_months_clamped(today, -12))

    if macro == "All":
        return rng("1900-01-01", "2999-12-31")
    if macro == "Today":
        return rng(today, today)
    if macro == "Yesterday":
        return rng(add_days(today, -1), add_days(today, -1))
    if macro == "ThisWeek":
        return rng(week_start, add_days(week_start, 6))
    if macro == "ThisWeekToDate":
        return rng(week_start, today)
    if macro == "LastWeek":
        return rng(add_days(week_start, -7), add_days(week_start, -1))
    if macro == "LastWeekToDate":
        return rng(add_days(week_start, -7), add_days(today, -7))
    if macro == "NextWeek":
        return rng(add_days(week_start, 7), add_days(week_start, 13))
    if macro == "NextFourWeeks":
        return rng(add_days(week_start, 7), add_days(week_start, 34))
    if macro == "ThisMonth":
        return rng(_ymd(y, m, 1), _ymd(y, m + 1, 0))
    if macro == "ThisMonthToDate":
        return rng(_ymd(y, m, 1), today)
    if macro == "LastMonth":
        return rng(_ymd(y, m - 1, 1), _ymd(y, m, 0))
    if macro == "LastMonthToDate":
        return to_date(rng(_ymd(y, m - 1, 1), _ymd(y, m, 0)), add_months_clamped(today, -1))
    if macro == "NextMonth":
        return rng(_ymd(y, m + 1, 1), _ymd(y, m + 2, 0))
    if macro == "ThisCalendarQuarter":
        return quarter(0)
    if macro == "ThisCalendarQuarterToDate":
        return to_date(quarter(0))
    if macro == "LastCalendarQuarter":
        return quarter(-1)
    if macro == "LastCalendarQuarterToDate":
        return to_date(quarter(-1), add_months_clamped(today, -3))
    if macro == "NextCalendarQuarter":
        return quarter(1)
    if macro == "ThisCalendarYear":
        return rng(_ymd(y, 0, 1), _ymd(y, 12, 0))
    if macro == "ThisCalendarYearToDate":
        return rng(_ymd(y, 0, 1), today)
    if macro == "LastCalendarYear":
        return rng(_ymd(y - 1, 0, 1), _ymd(y, 0, 0))
    if macro == "LastCalendarYearToDate":
        return last_year_to_date(rng(_ymd(y - 1, 0, 1), _ymd(y, 0, 0)))
    if macro == "NextCalendarYear":
        return rng(_ymd(y + 1, 0, 1), _ymd(y + 2, 0, 0))
    if macro == "ThisFiscalQuarter":
        return fiscal(0, 3)
    if macro == "ThisFiscalQuarterToDate":
        return to_date(fiscal(0, 3))
    if macro == "LastFiscalQuarter":
        return fiscal(-1, 3)
    if macro == "LastFiscalQuarterToDate":
        return to_date(fiscal(-1, 3), add_months_clamped(today, -3))
    if macro == "NextFiscalQuarter":
        return fiscal(1, 3)
    if macro == "ThisFiscalYear":
        return fiscal(0, 12)
    if macro == "ThisFiscalYearToDate":
        return to_date(fiscal(0, 12))
    if macro == "LastFiscalYear":
        return fiscal(-1, 12)
    if macro == "LastFiscalYearToDate":
        return last_year_to_date(fiscal(-1, 12))
    if macro == "NextFiscalYear":
        return fiscal(1, 12)
    return None
