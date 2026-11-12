"""Analytics page: charts and comparisons for a team, a department or the whole office."""
from flask import Blueprint, abort, g, render_template, request

from ..core.rbac import has_perm, require
from ..core.utils import STAFF_GROUPS, staff_group, staff_group_arg
from ..services import analytics as an
from ..services import workflow

bp = Blueprint("analytics", __name__, url_prefix="/analytics")


@bp.route("/")
@require("analytics.view_team", "analytics.view_office")
def index():
    options = an.scope_options(g.user)
    scope = request.args.get("scope") or options[0][0]
    emps = an.employees_in(g.user, scope)
    if emps is None:
        abort(403)
    group = staff_group_arg(request.args.get("group"))
    if group:
        emps = [e for e in emps if staff_group(e["designation"]) == group]
    period = request.args.get("period") if request.args.get("period") in an.PERIODS else "this"
    view = request.args.get("view") if request.args.get("view") in an.VIEWS else "people"
    start, end = an.period_range(period)
    data = an.compute(emps, start, end)
    project = None
    if view == "projects" and request.args.get("project"):
        project = next((p for p in data["project_rows"] if str(p["id"]) == request.args["project"]), None)
    # a person's calendar opens only where the timesheet pages already allow it
    if has_perm("timesheet.manage") or has_perm("leave.view_all"):
        can_open = None
    else:
        can_open = workflow.visible_employee_ids(g.user) - {g.user["employee_id"]}
    return render_template("analytics/index.html", d=data, options=options, scope=scope,
                           scope_label=dict(options)[scope], period=period, periods=an.PERIODS, view=view,
                           views=an.VIEWS, start=start, end=end, project=project, can_open=can_open,
                           people_by_id={p["emp"]["id"]: p for p in data["people"]}, scale=an.scale,
                           group=group, groups=STAFF_GROUPS)
