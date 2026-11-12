"""Timesheets, booked day by day on a month calendar and submitted week by week.

Every weekday has an ideal of the configured daily hours (8 by default); public holidays count as
weekdays unless the setting 'timesheet_holidays_expected' is 0. The ideal is met by hours booked on
projects (8 on one project, or 4 + 4 on two) and by approved paid leave, which is linked to its day
automatically (a half day gives half the hours). Unpaid leave books nothing, so it shows as a
shortfall. Weekly offs, days before joining and days after the contract end date have no ideal and
cannot be booked. Days below the ideal are flagged, never blocked. A submitted week is locked; the
employee may reopen it (audited). There is no approval step.
"""
import csv
import io
from datetime import date, timedelta

from ..core import audit
from ..core.db import query, transaction
from ..core.utils import (ValidationError, clean_text, fmt_date, get_int_setting, get_setting, today, utcnow_iso,
                          weekend_days)

MAX_ROWS_PER_DAY = 6
MAX_HOURS_PER_DAY = 16
MAX_HOURS_PER_ROW = 8
HOUR_CHOICES = [str(h) for h in range(1, MAX_HOURS_PER_ROW + 1)]  # whole hours only


def week_start(d):
    return d - timedelta(days=d.weekday())


def parse_week(value):
    try:
        d = date.fromisoformat(value) if value else today()
    except ValueError:
        d = today()
    return week_start(d)


def daily_hours():
    return get_int_setting("timesheet_daily_hours")


def projects(active_only=True):
    return query("SELECT * FROM timesheet_projects" + (" WHERE is_active = 1" if active_only else "") + " ORDER BY name")


def holidays_expected():
    return get_int_setting("timesheet_holidays_expected") == 1


LEAVE_SQL = """SELECT r.employee_id, r.from_date, r.to_date, r.half_day, lt.name, lt.is_paid
                 FROM leave_requests r JOIN leave_types lt ON lt.id = r.leave_type_id
                WHERE r.status = 'APPROVED' AND r.from_date <= ? AND r.to_date >= ?"""


def day_rules():
    """Settings that decide each day's ideal hours, read once."""
    return {"offs": weekend_days(), "today": today(), "hours": daily_hours(), "hol_ideal": holidays_expected(),
            "began": get_setting("timesheet_start_date") or None}


def day_info(employee_id, start):
    """The 7 days of the week with the ideal hours of each and any approved leave linked to it.

    ideal    = hours the day should account for (0 on weekly offs and closed days)
    leave    = the approved leave on that day: name, paid, half ('FIRST'/'SECOND'/None), hours
    expected = hours still to book on projects = ideal minus paid leave hours"""
    emp = query("SELECT joining_date, contract_end_date FROM employees WHERE id = ?", (employee_id,), one=True)
    end = start + timedelta(days=6)
    hols = {r["date"]: r["name"] for r in query("SELECT date, name FROM holidays WHERE date BETWEEN ? AND ?",
                                                 (start.isoformat(), end.isoformat()))}
    leave = query(LEAVE_SQL + " AND r.employee_id = ? ORDER BY r.from_date",
                  (end.isoformat(), start.isoformat(), employee_id))
    return build_days(emp, start, 7, hols, leave, day_rules())


def build_days(emp, start, count, hols, leave, rules):
    """The rules for `count` days from `start` for one employee (emp: joining_date, contract_end_date;
    leave: that employee's approved leave rows). Shared by the timesheet and the analytics."""
    joined = emp["joining_date"] if emp else None
    ends = emp["contract_end_date"] if emp else None
    offs, t, hours, hol_ideal, began = (rules["offs"], rules["today"], rules["hours"], rules["hol_ideal"],
                                        rules["began"])
    days = []
    for i in range(count):
        d = start + timedelta(days=i)
        iso = d.isoformat()
        ideal, kind, note = hours, "working", None
        if joined and iso < joined:
            ideal, kind, note = 0, "not_eligible", "Before date of joining"
        elif began and iso < began:
            ideal, kind, note = 0, "not_eligible", "Before timesheets started"
        elif ends and iso > ends:
            ideal, kind, note = 0, "after_contract", "After contract end date"
        elif d.weekday() in offs:
            ideal, kind, note = 0, "off", "Weekly off"
        elif iso in hols:
            kind, note = "holiday", f"Holiday: {hols[iso]}"
            if not hol_ideal:
                ideal = 0
        lv = None
        if ideal:
            # a holiday inside an approved leave period is covered by that leave
            for r in leave:
                if r["from_date"] <= iso <= r["to_date"]:
                    lv = {"name": r["name"], "paid": bool(r["is_paid"]), "half": r["half_day"],
                          "hours": hours / 2 if r["half_day"] else hours}
            if lv and not lv["half"]:
                kind = "leave"
                note = f"On {'paid' if lv['paid'] else 'unpaid'} leave ({lv['name']})"
        leave_hours = lv["hours"] if lv and lv["paid"] else 0
        days.append({"date": d, "iso": iso, "note": note, "ideal": ideal, "expected": max(0, ideal - leave_hours),
                     "leave": lv, "leave_hours": leave_hours, "leave_type": lv["name"] if lv else None,
                     "future": d > t, "kind": kind, "holiday": hols.get(iso),
                     "locked": d > t or kind in ("not_eligible", "after_contract")})
    return days


def sheet(employee_id, start):
    s = query("SELECT * FROM timesheets WHERE employee_id = ? AND week_start = ?", (employee_id, start.isoformat()), one=True)
    entries = query("""SELECT e.*, p.name AS project FROM timesheet_entries e JOIN timesheet_projects p ON p.id = e.project_id
                       WHERE e.timesheet_id = ? ORDER BY e.work_date, e.seq""", (s["id"],)) if s else []
    return s, entries


def week_view(employee_id, start):
    """Everything the week page needs: days with their entries, totals and shortfalls."""
    s, entries = sheet(employee_id, start)
    days = day_info(employee_id, start)
    for d in days:
        d["entries"] = [e for e in entries if e["work_date"] == d["iso"]]
        d["total"] = sum(e["hours"] for e in d["entries"])
        d["booked"] = d["total"] + d["leave_hours"]
        d["short"] = (not d["future"]) and d["ideal"] > 0 and d["booked"] < d["ideal"]
    expected = sum(d["expected"] for d in days if not d["future"])
    return {"sheet": s, "days": days, "total": sum(d["total"] for d in days), "expected": expected,
            "expected_week": sum(d["expected"] for d in days), "short_days": [d for d in days if d["short"]],
            "leave_hours": sum(d["leave_hours"] for d in days)}


def _status(d):
    if d["kind"] == "not_eligible":
        return "not-eligible"
    if d["kind"] == "after_contract":
        return "after-contract"
    if d["kind"] == "off":
        return "off"
    if d["ideal"] == 0:
        return "holiday"
    if d["leave"] and not d["leave"]["half"] and d["total"] == 0:
        return "leave" if d["leave"]["paid"] else "unpaid"
    if d["future"]:
        return "upcoming"
    if d["booked"] >= d["ideal"]:
        return "complete"
    return "pending" if d["booked"] == 0 else "short"


def month_view(employee_id, year, month):
    """Calendar for one month: weeks (Monday to Sunday) of days with status, plus the monthly summary.

    Ideal time = days with an ideal in the month x daily hours. Total time booked = project hours + paid
    leave hours. Shortfall = ideal - booked for the whole month (the 'so far' figures stop at today)."""
    import calendar as _cal
    first = date(year, month, 1)
    last = date(year, month, _cal.monthrange(year, month)[1])
    t = today()
    weeks, start = [], week_start(first)
    s = {"ideal": 0, "ideal_so_far": 0, "project": 0, "project_so_far": 0, "leave_hours": 0, "leave_so_far": 0,
         "working_days": 0, "leave_days": 0, "unpaid_days": 0, "complete": 0, "short": 0, "pending": 0,
         "holidays": 0, "weekend_hours": 0, "projects": {}}
    while start <= last:
        wv = week_view(employee_id, start)
        for d in wv["days"]:
            d["in_month"] = first <= d["date"] <= last
            d["status"] = _status(d)
            d["shortfall"] = max(0, d["ideal"] - d["booked"]) if d["status"] in ("short", "pending", "unpaid") else 0
            if not d["in_month"]:
                continue
            s["ideal"] += d["ideal"]
            s["project"] += d["total"]
            s["leave_hours"] += d["leave_hours"]
            if d["ideal"]:
                s["working_days"] += 1
            if d["holiday"]:
                s["holidays"] += 1
            if d["leave"]:
                share = 0.5 if d["leave"]["half"] else 1
                s["leave_days" if d["leave"]["paid"] else "unpaid_days"] += share
            if d["kind"] == "off":
                s["weekend_hours"] += d["total"]
            if d["status"] in ("complete", "short", "pending"):
                s[d["status"]] += 1
            if not d["future"]:
                s["ideal_so_far"] += d["ideal"]
                s["project_so_far"] += d["total"]
                s["leave_so_far"] += d["leave_hours"]
            for e in d["entries"]:
                s["projects"][e["project"]] = s["projects"].get(e["project"], 0) + e["hours"]
        weeks.append(wv)
        start += timedelta(days=7)
    s["booked"] = s["project"] + s["leave_hours"]
    s["shortfall"] = max(0, s["ideal"] - s["booked"])
    s["surplus"] = max(0, s["booked"] - s["ideal"])
    s["booked_so_far"] = s["project_so_far"] + s["leave_so_far"]
    s["behind"] = max(0, s["ideal_so_far"] - s["booked_so_far"])
    s["project_rows"] = sorted(s["projects"].items(), key=lambda kv: -kv[1])
    return {"first": first, "last": last, "weeks": weeks, "summary": s, "this_week": week_start(t)}


def year_summary(employee_id, year):
    """Month-wise snapshot: one row per month of the year up to the current month."""
    t = today()
    rows = []
    for m in range(1, 13):
        if date(year, m, 1) > t:
            break
        s = month_view(employee_id, year, m)["summary"]
        if s["ideal"] == 0 and s["booked"] == 0:  # before joining or before timesheets started
            continue
        rows.append({"month": date(year, m, 1), **{k: s[k] for k in (
            "ideal", "project", "leave_hours", "booked", "shortfall", "surplus", "leave_days", "unpaid_days",
            "working_days")}})
    total = {k: sum(r[k] for r in rows) for k in ("ideal", "project", "leave_hours", "booked", "leave_days",
                                                   "unpaid_days")}
    total["shortfall"] = max(0, total["ideal"] - total["booked"])
    total["surplus"] = max(0, total["booked"] - total["ideal"])
    return rows, total


def day_view(employee_id, d):
    """One day with its entries, and the week it belongs to (for its status)."""
    wv = week_view(employee_id, week_start(d))
    day = next(x for x in wv["days"] if x["iso"] == d.isoformat())
    return wv, day


def save_day(user, d, form):
    """Replace one day's entries (form fields e0_<n>_project / _hours / _desc)."""
    emp_id = user["employee_id"]
    start = week_start(d)
    wv, day = day_view(emp_id, d)
    valid = {p["id"]: p["name"] for p in projects()}
    valid.update({e["project_id"]: e["project"] for e in day["entries"]})
    entries = _read_entries(form, [day], valid)
    with transaction() as conn:
        row = conn.execute("SELECT * FROM timesheets WHERE employee_id = ? AND week_start = ?",
                           (emp_id, start.isoformat())).fetchone()
        if row and row["status"] == "SUBMITTED":
            raise ValidationError("This week has already been submitted. Reopen the week to make changes.")
        now = utcnow_iso()
        if row is None:
            sid = conn.execute("""INSERT INTO timesheets (employee_id, week_start, status, total_hours, updated_at)
                                  VALUES (?, ?, 'DRAFT', 0, ?)""", (emp_id, start.isoformat(), now)).lastrowid
        else:
            sid = row["id"]
        conn.execute("DELETE FROM timesheet_entries WHERE timesheet_id = ? AND work_date = ?", (sid, d.isoformat()))
        for work_date, pid, hours, desc, seq in entries:
            conn.execute("""INSERT INTO timesheet_entries (timesheet_id, work_date, project_id, hours, description, seq)
                            VALUES (?, ?, ?, ?, ?, ?)""", (sid, work_date, pid, hours, desc, seq))
        total = conn.execute("SELECT COALESCE(SUM(hours), 0) FROM timesheet_entries WHERE timesheet_id = ?",
                             (sid,)).fetchone()[0]
        conn.execute("UPDATE timesheets SET total_hours = ?, updated_at = ? WHERE id = ?", (total, now, sid))
        audit.log("TIMESHEET_SAVED", "timesheet", sid,
                  new={"day": d.isoformat(), "hours": sum(e[2] for e in entries), "entries": len(entries)})
    return sum(e[2] for e in entries)


def submit_week(user, start):
    with transaction() as conn:
        row = conn.execute("SELECT * FROM timesheets WHERE employee_id = ? AND week_start = ?",
                           (user["employee_id"], start.isoformat())).fetchone()
        if row is None or not conn.execute("SELECT 1 FROM timesheet_entries WHERE timesheet_id = ?", (row["id"],)).fetchone():
            raise ValidationError("Add at least one entry before submitting the week.")
        if row["status"] == "SUBMITTED":
            raise ValidationError("This week has already been submitted.")
        conn.execute("UPDATE timesheets SET status = 'SUBMITTED', submitted_at = ?, updated_at = ? WHERE id = ?",
                     (utcnow_iso(), utcnow_iso(), row["id"]))
        audit.log("TIMESHEET_SUBMITTED", "timesheet", row["id"], new={"week": start.isoformat(), "hours": row["total_hours"]})
    return row["total_hours"]


def _read_entries(form, days, valid_projects):
    out, problems = [], []
    for i, d in enumerate(days):
        day_total = 0
        for n in range(MAX_ROWS_PER_DAY):
            pid = (form.get(f"e{i}_{n}_project") or "").strip()
            hrs = (form.get(f"e{i}_{n}_hours") or "").strip()
            desc = (form.get(f"e{i}_{n}_desc") or "").strip()
            if not (pid or hrs or desc):
                continue
            label = f"{d['date']:%a %d %b}"
            if d["locked"]:
                problems.append(f"{label}: hours cannot be entered for {'a future date' if d['future'] else 'this date'}.")
                continue
            if not pid.isdigit() or int(pid) not in valid_projects:
                problems.append(f"{label}: choose a project.")
                continue
            if hrs not in HOUR_CHOICES:
                problems.append(f"{label}: choose the hours, a whole number from 1 to {MAX_HOURS_PER_ROW}.")
                continue
            h = int(hrs)
            if not desc:
                problems.append(f"{label}: add a short note of the task for {valid_projects[int(pid)]}.")
                continue
            try:
                desc = clean_text(desc, "Task note", max_len=300)
            except ValidationError as exc:
                problems.append(f"{label}: {exc}")
                continue
            day_total += h
            out.append((d["iso"], int(pid), h, desc, n))
        if day_total > MAX_HOURS_PER_DAY:
            problems.append(f"{d['date']:%a %d %b}: {day_total:g} hours in one day is more than {MAX_HOURS_PER_DAY}.")
    if problems:
        raise ValidationError(" ".join(problems))
    return out


def reopen(user, start):
    with transaction() as conn:
        row = conn.execute("SELECT * FROM timesheets WHERE employee_id = ? AND week_start = ?",
                           (user["employee_id"], start.isoformat())).fetchone()
        if row is None or row["status"] != "SUBMITTED":
            raise ValidationError("Only a submitted week can be reopened.")
        conn.execute("UPDATE timesheets SET status = 'DRAFT', submitted_at = NULL, updated_at = ? WHERE id = ?",
                     (utcnow_iso(), row["id"]))
        audit.log("TIMESHEET_REOPENED", "timesheet", row["id"], prev={"status": "SUBMITTED"}, new={"status": "DRAFT"},
                  remarks=f"Week of {fmt_date(start)} reopened by the employee")


def my_weeks(employee_id, limit=26):
    return query("SELECT * FROM timesheets WHERE employee_id = ? ORDER BY week_start DESC LIMIT ?", (employee_id, limit))


def team_week(scope_ids, start):
    """Status of each active employee in scope for the week."""
    where, params = "", []
    if scope_ids is not None:
        ids = sorted(scope_ids) or [-1]
        where, params = f" AND e.id IN ({','.join('?' for _ in ids)})", ids
    rows = query(f"""SELECT e.id, e.emp_code, e.full_name, g.name AS designation, d.name AS department,
                            t.status, t.total_hours, t.submitted_at
                       FROM employees e LEFT JOIN designations g ON g.id = e.designation_id
                       LEFT JOIN departments d ON d.id = e.department_id
                       LEFT JOIN timesheets t ON t.employee_id = e.id AND t.week_start = ?
                      WHERE e.status = 'ACTIVE'{where} ORDER BY e.full_name""", [start.isoformat()] + params)
    out = []
    for r in rows:
        expected = sum(d["expected"] for d in day_info(r["id"], start) if not d["future"])
        out.append({**dict(r), "expected": expected,
                    "short": expected > 0 and (r["total_hours"] or 0) < expected})
    return out


def month_report(scope_ids, month):
    """Hours by project and by person for a month (all saved entries, submitted or not)."""
    y, m = (int(x) for x in month.split("-"))
    first = date(y, m, 1)
    last = (date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1))
    where, params = "", [first.isoformat(), last.isoformat()]
    if scope_ids is not None:
        ids = sorted(scope_ids) or [-1]
        where = f" AND t.employee_id IN ({','.join('?' for _ in ids)})"
        params += ids
    rows = query(f"""SELECT e.emp_code, e.full_name, p.name AS project, en.work_date, en.hours, en.description, t.status
                       FROM timesheet_entries en JOIN timesheets t ON t.id = en.timesheet_id
                       JOIN employees e ON e.id = t.employee_id JOIN timesheet_projects p ON p.id = en.project_id
                      WHERE en.work_date BETWEEN ? AND ?{where} ORDER BY e.full_name, en.work_date""", params)
    by_project, by_person = {}, {}
    for r in rows:
        by_project[r["project"]] = by_project.get(r["project"], 0) + r["hours"]
        key = (r["emp_code"], r["full_name"])
        person = by_person.setdefault(key, {})
        person[r["project"]] = person.get(r["project"], 0) + r["hours"]
    return rows, sorted(by_project.items(), key=lambda kv: -kv[1]), by_person, (first, last)


def report_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Employee ID", "Name", "Date", "Project", "Hours", "Task", "Week status"])
    for r in rows:
        w.writerow([r["emp_code"], r["full_name"], r["work_date"], r["project"], f"{r['hours']:g}", r["description"],
                    r["status"].title()])
    return "﻿" + buf.getvalue()  # BOM so Excel opens Marathi / special characters correctly


# ---------------------------------------------------------------- project list (HR / admin)
PROJECT_STATUSES = {"ACTIVE": "Active", "ON_HOLD": "On hold", "COMPLETED": "Completed"}
PROJECT_FIELDS = ("name", "code", "description", "department_id", "head_employee_id", "start_date", "end_date",
                  "funding", "status")
PROJECT_SELECT = """SELECT p.*, d.name AS department, h.full_name AS head_name,
                           (SELECT COALESCE(SUM(hours), 0) FROM timesheet_entries WHERE project_id = p.id) AS hours,
                           (SELECT COUNT(DISTINCT t.employee_id) FROM timesheet_entries en
                              JOIN timesheets t ON t.id = en.timesheet_id WHERE en.project_id = p.id) AS people
                      FROM timesheet_projects p LEFT JOIN departments d ON d.id = p.department_id
                      LEFT JOIN employees h ON h.id = p.head_employee_id"""


def project_list(status=None):
    sql, params = PROJECT_SELECT, []
    if status in PROJECT_STATUSES:
        sql += " WHERE p.status = ?"
        params = [status]
    return query(sql + " ORDER BY CASE p.status WHEN 'ACTIVE' THEN 0 WHEN 'ON_HOLD' THEN 1 ELSE 2 END, p.name", params)


def project(project_id):
    return query(PROJECT_SELECT + " WHERE p.id = ?", (project_id,), one=True)


def project_people(project_id):
    """Who has booked time on the project, most hours first."""
    return query("""SELECT e.id, e.full_name, e.emp_code, SUM(en.hours) AS hours, MAX(en.work_date) AS last_day
                      FROM timesheet_entries en JOIN timesheets t ON t.id = en.timesheet_id
                      JOIN employees e ON e.id = t.employee_id
                     WHERE en.project_id = ? GROUP BY e.id ORDER BY hours DESC""", (project_id,))


def _opt_date(value, label):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValidationError(f"{label} is not a valid date.")


def _read_project(form):
    data = {
        "name": clean_text(form.get("name"), "Project name", required=True, max_len=100),
        "code": clean_text(form.get("code"), "Project code", max_len=20).upper() or None,
        "description": clean_text(form.get("description"), "Description", max_len=500) or None,
        "funding": clean_text(form.get("funding"), "Funding source / client", max_len=150) or None,
        "start_date": _opt_date(form.get("start_date"), "Start date"),
        "end_date": _opt_date(form.get("end_date"), "End date"),
        "status": form.get("status") or "ACTIVE",
    }
    if data["status"] not in PROJECT_STATUSES:
        raise ValidationError("Choose a valid status.")
    if data["start_date"] and data["end_date"] and data["end_date"] < data["start_date"]:
        raise ValidationError("The end date cannot be before the start date.")
    for key, label, sql in (
            ("department_id", "department", "SELECT 1 FROM departments WHERE id = ?"),
            ("head_employee_id", "project head", "SELECT 1 FROM employees WHERE id = ? AND status = 'ACTIVE'")):
        raw = (form.get(key) or "").strip()
        if not raw:
            data[key] = None
        elif not raw.isdigit() or not query(sql, (int(raw),), one=True):
            raise ValidationError(f"Choose a valid {label}.")
        else:
            data[key] = int(raw)
    return data


def save_project(form, project_id=None):
    """Add or update a project. Only Active projects are offered in the timesheet; past entries always stay."""
    data = _read_project(form)
    data["is_active"] = 1 if data["status"] == "ACTIVE" else 0
    cols = PROJECT_FIELDS + ("is_active",)
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM timesheet_projects WHERE lower(name) = lower(?) AND id IS NOT ?",
                        (data["name"], project_id)).fetchone():
            raise ValidationError("A project with that name already exists.")
        if data["code"] and conn.execute("SELECT 1 FROM timesheet_projects WHERE code = ? AND id IS NOT ?",
                                         (data["code"], project_id)).fetchone():
            raise ValidationError(f"The project code {data['code']} is already used.")
        if project_id is None:
            pid = conn.execute(f"INSERT INTO timesheet_projects ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                               [data[c] for c in cols]).lastrowid
            audit.log("TIMESHEET_PROJECT_ADDED", "timesheet_project", pid, new={c: data[c] for c in PROJECT_FIELDS})
            return pid
        old = conn.execute("SELECT * FROM timesheet_projects WHERE id = ?", (project_id,)).fetchone()
        if old is None:
            raise ValidationError("Project not found.")
        conn.execute(f"UPDATE timesheet_projects SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                     [data[c] for c in cols] + [project_id])
        changed = {c: data[c] for c in PROJECT_FIELDS if old[c] != data[c]}
        if changed:
            audit.log("TIMESHEET_PROJECT_UPDATED", "timesheet_project", project_id,
                      prev={c: old[c] for c in changed}, new=changed)
        return project_id
