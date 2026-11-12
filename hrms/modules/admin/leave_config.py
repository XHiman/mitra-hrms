"""Leave configuration: leave types, entitlements, rules, holidays."""
import re
from datetime import timedelta

from flask import abort, flash, redirect, render_template, request, url_for

from ...core import audit
from ...core.db import query, transaction
from ...core.rbac import require
from ...services import calendar_events as events
from ...core.utils import (ValidationError, clean_text, employment_types, leave_year_of, opt_float, opt_int,
                           parse_date, today, utcnow_iso)
from . import bp

_FIELDS = ("code", "name", "description", "balance_tracked", "annual_entitlement", "accrual_per_month", "min_days", "max_days",
           "requires_document", "document_after_days", "count_non_working_days", "allow_backdated",
           "min_notice_days", "eligible_employment_types", "workflow_id", "credit_based", "is_paid", "is_active")


def _read(form):
    f = form
    data = {
        "code": clean_text(f.get("code"), "Code", required=True, max_len=10).upper(),
        "name": clean_text(f.get("name"), "Name", required=True, max_len=80),
        "description": clean_text(f.get("description"), "Description", max_len=500) or None,
        "balance_tracked": 1 if f.get("balance_tracked") else 0,
        "annual_entitlement": opt_float(f.get("annual_entitlement"), "Annual entitlement"),
        "accrual_per_month": opt_float(f.get("accrual_per_month"), "Monthly credit"),
        "min_days": opt_float(f.get("min_days"), "Minimum days"),
        "max_days": opt_float(f.get("max_days"), "Maximum days"),
        "requires_document": 1 if f.get("requires_document") else 0,
        "document_after_days": opt_float(f.get("document_after_days"), "Document threshold"),
        "count_non_working_days": 1 if f.get("count_non_working_days") else 0,
        "allow_backdated": 1 if f.get("allow_backdated") else 0,
        "min_notice_days": opt_int(f.get("min_notice_days"), "Minimum notice", 0, 365),
        "credit_based": 1 if f.get("credit_based") else 0,
        "is_paid": 1 if f.get("is_paid") else 0,
        "is_active": 1 if f.get("is_active") else 0,
    }
    if data["credit_based"]:  # balance comes only from approved comp-off claims
        data["balance_tracked"], data["annual_entitlement"], data["accrual_per_month"] = 1, None, None
    if data["accrual_per_month"] == 0:
        data["accrual_per_month"] = None
    if data["accrual_per_month"] is not None and data["annual_entitlement"] is None:
        raise ValidationError("Enter the annual entitlement too: it is the most that monthly credits can add up to.")
    if not re.match(r"^[A-Z0-9_]{1,10}$", data["code"]):
        raise ValidationError("Code must be up to 10 letters, digits or underscores.")
    if data["min_days"] is not None and data["max_days"] is not None and data["min_days"] > data["max_days"]:
        raise ValidationError("Minimum days cannot exceed maximum days.")
    allowed = set(employment_types())
    chosen = [t for t in f.getlist("eligible_employment_types") if t in allowed]
    data["eligible_employment_types"] = ",".join(chosen) or None
    wf = f.get("workflow_id") or ""
    if wf and not (wf.isdigit() and query("SELECT 1 FROM approval_workflows WHERE id = ?", (int(wf),), one=True)):
        raise ValidationError("Select a valid workflow.")
    data["workflow_id"] = int(wf) if wf else None
    return data


@bp.route("/leave-types")
@require("leave_config.manage")
def leave_types():
    rows = query("""SELECT lt.*, w.name AS workflow_name FROM leave_types lt
                    LEFT JOIN approval_workflows w ON w.id = lt.workflow_id ORDER BY lt.is_active DESC, lt.name""")
    holidays = query("SELECT * FROM holidays ORDER BY date")
    return render_template("admin/leave_types.html", rows=rows, holidays=holidays,
                           official_years=sorted(events.bundled_holiday_files()))


@bp.route("/leave-types/<int:type_id>", methods=["GET", "POST"])
@bp.route("/leave-types/new", methods=["GET", "POST"], defaults={"type_id": None})
@require("leave_config.manage")
def leave_type_edit(type_id):
    lt = query("SELECT * FROM leave_types WHERE id = ?", (type_id,), one=True) if type_id else None
    if type_id and lt is None:
        abort(404)
    error = None
    if request.method == "POST":
        try:
            data = _read(request.form)
            with transaction() as conn:
                dup = conn.execute("SELECT 1 FROM leave_types WHERE (code = ? OR name = ?) AND id IS NOT ?",
                                   (data["code"], data["name"], type_id)).fetchone()
                if dup:
                    raise ValidationError("Another leave type already uses that code or name.")
                if data["credit_based"] and data["is_active"] and conn.execute(
                        "SELECT 1 FROM leave_types WHERE credit_based = 1 AND is_active = 1 AND id IS NOT ?",
                        (type_id,)).fetchone():
                    raise ValidationError("Another active leave type is already the comp-off (credit-based) type.")
                if lt is not None and data["credit_based"] != lt["credit_based"] and (
                        conn.execute("SELECT 1 FROM leave_requests WHERE leave_type_id = ?", (type_id,)).fetchone()
                        or conn.execute("SELECT 1 FROM compoff_claims WHERE leave_type_id = ?", (type_id,)).fetchone()):
                    raise ValidationError("The comp-off setting cannot be changed once applications or claims exist "
                                          "for this leave type. Create a new leave type instead.")
                now = utcnow_iso()
                if lt is None:
                    cols = list(_FIELDS)
                    new_id = conn.execute(
                        f"INSERT INTO leave_types ({', '.join(cols)}, created_at, updated_at) "
                        f"VALUES ({', '.join('?' for _ in cols)}, ?, ?)", [data[c] for c in cols] + [now, now]).lastrowid
                    audit.log("LEAVE_TYPE_CREATED", "leave_type", new_id, new=data)
                else:
                    conn.execute(f"UPDATE leave_types SET {', '.join(c + ' = ?' for c in _FIELDS)}, updated_at = ? "
                                 "WHERE id = ?", [data[c] for c in _FIELDS] + [now, type_id])
                    prev = {c: lt[c] for c in _FIELDS}
                    changed = {c: data[c] for c in _FIELDS if prev[c] != data[c]}
                    if "annual_entitlement" in changed or "credit_based" in changed:
                        # propagate to current and future leave years, except per-employee overrides
                        # (a credit-based type has no requests or claims yet here, so it starts at zero)
                        year = int((today() - timedelta(days=366)).strftime("%Y%m%d"))  # current & future cycles
                        conn.execute("""UPDATE leave_balances SET entitlement = ? WHERE leave_type_id = ?
                                        AND leave_year >= ? AND is_override = 0""",
                                     (0 if data["credit_based"] else data["annual_entitlement"], type_id, year))
                    if changed:
                        audit.log("LEAVE_TYPE_UPDATED", "leave_type", type_id,
                                  prev={c: prev[c] for c in changed}, new=changed)
        except ValidationError as exc:
            error = str(exc)
        else:
            flash("Leave type saved.", "success")
            return redirect(url_for("admin.leave_types"))
    if request.method == "POST":
        form, selected = request.form, request.form.getlist("eligible_employment_types")
    else:
        form = dict(lt) if lt else {"balance_tracked": 1, "is_active": 1, "is_paid": 1}
        selected = [t for t in (form.get("eligible_employment_types") or "").split(",") if t]
    return render_template("admin/leave_type_form.html", lt=lt, error=error, form=form, selected_types=selected,
                           employment_types=employment_types(),
                           workflows=query("SELECT * FROM approval_workflows WHERE is_active = 1 ORDER BY name"))


@bp.route("/holidays/load", methods=["POST"])
@require("leave_config.manage")
def holiday_load():
    files = events.bundled_holiday_files()
    year = request.form.get("year", "")
    if not year.isdigit() or int(year) not in files:
        abort(400)
    try:
        added, updated = events.load_holidays(files[int(year)], f"Government of Maharashtra holiday list {year}")
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Government of Maharashtra holidays for {year}: {added} added, {updated} updated.", "success")
    return redirect(url_for("admin.leave_types") + "#holidays")


@bp.route("/holidays", methods=["POST"])
@require("leave_config.manage")
def holiday_add():
    try:
        d = parse_date(request.form.get("date"), "Holiday date")
        name = clean_text(request.form.get("name"), "Holiday name", required=True, max_len=80)
        desc = clean_text(request.form.get("description"), "Occasion", max_len=300) or None
        with transaction() as conn:
            if conn.execute("SELECT 1 FROM holidays WHERE date = ?", (d.isoformat(),)).fetchone():
                raise ValidationError("A holiday already exists on that date.")
            hid = conn.execute("INSERT INTO holidays (date, name, description) VALUES (?, ?, ?)",
                               (d.isoformat(), name, desc)).lastrowid
            audit.log("HOLIDAY_ADDED", "holiday", hid, new={"date": d.isoformat(), "name": name})
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Holiday added. It applies to applications submitted from now on.", "success")
    return redirect(url_for("admin.leave_types") + "#holidays")


@bp.route("/holidays/<int:holiday_id>/delete", methods=["POST"])
@require("leave_config.manage")
def holiday_delete(holiday_id):
    with transaction() as conn:
        row = conn.execute("SELECT * FROM holidays WHERE id = ?", (holiday_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM holidays WHERE id = ?", (holiday_id,))
            audit.log("HOLIDAY_REMOVED", "holiday", holiday_id, prev=dict(row))
    flash("Holiday removed.", "success")
    return redirect(url_for("admin.leave_types") + "#holidays")
