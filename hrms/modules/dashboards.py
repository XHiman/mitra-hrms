"""Employee and supervisory dashboards, profile, leave calendar, employee-wise views."""
import calendar
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..core import audit
from ..core.db import query, transaction
from ..core.rbac import has_perm, require
from ..core.utils import ValidationError, parse_date, today, utcnow_iso
from ..services import calendar_events as events
from ..services import compoff as compoff_svc
from ..services import contracts, cycles
from ..services import timesheet as timesheet_svc
from ..services import leave as leave_svc
from ..services import workflow
from ..services.notifications import recent as recent_notifications

bp = Blueprint("dashboards", __name__)


@bp.route("/")
def home():
    if has_perm("dashboard.admin"):
        return redirect(url_for("admin.dashboard"))
    if has_perm("dashboard.supervisory"):
        return redirect(url_for("dashboards.supervisory"))
    return redirect(url_for("dashboards.employee"))


# ---------------------------------------------------------------- helpers
def supervisory_scope(user):
    """Employee ids a supervisor oversees (None = everyone). Excludes the user themself."""
    if has_perm("leave.view_all", user):
        return None
    if has_perm("leave.view_team", user):
        return workflow.subordinate_employee_ids(user["employee_id"])
    return set()


def _in_clause(ids, column="e.id"):
    if ids is None:
        return "1=1", []
    ids = sorted(ids) or [-1]
    return f"{column} IN ({','.join('?' for _ in ids)})", ids


def month_calendar(year, month, requests):
    """Weeks (Mon-Sun) of day cells, each carrying the leave entries that cover it."""
    offs = {int(x) for x in query("SELECT value FROM settings WHERE key='weekend_days'", one=True)["value"].split(",")
            if x.strip().isdigit()}
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    hols = {r["date"]: r for r in events.holidays_between(first, last)}
    bdays = {}
    for b in events.birthdays_between(first, last):
        bdays.setdefault(b["date"].isoformat(), []).append(b)
    start = first - timedelta(days=first.weekday())
    weeks, d = [], start
    t = today()
    while d <= last or d.weekday() != 0:
        if d.weekday() == 0:
            weeks.append([])
        iso = d.isoformat()
        weeks[-1].append({
            "date": d, "in_month": d.month == month, "is_today": d == t,
            "is_off": d.weekday() in offs, "holiday": hols.get(iso) if d.month == month else None,
            "birthdays": bdays.get(iso, []) if d.month == month else [],
            "entries": [r for r in requests if r["from_date"] <= iso <= r["to_date"]] if d.month == month else [],
        })
        d += timedelta(days=1)
    return weeks


def _month_args():
    t = today()
    try:
        year = int(request.args.get("y", t.year))
        month = int(request.args.get("m", t.month))
        date(year, month, 1)
    except ValueError:
        year, month = t.year, t.month
    prev = (date(year, month, 1) - timedelta(days=1))
    nxt = (date(year, month, 28) + timedelta(days=5))
    return year, month, (prev.year, prev.month), (nxt.year, nxt.month)


def _requests_overlapping(scope_ids, first, last, statuses=("APPROVED", "PENDING")):
    where = f"r.status IN ({','.join('?' for _ in statuses)}) AND r.from_date <= ? AND r.to_date >= ?"
    return leave_svc.requests_in_scope(scope_ids, where, list(statuses) + [last.isoformat(), first.isoformat()],
                                       order="r.from_date")


# ---------------------------------------------------------------- employee dashboard
@bp.route("/dashboard")
@require("leave.view_own")
def employee():
    emp_id = g.user["employee_id"]
    t = today()
    cycle = leave_svc.current_cycle(emp_id, t)
    balances = leave_svc.balances_for(emp_id, cycle)
    tracked = [b for b in balances if b["tracked"] and b["available"] is not None]
    pending = query("SELECT COUNT(*) AS n FROM leave_requests WHERE employee_id = ? AND status = 'PENDING'",
                    (emp_id,), one=True)["n"]
    taken = query("SELECT COALESCE(SUM(days), 0) AS n FROM leave_requests WHERE employee_id = ? AND status = 'APPROVED' "
                  "AND leave_year = ? AND from_date <= ?", (emp_id, cycle.key, t.isoformat()), one=True)["n"]
    upcoming = leave_svc.requests_in_scope({emp_id}, "r.status = 'APPROVED' AND r.from_date > ?", [t.isoformat()],
                                           order="r.from_date")
    y, m, prev, nxt = _month_args()
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    cal = month_calendar(y, m, _requests_overlapping({emp_id}, first, last))
    return render_template(
        "dashboards/employee.html", balances=balances, cycle=cycle, emp=leave_svc.employee(emp_id),
        contract=cycles.contract_summary(emp_id, t), cstatus=contracts.status(emp_id, t),
        total_available=sum(b["available"] for b in tracked) if tracked else None,
        pending=pending, taken=taken, upcoming=upcoming,
        recent=leave_svc.requests_for_employee(emp_id, limit=6),
        notifications=recent_notifications(g.user["id"], 8),
        birthdays=events.upcoming_birthdays(t, 30), today_date=t, week=timesheet_svc.week_view(emp_id, timesheet_svc.week_start(t)),
        last_week=timesheet_svc.week_view(emp_id, timesheet_svc.week_start(t) - timedelta(days=7)),
        last_week_start=timesheet_svc.week_start(t) - timedelta(days=7),
        next_holidays=events.holidays_between(t, t + timedelta(days=366))[:4],
        cal=cal, cal_month=date(y, m, 1), prev=prev, nxt=nxt)


# ---------------------------------------------------------------- holiday list
@bp.route("/holidays")
@require("profile.view_own")
def holidays():
    t = today()
    try:
        year = int(request.args.get("year", t.year))
        date(year, 1, 1)
    except ValueError:
        year = t.year
    rows = events.holidays_between(date(year, 1, 1), date(year, 12, 31))
    offs = {int(x) for x in query("SELECT value FROM settings WHERE key='weekend_days'", one=True)["value"].split(",")
            if x.strip().isdigit()}
    years = sorted({int(r["y"]) for r in query("SELECT DISTINCT substr(date, 1, 4) AS y FROM holidays")} | {t.year})
    items = [{"row": r, "date": date.fromisoformat(r["date"]),
              "on_weekly_off": date.fromisoformat(r["date"]).weekday() in offs} for r in rows]
    return render_template("dashboards/holidays.html", items=items, year=year, years=years, today_iso=t.isoformat())


# ---------------------------------------------------------------- supervisory dashboard
def _employee_summary(scope_ids, types):
    clause, params = _in_clause(scope_ids)
    emps = query(f"""SELECT e.id, e.emp_code, e.full_name, d.name AS department, g.name AS designation
                      FROM employees e LEFT JOIN departments d ON d.id = e.department_id
                      LEFT JOIN designations g ON g.id = e.designation_id
                     WHERE e.status = 'ACTIVE' AND {clause} ORDER BY d.name, e.full_name""", params)
    return [{"emp": e, "balances": leave_svc.balances_for(e["id"], None, types)} for e in emps]  # current cycles


@bp.route("/supervise")
@require("dashboard.supervisory")
def supervisory():
    user = g.user
    t = today()
    year = t.year  # organisation-wide counts use the calendar year; balances use each person's cycle
    y_first, y_last = date(year, 1, 1), date(year, 12, 31)
    scope = supervisory_scope(user)
    clause, params = _in_clause(scope)
    types = query("SELECT * FROM leave_types WHERE is_active = 1 ORDER BY name")

    total_employees = query(f"SELECT COUNT(*) AS n FROM employees e WHERE e.status = 'ACTIVE' AND {clause}",
                            params, one=True)["n"]
    on_leave_today = leave_svc.requests_in_scope(scope, "r.status = 'APPROVED' AND r.from_date <= ? AND r.to_date >= ?",
                                                 [t.isoformat(), t.isoformat()], order="e.full_name")
    upcoming = leave_svc.requests_in_scope(scope, "r.status = 'APPROVED' AND r.from_date > ? AND r.from_date <= ?",
                                           [t.isoformat(), (t + timedelta(days=30)).isoformat()], order="r.from_date")
    actionable = workflow.actionable_requests(user)
    pending_rows = [leave_svc.request_detail(r["id"]) for r in actionable]
    pending_claims = compoff_svc.actionable_claims(user)

    def year_count(status):
        return len(leave_svc.requests_in_scope(scope, "r.status = ? AND r.from_date BETWEEN ? AND ?",
                                               [status, y_first.isoformat(), y_last.isoformat()]))

    employee_summary = _employee_summary(scope, types)
    renewals_due, renewals_missing = [], 0
    for row in employee_summary:
        s = contracts.status(row["emp"]["id"], t)
        if s and s["state"] == "scheduled" and s["days_left"] <= 60:
            renewals_due.append({"emp": row["emp"], "s": s})
        elif s and s["state"] == "missing":
            renewals_missing += 1
    renewals_due.sort(key=lambda r: r["s"]["renewal_due"])
    utilisation = []
    for lt in types:
        if not lt["balance_tracked"]:
            continue
        ent = used = pend = 0
        configured = False
        for row in employee_summary:
            b = next(b for b in row["balances"] if b["leave_type"]["id"] == lt["id"])
            if b["entitlement"] is not None:
                configured = True
                ent += b["entitlement"]
            used += b["used"]
            pend += b["pending"]
        utilisation.append({"lt": lt, "entitlement": ent if configured else None, "used": used, "pending": pend,
                            "pct": round(100 * used / ent) if configured and ent else 0})

    dept_rows = query(f"""SELECT d.id, d.name, COUNT(e.id) AS headcount
                           FROM departments d JOIN employees e ON e.department_id = d.id AND e.status = 'ACTIVE'
                          WHERE {clause} GROUP BY d.id ORDER BY d.name""", params)
    dept_summary = []
    for d in dept_rows:
        in_dept = leave_svc.requests_in_scope(scope, "e.department_id = ? AND r.from_date BETWEEN ? AND ?",
                                              [d["id"], y_first.isoformat(), y_last.isoformat()])
        dept_summary.append({
            "name": d["name"], "headcount": d["headcount"],
            "on_leave": sum(1 for r in on_leave_today if r["department"] == d["name"]),
            "days_used": sum(r["days"] for r in in_dept if r["status"] == "APPROVED"),
            "pending": sum(1 for r in in_dept if r["status"] == "PENDING"),
        })

    decided_where = "r.status IN ('APPROVED', 'REJECTED') AND r.decided_at IS NOT NULL"
    recent_decisions = leave_svc.requests_in_scope(scope, decided_where, order="r.decided_at DESC", limit=8)
    if scope is not None:  # also include decisions this user personally took
        mine = leave_svc.requests_in_scope(None, decided_where + " AND r.decided_by = ?", [user["id"]],
                                           order="r.decided_at DESC", limit=8)
        merged = {r["id"]: r for r in list(recent_decisions) + list(mine)}
        recent_decisions = sorted(merged.values(), key=lambda r: r["decided_at"], reverse=True)[:8]

    y, m, prev, nxt = _month_args()
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    cal = month_calendar(y, m, _requests_overlapping(scope, first, last, ("APPROVED",)))

    return render_template(
        "dashboards/supervisory.html", year=year, y_first=y_first, y_last=y_last, scope_all=scope is None,
        total_employees=total_employees, on_leave_today=on_leave_today, upcoming=upcoming,
        pending_rows=pending_rows, pending_claims=pending_claims, approved_count=year_count("APPROVED"), rejected_count=year_count("REJECTED"),
        utilisation=utilisation, employee_summary=employee_summary, types=types, dept_summary=dept_summary,
        renewals_due=renewals_due, renewals_missing=renewals_missing,
        recent_decisions=recent_decisions, cal=cal, cal_month=date(y, m, 1), prev=prev, nxt=nxt)


# ---------------------------------------------------------------- profile & employee views
@bp.route("/profile")
@require("profile.view_own")
def profile():
    emp = leave_svc.employee(g.user["employee_id"])
    cycle = leave_svc.current_cycle(emp["id"])
    return render_template("dashboards/profile.html", emp=emp, balances=leave_svc.balances_for(emp["id"], cycle),
                           cycle=cycle, contract=cycles.contract_summary(emp["id"], today()),
                           cstatus=contracts.status(emp["id"]), **_people_context(emp["id"]))


@bp.route("/profile/birthday", methods=["POST"])
@require("profile.view_own")
def profile_birthday():
    """Employees may record their own date of birth and choose whether colleagues see the day and month."""
    emp_id = g.user["employee_id"]
    try:
        raw = (request.form.get("date_of_birth") or "").strip()
        dob = parse_date(raw, "Date of birth").isoformat() if raw else None
        if dob and not dob.startswith("1900-") and not ("1940-01-01" <= dob <= (today() - timedelta(days=365 * 15)).isoformat()):
            raise ValidationError("Please enter a valid date of birth.")
        show = 1 if request.form.get("show_birthday") else 0
        with transaction() as conn:
            old = conn.execute("SELECT date_of_birth, show_birthday FROM employees WHERE id = ?", (emp_id,)).fetchone()
            if (old["date_of_birth"], old["show_birthday"]) != (dob, show):
                conn.execute("UPDATE employees SET date_of_birth = ?, show_birthday = ?, updated_at = ? WHERE id = ?",
                             (dob, show, utcnow_iso(), emp_id))
                audit.log("PROFILE_UPDATED", "employee", emp_id,
                          prev={"date_of_birth_set": bool(old["date_of_birth"]), "show_birthday": old["show_birthday"]},
                          new={"date_of_birth_set": bool(dob), "show_birthday": show},
                          remarks="Birthday details updated by the employee")
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Birthday details saved.", "success")
    return redirect(url_for("dashboards.profile") + "#birthday")


@bp.route("/pending")
@require("leave.approve", "compoff.approve", "contracts.manage", "payroll.manage")
def pending():
    from ..services import tasks
    return render_template("dashboards/pending.html", t=tasks.for_user(g.user))


@bp.route("/search")
@require("profile.view_own")
def search():
    q = (request.args.get("q") or "").strip()[:60]
    people, leave_hit = [], None
    if len(q) >= 2:
        like = f"%{q}%"
        rows = query("""SELECT e.id, e.emp_code, e.full_name, g.name AS designation, d.name AS department
                          FROM employees e LEFT JOIN designations g ON g.id = e.designation_id
                          LEFT JOIN departments d ON d.id = e.department_id LEFT JOIN users u ON u.employee_id = e.id
                         WHERE e.status = 'ACTIVE' AND (e.full_name LIKE ? OR e.emp_code LIKE ? OR g.name LIKE ?
                               OR d.name LIKE ? OR u.username LIKE ?)
                         ORDER BY e.full_name LIMIT 50""", (like, like, like, like, like))
        for r in rows:
            own = r["id"] == g.user["employee_id"]
            people.append({"r": r, "own": own,
                           "open": own or ((has_perm("leave.view_team") or has_perm("leave.view_all")
                                            or has_perm("leave.approve")) and workflow.can_view_employee(g.user, r["id"]))})
    number = q.lstrip("#")
    if number.isdigit():
        req = leave_svc.request_detail(int(number))
        if req is not None and workflow.can_view_request(g.user, req):
            leave_hit = req
    return render_template("dashboards/search.html", q=q, people=people, leave_hit=leave_hit)


@bp.route("/contracts")
@require("contracts.manage")
def renewals():
    rows = contracts.overview()
    return render_template("dashboards/renewals.html", rows=rows,
                           missing=sum(1 for r in rows if r["s"]["state"] == "missing"),
                           soon=sum(1 for r in rows if r["s"]["state"] == "scheduled" and r["s"]["days_left"] <= 30))


@bp.route("/employees/<int:employee_id>/renewals", methods=["POST"])
@require("contracts.manage")
def renewal_add(employee_id):
    try:
        if employee_id == g.user["employee_id"]:
            raise ValidationError("You cannot record your own contract renewal. Please ask the administrator.")
        contracts.add_renewal(employee_id, request.form.get("renewal_date"), request.form.get("note"))
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Renewal date recorded. Leave balances follow the new contract term.", "success")
    return redirect(url_for("dashboards.employee_history", employee_id=employee_id) + "#contract")


@bp.route("/employees/<int:employee_id>/contract-end", methods=["POST"])
@require("contracts.manage")
def contract_end_set(employee_id):
    try:
        if employee_id == g.user["employee_id"]:
            raise ValidationError("You cannot set your own contract end date. Please ask the administrator.")
        contracts.set_contract_end(employee_id, request.form.get("contract_end_date"))
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Contract end date saved. Timesheet and leave are closed after that date.", "success")
    return redirect(url_for("dashboards.employee_history", employee_id=employee_id) + "#contract")


@bp.route("/employees/<int:employee_id>/renewals/<int:renewal_id>/delete", methods=["POST"])
@require("contracts.manage")
def renewal_delete(employee_id, renewal_id):
    if employee_id == g.user["employee_id"]:
        abort(403)
    try:
        contracts.remove_renewal(employee_id, renewal_id)
    except ValidationError:
        abort(404)
    flash("Renewal date removed.", "success")
    return redirect(url_for("dashboards.employee_history", employee_id=employee_id) + "#contract")


@bp.route("/employees/<int:employee_id>")
@require("leave.view_team", "leave.view_all", "leave.approve", "compoff.approve")
def employee_history(employee_id):
    if not (workflow.can_view_employee(g.user, employee_id)
            or any(c["employee_id"] == employee_id for c in compoff_svc.actionable_claims(g.user))):
        abort(403)
    emp = leave_svc.employee(employee_id)
    if emp is None:
        abort(404)
    cycle = leave_svc.current_cycle(employee_id)
    return render_template("dashboards/employee_history.html", emp=emp,
                           balances=leave_svc.balances_for(employee_id, cycle), cycle=cycle,
                           contract=cycles.contract_summary(employee_id, today()), cstatus=contracts.status(employee_id),
                           renewals=query("SELECT * FROM contract_renewals WHERE employee_id = ? ORDER BY renewal_date",
                                          (employee_id,)),
                           requests=leave_svc.requests_for_employee(employee_id),
                           claims=compoff_svc.claims_for_employee(employee_id), **_people_context(employee_id))


def _people_context(employee_id):
    from ..services import people
    team = people.team_of(employee_id)
    mine = people.projects_of(employee_id)
    ctx = {"team": team, "my_projects": mine}
    if has_perm("people.manage") and employee_id != g.user["employee_id"]:
        taken = {p["id"] for p in mine}
        ctx.update(ro_choices=people.active_people(exclude={employee_id}),
                   member_choices=people.active_people(exclude={employee_id} | {t["id"] for t in team}),
                   project_choices=[p for p in timesheet_svc.projects() if p["id"] not in taken])
    return ctx


def _people_action(employee_id, action):
    """Runs one reporting-line / project change from the person's page and returns to it."""
    if query("SELECT 1 FROM employees WHERE id = ?", (employee_id,), one=True) is None:
        abort(404)
    try:
        msg = action()
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash(msg, "success")
    return redirect(url_for("dashboards.employee_history", employee_id=employee_id) + "#people")


@bp.route("/employees/<int:employee_id>/reporting-officer", methods=["POST"])
@require("people.manage")
def set_reporting_officer(employee_id):
    from ..services import people

    def run():
        changed = people.set_reporting_officer(g.user, employee_id, request.form.get("reporting_officer_id"))
        return "Reporting officer updated." if changed else "No change: that is already the reporting officer."
    return _people_action(employee_id, run)


@bp.route("/employees/<int:employee_id>/team", methods=["POST"])
@require("people.manage")
def team_add(employee_id):
    from ..services import people
    return _people_action(employee_id, lambda: f"{people.add_team_member(g.user, employee_id, request.form.get('member_id'))['full_name']} now reports here.")


@bp.route("/employees/<int:employee_id>/team/<int:member_id>/remove", methods=["POST"])
@require("people.manage")
def team_remove(employee_id, member_id):
    from ..services import people
    return _people_action(employee_id, lambda: f"{people.remove_team_member(g.user, employee_id, member_id)['full_name']} no longer reports here.")


@bp.route("/employees/<int:employee_id>/projects", methods=["POST"])
@require("people.manage")
def project_add(employee_id):
    from ..services import people
    return _people_action(employee_id, lambda: f"Added to {people.add_project(g.user, employee_id, request.form.get('project_id'))['name']}.")


@bp.route("/employees/<int:employee_id>/projects/<int:project_id>/remove", methods=["POST"])
@require("people.manage")
def project_remove(employee_id, project_id):
    from ..services import people
    return _people_action(employee_id, lambda: f"Removed from {people.remove_project(g.user, employee_id, project_id)['name']}.")
