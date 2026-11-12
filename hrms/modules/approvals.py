"""Supervisory approvals: the pending queue and approve/reject actions."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..core.rbac import require
from ..core.utils import ValidationError
from ..services import leave as leave_svc
from ..services import workflow

bp = Blueprint("approvals", __name__, url_prefix="/approvals")


@bp.route("/")
@require("leave.approve")
def queue():
    rows = [leave_svc.request_detail(r["id"]) for r in workflow.actionable_requests(g.user)]
    return render_template("approvals/queue.html", rows=rows)


@bp.route("/<int:request_id>/decide", methods=["POST"])
@require("leave.approve")
def decide(request_id):
    decision = request.form.get("decision")
    stage = request.form.get("stage", "")
    remarks = request.form.get("remarks")
    try:
        if decision == "approve":
            outcome = leave_svc.approve(g.user, request_id, stage, remarks)
            flash("Leave approved." if outcome == "approved" else "Approved at your stage and forwarded to the next authority.",
                  "success")
        elif decision == "reject":
            leave_svc.reject(g.user, request_id, stage, remarks)
            flash("Leave request rejected.", "success")
        else:
            abort(400)
    except PermissionError:
        abort(403)
    except ValidationError as exc:
        flash(str(exc), "error")  # stay on the page the decision was made from
    target = request.form.get("return_to")
    if target == "queue":
        return redirect(url_for("approvals.queue"))
    if target == "pending":
        return redirect(url_for("dashboards.pending"))
    if target == "dashboard":
        return redirect(url_for("dashboards.supervisory"))
    return redirect(url_for("leave.detail", request_id=request_id))
