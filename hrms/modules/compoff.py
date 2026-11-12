"""Compensatory off: employee claims, and approval by the officer who assigned the work."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..core.rbac import has_perm, require
from ..core.utils import ValidationError, today
from ..services import compoff as compoff_svc
from ..services import leave as leave_svc
from ..services import workflow

bp = Blueprint("compoff", __name__, url_prefix="/compoff")


@bp.route("/", methods=["GET", "POST"])
@require("leave.apply")
def index():
    error = None
    if request.method == "POST":
        try:
            compoff_svc.submit(g.user, request.form)
        except ValidationError as exc:
            error = str(exc)
        else:
            flash("Your comp-off claim has been sent to the officer who assigned the work.", "success")
            return redirect(url_for("compoff.index"))
    lt = compoff_svc.credit_leave_type()
    cycle = leave_svc.current_cycle(g.user["employee_id"])
    balance = leave_svc.balances_for(g.user["employee_id"], cycle, [lt])[0] if lt else None
    return render_template("compoff/index.html", lt=lt, balance=balance, cycle=cycle, error=error, form=request.form,
                           approvers=compoff_svc.approver_choices(g.user["employee_id"]),
                           claims=compoff_svc.claims_for_employee(g.user["employee_id"]), today=today().isoformat())


@bp.route("/<int:claim_id>/withdraw", methods=["POST"])
@require("leave.apply")
def withdraw(claim_id):
    try:
        compoff_svc.withdraw(g.user, claim_id)
    except PermissionError:
        abort(403)
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Comp-off claim withdrawn.", "success")
    return redirect(url_for("compoff.index"))


@bp.route("/approvals")
@require("compoff.approve")
def approvals():
    scope = None
    if has_perm("leave.view_team") or has_perm("leave.view_all"):
        scope = workflow.visible_employee_ids(g.user)
        if scope is not None:
            scope = scope - {g.user["employee_id"]}
        in_scope = compoff_svc.claims_in_scope(scope)
    else:
        in_scope = []
    return render_template("compoff/approvals.html", pending=compoff_svc.actionable_claims(g.user),
                           decided=compoff_svc.decided_by(g.user), in_scope=in_scope, scope_all=scope is None)


@bp.route("/<int:claim_id>/decide", methods=["POST"])
@require("compoff.approve")
def decide(claim_id):
    decision = request.form.get("decision")
    remarks = request.form.get("remarks")
    try:
        if decision == "approve":
            compoff_svc.approve(g.user, claim_id, remarks)
            flash("Comp-off approved and credited to the employee's balance.", "success")
        elif decision == "reject":
            compoff_svc.reject(g.user, claim_id, remarks)
            flash("Comp-off claim rejected.", "success")
        else:
            abort(400)
    except PermissionError:
        abort(403)
    except ValidationError as exc:
        flash(str(exc), "error")
    if request.form.get("return_to") == "pending":
        return redirect(url_for("dashboards.pending"))
    if request.form.get("return_to") == "dashboard":
        return redirect(url_for("dashboards.supervisory") + "#compoff")
    return redirect(url_for("compoff.approvals"))
