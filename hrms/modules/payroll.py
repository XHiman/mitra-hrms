"""Salary slips: HR / Accounts prepare and issue; employees view and print their own."""
from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..core.db import query
from ..core.rbac import require
from ..core.utils import ValidationError, today
from ..services import payroll as pay

bp = Blueprint("payroll", __name__)


def _month_arg():
    month = request.values.get("month") or today().strftime("%Y-%m")
    try:
        return pay.valid_month(month)
    except ValidationError:
        return today().strftime("%Y-%m")


def _months_around(month):
    y, m = (int(x) for x in month.split("-"))
    prev = date(y - (m == 1), 12 if m == 1 else m - 1, 1).strftime("%Y-%m")
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1).strftime("%Y-%m")
    return prev, nxt


# ---------------------------------------------------------------- HR / Accounts
@bp.route("/payroll/")
@require("payroll.manage")
def index():
    month = _month_arg()
    rows = pay.month_overview(month)
    counts = {"structure": sum(1 for r in rows if r["has_structure"]),
              "draft": sum(1 for r in rows if r["slip_status"] == "DRAFT"),
              "issued": sum(1 for r in rows if r["slip_status"] == "ISSUED"),
              "net": sum(r["net"] or 0 for r in rows if r["slip_status"] == "ISSUED")}
    prev, nxt = _months_around(month)
    return render_template("payroll/index.html", rows=rows, month=month, label=pay.month_label(month),
                           counts=counts, prev=prev, nxt=nxt)


@bp.route("/payroll/generate", methods=["POST"])
@require("payroll.manage")
def generate():
    month = _month_arg()
    emp = request.form.get("employee_id", "")
    try:
        created, skipped = pay.generate(g.user, month, {int(emp)} if emp.isdigit() else None)
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        if len(created) == 1 and emp.isdigit():
            return redirect(url_for("payroll.slip_edit", slip_id=created[0]))
        msg = f"{len(created)} draft slip(s) prepared for {pay.month_label(month)}."
        if skipped:
            msg += " Not prepared: " + "; ".join(skipped[:6]) + ("…" if len(skipped) > 6 else "") + "."
        flash(msg, "success" if created else "info")
    return redirect(url_for("payroll.index", month=month))


@bp.route("/payroll/issue", methods=["POST"])
@require("payroll.manage")
def issue():
    month = _month_arg()
    slip_id = request.form.get("slip_id", "")
    if slip_id.isdigit():
        ids = [int(slip_id)]
    else:
        ids = [r["id"] for r in query("SELECT id FROM salary_slips WHERE month = ? AND status = 'DRAFT'", (month,))]
    try:
        issued = pay.issue(g.user, ids)
    except PermissionError as exc:
        flash(str(exc), "error")
    else:
        flash(f"{len(issued)} salary slip(s) issued. Employees have been notified." if issued else
              "No draft slips to issue.", "success" if issued else "info")
    if slip_id.isdigit():
        return redirect(url_for("payroll.slip_edit", slip_id=int(slip_id)))
    return redirect(url_for("payroll.index", month=month))


@bp.route("/payroll/structure/<int:employee_id>", methods=["GET", "POST"])
@require("payroll.manage")
def structure(employee_id):
    emp = query("""SELECT e.*, g.name AS designation, d.name AS department FROM employees e
                   LEFT JOIN designations g ON g.id = e.designation_id LEFT JOIN departments d ON d.id = e.department_id
                   WHERE e.id = ?""", (employee_id,), one=True)
    if emp is None:
        abort(404)
    own = employee_id == g.user["employee_id"]
    error = None
    if request.method == "POST":
        try:
            pay.save_structure(g.user, employee_id, request.form)
        except (ValidationError, PermissionError) as exc:
            error = str(exc)
        else:
            flash("Pay structure saved.", "success")
            return redirect(url_for("payroll.index", month=request.args.get("month") or None))
    s, lines = pay.structure(employee_id)
    if request.method == "POST":
        form = request.form
    else:
        form = {k: (s[k] or "") for k in ("pan", "bank_name", "bank_account", "ifsc")} if s else {}
        for kind, prefix in (("EARNING", "earning"), ("DEDUCTION", "deduction")):
            for i, line in enumerate(l for l in lines if l["kind"] == kind):
                form[f"{prefix}_name_{i}"] = line["name"]
                form[f"{prefix}_amount_{i}"] = pay.fmt_money(line["amount"]).replace(",", "")
    return render_template("payroll/structure.html", emp=emp, form=form, error=error, own=own,
                           rows=range(pay.MAX_LINES), month=request.args.get("month"))


@bp.route("/payroll/slips/<int:slip_id>", methods=["GET", "POST"])
@require("payroll.manage")
def slip_edit(slip_id):
    s, lines, details = pay.slip(slip_id)
    if s is None:
        abort(404)
    error = None
    if request.method == "POST":
        try:
            pay.update_draft(g.user, slip_id, request.form, recalculate=request.form.get("action") == "recalculate")
        except (ValidationError, PermissionError) as exc:
            error = str(exc)
        else:
            flash("Slip updated.", "success")
            return redirect(url_for("payroll.slip_edit", slip_id=slip_id))
        s, lines, details = pay.slip(slip_id)
    form = request.form if error else {}
    if not form:
        form = {"lop_days": f"{s['lop_days']:g}", "remarks": s["remarks"] or ""}
        for kind, prefix in (("EARNING", "earning"), ("DEDUCTION", "deduction")):
            for i, line in enumerate(l for l in lines if l["kind"] == kind):
                form[f"{prefix}_name_{i}"] = line["name"]
                form[f"{prefix}_amount_{i}"] = pay.fmt_money(line["amount"]).replace(",", "")
    return render_template("payroll/slip_edit.html", s=s, lines=lines, details=details, form=form, error=error,
                           rows=range(pay.MAX_LINES), label=pay.month_label(s["month"]),
                           own=s["employee_id"] == g.user["employee_id"])


@bp.route("/payroll/slips/<int:slip_id>/cancel", methods=["POST"])
@require("payroll.manage")
def slip_cancel(slip_id):
    try:
        pay.cancel(g.user, slip_id, request.form.get("reason"))
    except (ValidationError, PermissionError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("payroll.slip_edit", slip_id=slip_id))
    flash("Slip cancelled. You can now prepare a corrected slip for this month.", "success")
    s, _, _ = pay.slip(slip_id)
    return redirect(url_for("payroll.index", month=s["month"]))


@bp.route("/payroll/slips/<int:slip_id>/print")
@require("payroll.manage")
def slip_print(slip_id):
    s, lines, details = pay.slip(slip_id)
    if s is None or s["status"] == "CANCELLED":
        abort(404)
    return _render_slip(s, lines, details)


# ---------------------------------------------------------------- employee self-service
@bp.route("/salary/")
@require("profile.view_own")
def my_slips():
    return render_template("payroll/my_slips.html", slips=pay.slips_for_employee(g.user["employee_id"]),
                           label=pay.month_label)


@bp.route("/salary/<int:slip_id>")
@require("profile.view_own")
def my_slip(slip_id):
    s, lines, details = pay.slip(slip_id)
    if s is None or s["employee_id"] != g.user["employee_id"] or s["status"] != "ISSUED":
        abort(404)  # not 403: do not reveal whether someone else's slip exists
    return _render_slip(s, lines, details)


def _render_slip(s, lines, details):
    if not details:  # draft: show current employee details
        details = {"full_name": s["full_name"], "emp_code": s["emp_code"], "designation": s["designation"],
                   "department": s["department"], "joining_date": s["joining_date"]}
        st, _ = pay.structure(s["employee_id"])
        if st:
            details.update({k: st[k] for k in ("pan", "bank_name", "bank_account", "ifsc")})
    return render_template("payroll/slip.html", s=s, d=details, label=pay.month_label(s["month"]),
                           earnings=[l for l in lines if l["kind"] == "EARNING"],
                           deductions=[l for l in lines if l["kind"] == "DEDUCTION"],
                           words=pay.amount_in_words(s["net"]), paid_days=s["days_in_month"] - s["lop_days"])
