"""Holiday calendar and staff birthdays shown on everyone's calendar."""
import calendar
import csv
from datetime import date, timedelta
from pathlib import Path

from ..core import audit
from ..core.db import query, transaction
from ..core.utils import ValidationError

HOLIDAY_DIR = Path(__file__).resolve().parent.parent / "data" / "holidays"


# ---------------------------------------------------------------- holidays
def bundled_holiday_files():
    """{year: path} for the official Government of Maharashtra lists shipped with the system."""
    out = {}
    for p in sorted(HOLIDAY_DIR.glob("maharashtra-*.csv")):
        year = p.stem.rsplit("-", 1)[-1]
        if year.isdigit():
            out[int(year)] = p
    return out


def load_holidays(path, remarks):
    """Add or update holidays from a CSV (date,name,description). Returns (added, updated)."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        try:
            date.fromisoformat(r["date"].strip())
        except (KeyError, ValueError):
            raise ValidationError(f"Holiday file has an invalid date: {r.get('date')!r}")
        if not (r.get("name") or "").strip():
            raise ValidationError(f"Holiday on {r['date']} has no name.")
    added = updated = 0
    with transaction() as conn:
        for r in rows:
            d, name, desc = r["date"].strip(), r["name"].strip(), (r.get("description") or "").strip() or None
            old = conn.execute("SELECT * FROM holidays WHERE date = ?", (d,)).fetchone()
            if old is None:
                hid = conn.execute("INSERT INTO holidays (date, name, description) VALUES (?, ?, ?)",
                                   (d, name, desc)).lastrowid
                audit.log("HOLIDAY_ADDED", "holiday", hid, new={"date": d, "name": name}, remarks=remarks)
                added += 1
            elif (old["name"], old["description"]) != (name, desc):
                conn.execute("UPDATE holidays SET name = ?, description = ? WHERE id = ?", (name, desc, old["id"]))
                audit.log("HOLIDAY_UPDATED", "holiday", old["id"], prev={"name": old["name"]}, new={"name": name},
                          remarks=remarks)
                updated += 1
    return added, updated


def holidays_between(first, last):
    return query("SELECT * FROM holidays WHERE date BETWEEN ? AND ? ORDER BY date", (first.isoformat(), last.isoformat()))


# ---------------------------------------------------------------- birthdays
def _occurrence(dob, year):
    """Birthday in a given year; 29 Feb falls on 28 Feb in other years."""
    if dob.month == 2 and dob.day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, dob.month, dob.day)


def birthdays_between(first, last):
    """[{date, employee_id, full_name, designation, department}] for active staff who share their birthday.
    Only day and month are exposed, never the year of birth."""
    people = query("""SELECT e.id, e.full_name, e.date_of_birth, g.name AS designation, d.name AS department
                        FROM employees e LEFT JOIN designations g ON g.id = e.designation_id
                        LEFT JOIN departments d ON d.id = e.department_id
                       WHERE e.status = 'ACTIVE' AND e.show_birthday = 1 AND e.date_of_birth IS NOT NULL""")
    out = []
    for p in people:
        try:
            dob = date.fromisoformat(p["date_of_birth"])
        except ValueError:
            continue
        for year in range(first.year, last.year + 1):
            day = _occurrence(dob, year)
            if first <= day <= last:
                out.append({"date": day, "employee_id": p["id"], "full_name": p["full_name"],
                            "designation": p["designation"], "department": p["department"]})
    return sorted(out, key=lambda b: (b["date"], b["full_name"]))


def upcoming_birthdays(today, days=30):
    return birthdays_between(today, today + timedelta(days=days))
