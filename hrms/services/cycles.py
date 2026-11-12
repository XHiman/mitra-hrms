"""Leave cycles.

Staff with a joining date follow their contract: a cycle starts on the joining date and again on
every recorded renewal date. A term longer than a year is split at each yearly anniversary, and
after the last recorded date the cycle repeats yearly. Staff without a joining date (for example
officers on deputation) follow the organisation's calendar leave year.

A cycle is identified by its start date as an integer (YYYYMMDD), stored in the `leave_year`
columns of balances, requests and comp-off claims.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from ..core.db import query
from ..core.utils import fmt_date, leave_year_bounds, leave_year_of


@dataclass(frozen=True)
class Cycle:
    start: date
    end: date
    kind: str  # 'CONTRACT' or 'CALENDAR'

    @property
    def key(self):
        return int(self.start.strftime("%Y%m%d"))

    @property
    def label(self):
        return f"{fmt_date(self.start)} to {fmt_date(self.end)}"

    def contains(self, d):
        return self.start <= d <= self.end


def add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def key_to_date(key):
    k = int(key)
    if k < 10000:  # legacy calendar-year key (e.g. 2026)
        return leave_year_bounds(k)[0]
    return date(k // 10000, k // 100 % 100, k % 100)


def milestones(employee_id):
    """Joining date followed by renewal dates, sorted and de-duplicated."""
    emp = query("SELECT joining_date FROM employees WHERE id = ?", (employee_id,), one=True)
    if not emp or not emp["joining_date"]:
        return []
    dates = {date.fromisoformat(emp["joining_date"])}
    dates |= {date.fromisoformat(r["renewal_date"]) for r in
              query("SELECT renewal_date FROM contract_renewals WHERE employee_id = ?", (employee_id,))}
    joined = date.fromisoformat(emp["joining_date"])
    return sorted(d for d in dates if d >= joined)


def cycle_for(employee_id, on_date, marks=None):
    marks = milestones(employee_id) if marks is None else marks
    if not marks:
        first, last = leave_year_bounds(leave_year_of(on_date))
        return Cycle(first, last, "CALENDAR")
    if on_date < marks[0]:
        on_date = marks[0]  # before joining: the first cycle
    term_start = max(m for m in marks if m <= on_date)
    later = [m for m in marks if m > term_start]
    term_end = later[0] - timedelta(days=1) if later else None
    start = term_start
    while add_months(start, 12) <= on_date and (term_end is None or add_months(start, 12) <= term_end):
        start = add_months(start, 12)
    end = add_months(start, 12) - timedelta(days=1)
    if term_end is not None:
        end = min(end, term_end)
    return Cycle(start, end, "CONTRACT")


def cycle_by_key(employee_id, key):
    return cycle_for(employee_id, key_to_date(key))


def contract_summary(employee_id, today):
    """What the dashboard shows: joining date, current term and the next renewal date, if recorded."""
    marks = milestones(employee_id)
    if not marks:
        return None
    current = max((m for m in marks if m <= today), default=marks[0])
    upcoming = [m for m in marks if m > today]
    renewals = query("SELECT renewal_date, note FROM contract_renewals WHERE employee_id = ? ORDER BY renewal_date",
                     (employee_id,))
    return {"joining": marks[0], "term_start": current, "renewal_due": upcoming[0] if upcoming else None,
            "renewals": renewals, "days_to_renewal": (upcoming[0] - today).days if upcoming else None}
