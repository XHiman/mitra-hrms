"""Analytics for supervisors: time booked, projects and leave for a team, a department or the whole office.

Scopes: a supervisor's own reporting chain ('analytics.view_team'); the whole office or any department
('analytics.view_office': CEO, Joint CEO, HR). Figures follow the timesheet's own day rules
(timesheet.build_days) and stop at today, so the current month is measured so far:
  ideal        = ideal hours of the days in the period
  booked       = project hours + paid leave hours
  utilisation  = booked / ideal
Only active employees are included. No leave reasons or task notes are shown, only totals.
"""
from datetime import date, timedelta

from ..core.db import query
from ..core.utils import today
from . import timesheet as ts
from . import workflow

PERIODS = {"this": "This month", "last": "Last month", "3": "Last 3 months", "6": "Last 6 months",
           "12": "Last 12 months"}
VIEWS = {"people": "Compare people", "projects": "Projects", "departments": "Departments", "leave": "Leave",
         "trend": "Monthly trend"}


# ---------------------------------------------------------------- scope and period
def scope_options(user):
    opts = []
    if "analytics.view_team" in user["permissions"]:
        opts.append(("team", "My team (reporting to me)"))
    if "analytics.view_office" in user["permissions"]:
        opts.append(("office", "Whole office"))
        for d in query("SELECT id, name FROM departments ORDER BY name"):
            opts.append((f"dept:{d['id']}", f"Department: {d['name']}"))
    return opts


def employees_in(user, scope):
    """Active employees in the chosen scope, or None if the user may not use that scope."""
    allowed = {k for k, _ in scope_options(user)}
    if scope not in allowed:
        return None
    sql = """SELECT e.id, e.emp_code, e.full_name, e.joining_date, e.contract_end_date, e.department_id,
                    d.name AS department, g.name AS designation
               FROM employees e LEFT JOIN departments d ON d.id = e.department_id
               LEFT JOIN designations g ON g.id = e.designation_id
              WHERE e.status = 'ACTIVE'"""
    params = []
    if scope == "team":
        ids = sorted(workflow.subordinate_employee_ids(user["employee_id"])) or [-1]
        sql += f" AND e.id IN ({','.join('?' * len(ids))})"
        params = ids
    elif scope.startswith("dept:"):
        sql += " AND e.department_id = ?"
        params = [int(scope.split(":", 1)[1])]
    return query(sql + " ORDER BY e.full_name", params)


def _add_months(d, n):
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def period_range(key):
    """First and last day of the period; the last day never goes past today."""
    t = today()
    this = t.replace(day=1)
    if key == "last":
        first = _add_months(this, -1)
        return first, this - timedelta(days=1)
    months = int(key) if key in ("3", "6", "12") else 1
    return _add_months(this, -(months - 1)), t


def _month_key(d):
    return d.strftime("%Y-%m")


# ---------------------------------------------------------------- figures
def compute(emps, start, end):
    t = today()
    end = min(end, t)
    rules = ts.day_rules()
    out = {"people": [], "projects": {}, "departments": {}, "leave_types": {}, "months": {},
           "total": {"ideal": 0, "project": 0, "leave_hours": 0, "booked": 0, "leave_days": 0, "unpaid_days": 0,
                     "weeks_due": 0, "weeks_submitted": 0}}
    if not emps or end < start:
        return _finish(out)
    ids = [e["id"] for e in emps]
    marks = ",".join("?" * len(ids))
    n = (end - start).days + 1
    hols = {r["date"]: r["name"] for r in query("SELECT date, name FROM holidays WHERE date BETWEEN ? AND ?",
                                                 (start.isoformat(), end.isoformat()))}
    leave = {}
    for r in query(ts.LEAVE_SQL + f" AND r.employee_id IN ({marks}) ORDER BY r.from_date",
                   [end.isoformat(), start.isoformat()] + ids):
        leave.setdefault(r["employee_id"], []).append(r)
    entries = {}
    for r in query(f"""SELECT t.employee_id, en.work_date, en.hours, p.id AS project_id, p.name AS project
                         FROM timesheet_entries en JOIN timesheets t ON t.id = en.timesheet_id
                         JOIN timesheet_projects p ON p.id = en.project_id
                        WHERE en.work_date BETWEEN ? AND ? AND t.employee_id IN ({marks})""",
                   [start.isoformat(), end.isoformat()] + ids):
        entries.setdefault(r["employee_id"], {}).setdefault(r["work_date"], []).append(r)
    submitted = {(r["employee_id"], r["week_start"]) for r in query(
        f"""SELECT employee_id, week_start FROM timesheets WHERE status = 'SUBMITTED' AND week_start BETWEEN ? AND ?
              AND employee_id IN ({marks})""",
        [ts.week_start(start).isoformat(), end.isoformat()] + ids)}
    # each month of the period, so the trend shows months with no data too
    m = start.replace(day=1)
    while m <= end:
        out["months"][_month_key(m)] = {"month": m, "ideal": 0, "project": 0, "leave_hours": 0}
        m = _add_months(m, 1)

    for e in emps:
        p = {"emp": e, "ideal": 0, "project": 0, "leave_hours": 0, "leave_days": 0, "unpaid_days": 0,
             "projects": {}, "weeks_due": 0, "weeks_submitted": 0}
        week_ideal = {}
        for d in ts.build_days(e, start, n, hols, leave.get(e["id"], []), rules):
            mk = out["months"][_month_key(d["date"])]
            p["ideal"] += d["ideal"]
            mk["ideal"] += d["ideal"]
            p["leave_hours"] += d["leave_hours"]
            mk["leave_hours"] += d["leave_hours"]
            if d["leave"]:
                share = 0.5 if d["leave"]["half"] else 1
                p["leave_days" if d["leave"]["paid"] else "unpaid_days"] += share
                lt = out["leave_types"].setdefault(d["leave"]["name"], {"days": 0, "people": set()})
                lt["days"] += share
                lt["people"].add(e["id"])
            for x in entries.get(e["id"], {}).get(d["iso"], []):
                p["project"] += x["hours"]
                mk["project"] += x["hours"]
                p["projects"][x["project"]] = p["projects"].get(x["project"], 0) + x["hours"]
                pr = out["projects"].setdefault(x["project"], {"id": x["project_id"], "hours": 0, "people": {}})
                pr["hours"] += x["hours"]
                pr["people"][e["id"]] = pr["people"].get(e["id"], 0) + x["hours"]
            wk = ts.week_start(d["date"])
            week_ideal[wk] = week_ideal.get(wk, 0) + d["ideal"]
        for wk, ideal in week_ideal.items():  # completed weeks that needed hours
            if ideal and wk + timedelta(days=6) < t:
                p["weeks_due"] += 1
                p["weeks_submitted"] += (e["id"], wk.isoformat()) in submitted
        p["booked"] = p["project"] + p["leave_hours"]
        p["shortfall"] = max(0, p["ideal"] - p["booked"])
        p["util"] = round(100 * p["booked"] / p["ideal"]) if p["ideal"] else None
        out["people"].append(p)
        dept = out["departments"].setdefault(e["department"] or "No department",
                                             {"staff": 0, "ideal": 0, "project": 0, "leave_hours": 0, "leave_days": 0})
        dept["staff"] += 1
        for k in ("ideal", "project", "leave_hours", "leave_days"):
            dept[k] += p[k]
        for k in ("ideal", "project", "leave_hours", "leave_days", "unpaid_days", "weeks_due", "weeks_submitted"):
            out["total"][k] += p[k]
    return _finish(out)


def _pct(a, b):
    return round(100 * a / b) if b else None


def _finish(out):
    tot = out["total"]
    tot["booked"] = tot["project"] + tot["leave_hours"]
    tot["shortfall"] = max(0, tot["ideal"] - tot["booked"])
    tot["util"] = _pct(tot["booked"], tot["ideal"])
    tot["submission"] = _pct(tot["weeks_submitted"], tot["weeks_due"])
    tot["staff"] = len(out["people"])
    out["people"].sort(key=lambda p: (p["util"] is None, -(p["util"] or 0), p["emp"]["full_name"]))
    out["project_rows"] = sorted(({"name": k, **v} for k, v in out["projects"].items()), key=lambda r: -r["hours"])
    for d in out["departments"].values():
        d["booked"] = d["project"] + d["leave_hours"]
        d["util"] = _pct(d["booked"], d["ideal"])
    out["dept_rows"] = sorted(({"name": k, **v} for k, v in out["departments"].items()),
                              key=lambda r: (r["util"] is None, -(r["util"] or 0)))
    out["leave_rows"] = sorted(({"name": k, "days": v["days"], "people": len(v["people"])}
                                for k, v in out["leave_types"].items()), key=lambda r: -r["days"])
    rows = []
    for v in out["months"].values():
        v["booked"] = v["project"] + v["leave_hours"]
        v["util"] = _pct(v["booked"], v["ideal"])
        rows.append(v)
    out["month_rows"] = rows
    out["trend"] = line_chart(rows)
    return out


# ---------------------------------------------------------------- charts
def scale(rows, *keys):
    """Largest value among rows for the given keys (for bar widths); never zero."""
    return max([sum(r[k] for k in keys) for r in rows] + [1])


def line_chart(month_rows, width=640, height=220, pad=36):
    """Coordinates for an SVG chart of ideal vs booked hours per month (drawn by the template)."""
    if not month_rows:
        return None
    top = max([r["ideal"] for r in month_rows] + [r["booked"] for r in month_rows] + [1])
    step = (width - 2 * pad) / max(1, len(month_rows) - 1)

    def xy(i, v):
        return round(pad + i * step, 1), round(height - pad - (height - 2 * pad) * v / top, 1)

    pts = {"ideal": [], "booked": []}
    labels = []
    for i, r in enumerate(month_rows):
        for k in pts:
            pts[k].append(xy(i, r[k]))
        labels.append((xy(i, 0)[0], r["month"].strftime("%b %y")))
    ticks = [(round(height - pad - (height - 2 * pad) * f, 1), round(top * f)) for f in (0, 0.5, 1)]
    return {"w": width, "h": height, "pad": pad, "ticks": ticks, "labels": labels,
            "ideal": " ".join(f"{x},{y}" for x, y in pts["ideal"]),
            "booked": " ".join(f"{x},{y}" for x, y in pts["booked"]),
            "dots": pts["booked"]}
