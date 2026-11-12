"""Contract renewals: status, recording renewal dates, and reminders.

A renewal date starts a new contract term. When the next renewal date is recorded, the employee and
HR are reminded as it approaches. When it is not recorded, no end date is assumed: HR is asked to
enter it, in one combined notification listing everyone without a next renewal date, repeated weekly
while the list is not empty and sent at once when someone new joins the list.
"""
from datetime import date, timedelta

from flask import url_for

from ..core import audit
from ..core.db import query, transaction
from ..core.utils import ValidationError, clean_text, fmt_date, get_setting, parse_date, today, utcnow_iso
from . import cycles
from .notifications import notify


def status(employee_id, on_date=None):
    """None for staff without a joining date. Otherwise the current term and either the next recorded
    renewal ('scheduled') or 'missing' when HR has not recorded one."""
    t = on_date or today()
    marks = cycles.milestones(employee_id)
    if not marks:
        return None
    start = max((m for m in marks if m <= t), default=marks[0])
    upcoming = [m for m in marks if m > t]
    if upcoming:
        return {"term_start": start, "renewal_due": upcoming[0], "days_left": (upcoming[0] - t).days,
                "state": "scheduled"}
    return {"term_start": start, "renewal_due": None, "days_left": None, "state": "missing"}


def overview(on_date=None):
    """Every active employee with a joining date and their contract status: missing dates first."""
    rows = []
    for e in query("""SELECT e.id, e.emp_code, e.full_name, e.joining_date, e.contract_end_date, g.name AS designation, d.name AS department
                        FROM employees e LEFT JOIN designations g ON g.id = e.designation_id
                        LEFT JOIN departments d ON d.id = e.department_id
                       WHERE e.status = 'ACTIVE' AND e.joining_date IS NOT NULL ORDER BY e.full_name"""):
        rows.append({"emp": e, "s": status(e["id"], on_date)})
    return sorted(rows, key=lambda r: (r["s"]["state"] != "missing", r["s"]["renewal_due"] or r["s"]["term_start"]))


def hr_user_ids():
    return [r["id"] for r in query("""SELECT u.id FROM users u JOIN roles r ON r.id = u.role_id
                                      JOIN employees e ON e.id = u.employee_id
                                     WHERE r.code = 'HR' AND u.is_active = 1 AND e.status = 'ACTIVE'""")]


# ---------------------------------------------------------------- contract end date
def contract_end(employee_id):
    """Last day of the current contract, or None when HR has not recorded one (nothing is blocked)."""
    row = query("SELECT contract_end_date FROM employees WHERE id = ?", (employee_id,), one=True)
    return date.fromisoformat(row["contract_end_date"]) if row and row["contract_end_date"] else None


def after_contract(employee_id, d):
    end = contract_end(employee_id)
    return end is not None and d > end


def set_contract_end(employee_id, value):
    """Record or clear (blank value) the last day of the employee's current contract."""
    d = parse_date(value, "Contract end date") if (value or "").strip() else None
    with transaction() as conn:
        emp = conn.execute("SELECT joining_date, contract_end_date FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if emp is None:
            raise ValidationError("Employee not found.")
        if d and emp["joining_date"] and d.isoformat() < emp["joining_date"]:
            raise ValidationError("The contract end date cannot be before the date of joining.")
        new = d.isoformat() if d else None
        if new == emp["contract_end_date"]:
            return
        conn.execute("UPDATE employees SET contract_end_date = ?, updated_at = ? WHERE id = ?",
                     (new, utcnow_iso(), employee_id))
        audit.log("CONTRACT_END_SET", "employee", employee_id, prev={"contract_end_date": emp["contract_end_date"]},
                  new={"contract_end_date": new})


def missing_end_count():
    return query("SELECT COUNT(*) AS n FROM employees WHERE status = 'ACTIVE' AND contract_end_date IS NULL",
                 one=True)["n"]


# ---------------------------------------------------------------- recording renewals
def add_renewal(employee_id, renewal_date, note):
    d = parse_date(renewal_date, "Renewal date")
    note = clean_text(note, "Note", max_len=200) or None
    from .leave import rebuild_balances
    with transaction() as conn:
        emp = conn.execute("SELECT emp_code, joining_date FROM employees WHERE id = ?", (employee_id,)).fetchone()
        if emp is None:
            raise ValidationError("Employee not found.")
        if not emp["joining_date"]:
            raise ValidationError("Record the date of joining first; renewals follow from it.")
        if d.isoformat() <= emp["joining_date"]:
            raise ValidationError("The renewal date must be after the date of joining.")
        if conn.execute("SELECT 1 FROM contract_renewals WHERE employee_id = ? AND renewal_date = ?",
                        (employee_id, d.isoformat())).fetchone():
            raise ValidationError("That renewal date is already recorded.")
        rid = conn.execute("INSERT INTO contract_renewals (employee_id, renewal_date, note) VALUES (?, ?, ?)",
                           (employee_id, d.isoformat(), note)).lastrowid
        audit.log("CONTRACT_RENEWAL_ADDED", "employee", employee_id, new={"renewal_date": d.isoformat(), "note": note},
                  remarks=f"Renewal #{rid}")
    rebuild_balances([employee_id], remarks=f"Contract renewal added for {emp['emp_code']}")


def remove_renewal(employee_id, renewal_id):
    from .leave import rebuild_balances
    with transaction() as conn:
        row = conn.execute("SELECT * FROM contract_renewals WHERE id = ? AND employee_id = ?",
                           (renewal_id, employee_id)).fetchone()
        if row is None:
            raise ValidationError("Renewal not found.")
        conn.execute("DELETE FROM contract_renewals WHERE id = ?", (renewal_id,))
        audit.log("CONTRACT_RENEWAL_REMOVED", "employee", employee_id, prev=dict(row))
    rebuild_balances([employee_id], remarks="Contract renewal removed")


# ---------------------------------------------------------------- reminders
def reminder_days():
    days = []
    for p in (get_setting("renewal_reminder_days") or "").split(","):
        if p.strip().isdigit():
            days.append(int(p))
    return sorted(set(days) or {1}, reverse=True)


def _sent(conn, key):
    return conn.execute("SELECT 1 FROM reminder_log WHERE key = ?", (key,)).fetchone() is not None


def _mark(conn, key):
    conn.execute("INSERT OR IGNORE INTO reminder_log (key, sent_at) VALUES (?, ?)", (key, utcnow_iso()))


def _when(days):
    return "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")


def _describe(p):
    extra = ", ".join(x for x in (p["designation"], p["department"]) if x)
    return p["full_name"] + (f" ({extra})" if extra else "")


def send_reminders(on_date=None):
    """Send any reminders that are due. Safe to run many times a day. Returns the number of notifications."""
    t = on_date or today()
    thresholds = reminder_days()
    hr = hr_user_ids()
    people = query("""SELECT e.id, e.full_name, g.name AS designation, d.name AS department, u.id AS user_id
                        FROM employees e JOIN users u ON u.employee_id = e.id
                        LEFT JOIN designations g ON g.id = e.designation_id LEFT JOIN departments d ON d.id = e.department_id
                       WHERE e.status = 'ACTIVE' AND u.is_active = 1 AND e.joining_date IS NOT NULL
                       ORDER BY e.full_name""")
    sent, missing, newly_missing = 0, [], False
    with transaction() as conn:
        for p in people:
            s = status(p["id"], t)
            if s["state"] == "missing":
                missing.append(p)
                seen = f"renewal-missing:{p['id']}:{s['term_start'].isoformat()}"
                if not _sent(conn, seen):
                    _mark(conn, seen)
                    newly_missing = True
                continue
            base = f"renewal:{p['id']}:{s['renewal_due'].isoformat()}"
            due = [d for d in thresholds if s["days_left"] <= d]
            if not due:
                continue
            current = min(due)
            if _sent(conn, f"{base}:{current}"):
                continue
            for d in due:  # reminders missed while the check was not running are not sent late
                _mark(conn, f"{base}:{d}")
            when = _when(s["days_left"])
            title = f"Contract renewal due {when}"
            notify([p["user_id"]], title,
                   f"Your contract renewal is due on {fmt_date(s['renewal_due'])} ({when}). "
                   "Please contact HR or your reporting officer about the renewal.", url_for("dashboards.profile"))
            notify(hr, title, f"Contract renewal for {_describe(p)} is due on {fmt_date(s['renewal_due'])} ({when}).",
                   url_for("dashboards.employee_history", employee_id=p["id"]) + "#contract")
            sent += 1

        # one combined request to HR for every employee without a next renewal date
        year, week, _ = t.isocalendar()
        weekly = f"renewal-missing-digest:{year}-W{week:02d}"
        if missing and (newly_missing or not _sent(conn, weekly)):
            _mark(conn, weekly)
            names = ", ".join(p["full_name"] for p in missing[:12]) + (f" and {len(missing) - 12} more" if len(missing) > 12 else "")
            notify(hr, "Contract renewal dates needed",
                   f"No next renewal date is recorded for {len(missing)} employee{'s' if len(missing) != 1 else ''}: "
                   f"{names}. Please record each person's renewal date so that renewal reminders can be sent.",
                   url_for("dashboards.renewals"))
            sent += 1
        if sent:
            audit.log("RENEWAL_REMINDERS_SENT", "system", None,
                      new={"notifications": sent, "missing_dates": len(missing), "date": t.isoformat()})
    return sent


