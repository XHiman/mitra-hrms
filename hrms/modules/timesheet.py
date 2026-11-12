"""Weekly timesheets: staff fill and submit; supervisors and HR view team status and reports."""
from datetime import date, timedelta

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from ..core.db import query
from ..core.rbac import has_perm, require
from ..core.utils import STAFF_GROUPS, ValidationError, staff_group, staff_group_arg, today
from ..services import timesheet as ts
from ..services import workflow

bp = Blueprint("timesheet", __name__, url_prefix="/timesheet")


def _scope():
    """Employees whose timesheets this user may see (None = everyone); never includes the user."""
    if has_perm("timesheet.manage") or has_perm("leave.view_all"):
        return None
    return workflow.visible_employee_ids(g.user) - {g.user["employee_id"]}


def _rows_for(view, form=None):
    """Entry rows per day for the form: saved entries (or what was just typed) plus a few blank rows."""
    for i, d in enumerate(view["days"]):
        if form is not None:
            rows = [{"project": form.get(f"e{i}_{n}_project", ""), "hours": form.get(f"e{i}_{n}_hours", ""),
                     "desc": form.get(f"e{i}_{n}_desc", "")} for n in range(ts.MAX_ROWS_PER_DAY)]
            rows = [r for r in rows if r["project"] or r["hours"] or r["desc"]]
        else:
            rows = [{"project": str(e["project_id"]), "hours": f"{e['hours']:g}", "desc": e["description"]}
                    for e in d["entries"]]
        blanks = 0 if d["locked"] else (1 if d["expected"] == 0 else 2)
        d["rows"] = (rows + [{"project": "", "hours": "", "desc": ""}] * blanks)[:ts.MAX_ROWS_PER_DAY]
    return view


def _month_arg():
    raw = request.args.get("month") or ""
    try:
        y, m = (int(x) for x in raw.split("-"))
        date(y, m, 1)
        return y, m
    except ValueError:
        t = today()
        return t.year, t.month


def _neighbours(y, m):
    prev = date(y - (m == 1), 12 if m == 1 else m - 1, 1).strftime("%Y-%m")
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1).strftime("%Y-%m")
    return prev, nxt


def _render_month(y, m, sel=None, error=None, form=None):
    """The calendar page; with `sel` (a date) the editor for that day opens beside the calendar."""
    t = today()
    prev, nxt = _neighbours(y, m)
    editor = None
    if sel is not None:
        wv, info = ts.day_view(g.user["employee_id"], sel)
        info = _rows_for({"days": [info]}, form)["days"][0]
        if not info["locked"]:
            while len(info["rows"]) < 3:
                info["rows"].append({"project": "", "hours": "", "desc": ""})
        editor = {"d": sel, "info": info, "sheet": wv["sheet"], "error": error, "week": ts.week_start(sel).isoformat(),
                  "prev": (sel - timedelta(days=1)).isoformat(),
                  "nxt": (sel + timedelta(days=1)).isoformat() if sel < t else None}
    return render_template("timesheet/month.html", mv=ts.month_view(g.user["employee_id"], y, m), prev=prev,
                           nxt=nxt if date(y, m, 1) < t.replace(day=1) else None, own=True, emp=None,
                           daily=ts.daily_hours(), today_iso=t.isoformat(), editor=editor, projects=ts.projects(),
                           mine={r['project_id'] for r in query('SELECT project_id FROM project_members WHERE employee_id = ?', (g.user['employee_id'],))})


@bp.route("/")
@require("profile.view_own")
def month():
    if request.args.get("week"):  # older links to a week
        return redirect(url_for("timesheet.week", week=request.args["week"]))
    t = today()
    sel = None
    if request.args.get("day"):
        try:
            sel = date.fromisoformat(request.args["day"])
        except ValueError:
            sel = None
        if sel and sel > t:
            flash("Hours can be booked only for today or earlier.", "error")
            sel = None
    if sel:
        y, m = sel.year, sel.month
    else:
        y, m = _month_arg()
    if date(y, m, 1) > t.replace(day=1):
        return redirect(url_for("timesheet.month"))
    return _render_month(y, m, sel)


@bp.route("/summary")
@require("profile.view_own")
def summary():
    year = request.args.get("year", type=int) or today().year
    rows, total = ts.year_summary(g.user["employee_id"], year)
    return render_template("timesheet/summary.html", rows=rows, total=total, year=year, own=True, emp=None,
                           this_year=today().year)


@bp.route("/day/<day>", methods=["GET", "POST"])
@require("profile.view_own")
def day(day):
    """Saving one day from the calendar editor. Older links to a day open the calendar on that day."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        abort(404)
    if d > today():
        flash("Hours can be booked only for today or earlier.", "error")
        return redirect(url_for("timesheet.month", month=d.strftime("%Y-%m")))
    if request.method == "GET":
        return redirect(url_for("timesheet.month", day=d.isoformat()) + "#editor")
    try:
        hours = ts.save_day(g.user, d, request.form)
    except ValidationError as exc:
        return _render_month(d.year, d.month, d, error=str(exc), form=request.form)
    flash(f"{d.strftime('%A %d %b')}: {hours:g} project hours saved.", "success")
    if request.form.get("next") == "next-day" and d < today():
        return redirect(url_for("timesheet.month", day=(d + timedelta(days=1)).isoformat()) + "#editor")
    return redirect(url_for("timesheet.month", day=d.isoformat()) + "#editor")


@bp.route("/week/submit", methods=["POST"])
@require("profile.view_own")
def week_submit():
    start = ts.parse_week(request.form.get("week"))
    try:
        hours = ts.submit_week(g.user, start)
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Week of {start.strftime('%d %b')} submitted ({hours:g} hours).", "success")
    return redirect(url_for("timesheet.month", month=request.form.get("month") or start.strftime("%Y-%m")))


@bp.route("/week/reopen", methods=["POST"])
@require("profile.view_own")
def week_reopen():
    start = ts.parse_week(request.form.get("week"))
    try:
        ts.reopen(g.user, start)
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("The week is open again for changes. Submit it again when done.", "success")
    day = request.form.get("day")
    if day:
        return redirect(url_for("timesheet.month", day=day) + "#editor")
    return redirect(url_for("timesheet.month", month=start.strftime("%Y-%m")))


@bp.route("/week")
@require("profile.view_own")
def week():
    """Read-only week view; hours are booked on the calendar."""
    start = ts.parse_week(request.args.get("week"))
    if start > ts.week_start(today()):
        return redirect(url_for("timesheet.week"))
    return render_template("timesheet/view.html", v=ts.week_view(g.user["employee_id"], start), own=True, emp=None,
                           start=start, end=start + timedelta(days=6), prev=(start - timedelta(days=7)).isoformat(),
                           nxt=(start + timedelta(days=7)).isoformat(), this_week=ts.week_start(today()))


@bp.route("/team")
@require("leave.view_team", "leave.view_all", "timesheet.manage")
def team():
    start = ts.parse_week(request.args.get("week"))
    rows = ts.team_week(_scope(), start)
    group = staff_group_arg(request.args.get("group"))
    if group:
        rows = [r for r in rows if staff_group(r["designation"]) == group]
    counts = {"submitted": sum(1 for r in rows if r["status"] == "SUBMITTED"),
              "draft": sum(1 for r in rows if r["status"] == "DRAFT"),
              "missing": sum(1 for r in rows if not r["status"]), "short": sum(1 for r in rows if r["short"])}
    return render_template("timesheet/team.html", rows=rows, counts=counts, start=start, end=start + timedelta(days=6),
                           prev=(start - timedelta(days=7)).isoformat(), nxt=(start + timedelta(days=7)).isoformat(),
                           this_week=ts.week_start(today()), group=group, groups=STAFF_GROUPS)


@bp.route("/view/<int:employee_id>")
@require("leave.view_team", "leave.view_all", "timesheet.manage")
def view(employee_id):
    scope = _scope()
    if scope is not None and employee_id not in scope:
        abort(403)
    emp = query("""SELECT e.*, g.name AS designation FROM employees e LEFT JOIN designations g ON g.id = e.designation_id
                   WHERE e.id = ?""", (employee_id,), one=True)
    if emp is None:
        abort(404)
    if request.args.get("year"):  # month-wise summary of the person
        year = request.args.get("year", type=int) or today().year
        rows, total = ts.year_summary(employee_id, year)
        return render_template("timesheet/summary.html", rows=rows, total=total, year=year, own=False, emp=emp,
                               this_year=today().year)
    if not request.args.get("week"):  # default: the person's month calendar, read only
        y, m = _month_arg()
        prev, nxt = _neighbours(y, m)
        return render_template("timesheet/month.html", mv=ts.month_view(employee_id, y, m), prev=prev,
                               nxt=nxt if date(y, m, 1) < today().replace(day=1) else None, own=False, emp=emp,
                               daily=ts.daily_hours(), today_iso=today().isoformat(), editor=None)
    start = ts.parse_week(request.args.get("week"))
    v = ts.week_view(employee_id, start)
    return render_template("timesheet/view.html", v=v, emp=emp, own=False, start=start, end=start + timedelta(days=6),
                           prev=(start - timedelta(days=7)).isoformat(), nxt=(start + timedelta(days=7)).isoformat())


@bp.route("/report")
@require("leave.view_team", "leave.view_all", "timesheet.manage")
def report():
    month = request.args.get("month") or today().strftime("%Y-%m")
    try:
        y, m = (int(x) for x in month.split("-"))
        month = f"{y:04d}-{m:02d}"
        if not 1 <= m <= 12:
            raise ValueError
    except ValueError:
        month = today().strftime("%Y-%m")
    rows, by_project, by_person, (first, last) = ts.month_report(_scope(), month)
    if request.args.get("format") == "csv":
        return Response(ts.report_csv(rows), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=timesheets-{month}.csv"})
    project_names = [p for p, _ in by_project]
    return render_template("timesheet/report.html", month=month, first=first, by_project=by_project,
                           by_person=by_person, project_names=project_names, total=sum(h for _, h in by_project))


@bp.route("/projects")
@require("timesheet.manage")
def project_list():
    status = request.args.get("status") if request.args.get("status") in ts.PROJECT_STATUSES else ""
    every = ts.project_list()
    counts = {k: sum(1 for p in every if p["status"] == k) for k in ts.PROJECT_STATUSES}
    rows = [p for p in every if not status or p["status"] == status]
    return render_template("timesheet/projects.html", projects=rows, status=status, statuses=ts.PROJECT_STATUSES,
                           counts=counts, total=len(every))


def _project_form_choices():
    return {"departments": query("SELECT id, name FROM departments ORDER BY name"),
            "heads": query("""SELECT e.id, e.full_name, g.name AS designation FROM employees e
                               LEFT JOIN designations g ON g.id = e.designation_id
                              WHERE e.status = 'ACTIVE' ORDER BY e.full_name"""),
            "statuses": ts.PROJECT_STATUSES}


@bp.route("/projects/new", methods=["GET", "POST"], defaults={"project_id": None})
@bp.route("/projects/<int:project_id>", methods=["GET", "POST"])
@require("timesheet.manage")
def project_edit(project_id):
    current = ts.project(project_id) if project_id else None
    if project_id and current is None:
        abort(404)
    error = None
    if request.method == "POST":
        try:
            pid = ts.save_project(request.form, project_id)
        except ValidationError as exc:
            error = str(exc)
        else:
            flash("Project saved." if project_id else "Project added. Staff can now book time on it.", "success")
            return redirect(url_for("timesheet.project_edit", project_id=pid))
    form = request.form if request.method == "POST" else (dict(current) if current else {"status": "ACTIVE"})
    return render_template("timesheet/project_form.html", p=current, form=form, error=error,
                           people=ts.project_people(project_id) if project_id else [], **_project_form_choices())
