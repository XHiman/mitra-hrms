"""Employee self-service leave: apply, history, detail, withdraw/cancel, documents; scoped request lists."""
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_file, url_for)

from ..core import audit
from ..core.db import query
from ..core.rbac import has_perm, require
from ..core.utils import ValidationError, today
from ..services import leave as leave_svc
from ..services import workflow

bp = Blueprint("leave", __name__, url_prefix="/leave")


@bp.route("/apply", methods=["GET", "POST"])
@require("leave.apply")
def apply():
    emp = leave_svc.employee(g.user["employee_id"])
    types = leave_svc.applicable_leave_types(emp)
    error = None
    if request.method == "POST":
        try:
            req_id = leave_svc.apply(g.user, request.form, request.files.get("document"))
        except ValidationError as exc:
            error = str(exc)
        else:
            flash("Your leave application has been submitted for approval.", "success")
            return redirect(url_for("leave.detail", request_id=req_id))
    return render_template("leave/apply.html", types=types, error=error, form=request.form,
                           balances={b["leave_type"]["id"]: b for b in leave_svc.balances_for(emp["id"], None, types)},
                           today=today().isoformat(), contract_end=emp["contract_end_date"])


@bp.route("/preview")
@require("leave.apply")
def preview():
    try:
        days = leave_svc.preview(None, request.args.get("leave_type_id", "0") or 0,
                                 request.args.get("from_date"), request.args.get("to_date"),
                                 request.args.get("half_day") or None)
    except ValidationError as exc:
        return jsonify(ok=False, error=str(exc))
    return jsonify(ok=True, days=days)


@bp.route("/my")
@require("leave.view_own")
def my_requests():
    status = request.args.get("status", "")
    rows = leave_svc.requests_for_employee(g.user["employee_id"])
    if status in ("PENDING", "APPROVED", "REJECTED", "CANCELLED"):
        rows = [r for r in rows if r["status"] == status]
    return render_template("leave/my_requests.html", rows=rows, status=status)


@bp.route("/<int:request_id>")
@require("leave.view_own", "leave.view_team", "leave.view_all", "leave.approve")
def detail(request_id):
    req = leave_svc.request_detail(request_id)
    if req is None:
        abort(404)
    if not workflow.can_view_request(g.user, req):
        abort(403)
    own = req["employee_id"] == g.user["employee_id"]
    show_audit = has_perm("audit.view_all") or (own and has_perm("audit.view_own")) or \
        (not own and has_perm("audit.view_scope"))
    history = query("SELECT * FROM audit_logs WHERE entity = 'leave_request' AND entity_id = ? ORDER BY id",
                    (str(request_id),)) if show_audit else []
    return render_template("leave/detail.html", req=req, trail=leave_svc.approval_trail(request_id),
                           can_act=workflow.can_act(g.user, req), can_cancel=leave_svc.can_cancel(g.user, req),
                           history=history, own=own)


@bp.route("/<int:request_id>/cancel", methods=["POST"])
@require("leave.apply")
def cancel(request_id):
    try:
        leave_svc.cancel(g.user, request_id, request.form.get("reason"))
    except PermissionError:
        abort(403)
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("The leave request has been cancelled.", "success")
    return redirect(url_for("leave.detail", request_id=request_id))


@bp.route("/<int:request_id>/document")
@require("leave.view_own", "leave.view_team", "leave.view_all", "leave.approve")
def document(request_id):
    req = leave_svc.request_detail(request_id)
    if req is None or not req["attachment_path"]:
        abort(404)
    if not workflow.can_view_request(g.user, req):
        abort(403)
    path = Path(current_app.config["UPLOAD_DIR"]) / Path(req["attachment_path"]).name
    if not path.is_file():
        abort(404)
    audit.log("DOCUMENT_VIEWED", "leave_request", request_id, remarks=req["attachment_name"])
    return send_file(path, mimetype=req["attachment_mime"], as_attachment=True,
                     download_name=req["attachment_name"])


@bp.route("/records")
@require("leave.view_team", "leave.view_all")
def records():
    """All leave requests within the user's authorised scope, filterable."""
    scope = workflow.visible_employee_ids(g.user)
    if scope is not None:
        scope = scope - {g.user["employee_id"]}
    where, params = ["1=1"], []
    status = request.args.get("status", "")
    if status in ("PENDING", "APPROVED", "REJECTED", "CANCELLED"):
        where.append("r.status = ?")
        params.append(status)
    type_id = request.args.get("type", "")
    if type_id.isdigit():
        where.append("r.leave_type_id = ?")
        params.append(int(type_id))
    dept_id = request.args.get("dept", "")
    if dept_id.isdigit():
        where.append("e.department_id = ?")
        params.append(int(dept_id))
    q = (request.args.get("q") or "").strip()[:60]
    if q:
        where.append("(e.full_name LIKE ? OR e.emp_code LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    rows = leave_svc.requests_in_scope(scope, " AND ".join(where), params, limit=500)
    return render_template("leave/records.html", rows=rows, status=status, type_id=type_id, dept_id=dept_id, q=q,
                           types=query("SELECT id, name FROM leave_types ORDER BY name"),
                           depts=query("SELECT id, name FROM departments ORDER BY name"))
