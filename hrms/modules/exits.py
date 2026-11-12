"""Resignation and exit: employee self-service, reporting officer remarks, HR acceptance, clearance and relieving."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..core.rbac import require
from ..core.utils import ValidationError, fmt_date, today
from ..services import exits as exit_svc

bp = Blueprint("exits", __name__, url_prefix="/exit")


@bp.route("/", methods=["GET", "POST"])
@require("profile.view_own")
def mine():
    error = None
    if request.method == "POST":
        try:
            rid = exit_svc.submit(g.user, request.form)
        except ValidationError as exc:
            error = str(exc)
        else:
            flash("Your resignation has been submitted.", "success")
            return redirect(url_for("exits.detail", rid=rid))
    current = exit_svc.open_resignation(g.user["employee_id"])
    return render_template("exits/mine.html", error=error, form=request.form, current=current,
                           history=exit_svc.for_employee(g.user["employee_id"]), reasons=exit_svc.REASONS,
                           notice=exit_svc.notice_days(), officer=exit_svc.officer_for(g.user["employee_id"]),
                           today=today().isoformat(), labels=exit_svc.STATUS_LABELS)


@bp.route("/manage")
@require("exit.manage", "leave.approve")
def manage():
    return render_template("exits/manage.html", pending=exit_svc.actionable(g.user), rows=exit_svc.in_scope(g.user),
                           labels=exit_svc.STATUS_LABELS)


@bp.route("/<int:rid>")
@require("profile.view_own")
def detail(rid):
    res = exit_svc.detail(rid)
    if res is None:
        abort(404)
    if not exit_svc.can_view(g.user, res):
        abort(403)
    acting = exit_svc.can_act(g.user, res)
    return render_template("exits/detail.html", r=res, acting=acting, own=res["employee_id"] == g.user["employee_id"],
                           items=exit_svc.clearance(rid), notice_note=exit_svc.notice_note(res),
                           blockers=exit_svc.relieve_blockers(res) if res["status"] == "ACCEPTED" else [],
                           reportees=exit_svc.reportees(res["employee_id"]) if acting else [],
                           labels=exit_svc.STATUS_LABELS, today=today().isoformat())


@bp.route("/<int:rid>/act", methods=["POST"])
@require("profile.view_own")
def act(rid):
    action, remarks = request.form.get("action"), request.form.get("remarks")
    try:
        if action == "withdraw":
            exit_svc.withdraw(g.user, rid, remarks)
            flash("Your resignation has been withdrawn.", "success")
        elif action == "forward":
            exit_svc.forward(g.user, rid, remarks)
            flash("Resignation forwarded to HR.", "success")
        elif action == "accept":
            exit_svc.accept(g.user, rid, request.form.get("accepted_lwd"), remarks)
            flash("Resignation accepted. Complete the exit clearance before relieving.", "success")
        elif action == "revoke":
            exit_svc.revoke(g.user, rid, remarks)
            flash("Acceptance revoked; the employee continues in service.", "success")
        elif action == "relieve":
            exit_svc.relieve(g.user, rid, remarks)
            res = exit_svc.detail(rid)
            flash(f"{res['employee_name']} has been relieved from {fmt_date(res['accepted_lwd'])}; "
                  "their login is now closed.", "success")
        else:
            abort(400)
    except PermissionError:
        abort(403)
    except ValidationError as exc:
        flash(str(exc), "error")
    return redirect(url_for("exits.detail", rid=rid))


@bp.route("/<int:rid>/clearance/<int:seq>", methods=["POST"])
@require("profile.view_own")
def clearance(rid, seq):
    try:
        exit_svc.set_clearance(g.user, rid, seq, request.form.get("done") == "1", request.form.get("note"))
    except PermissionError:
        abort(403)
    except ValidationError as exc:
        flash(str(exc), "error")
    return redirect(url_for("exits.detail", rid=rid) + "#clearance")
