"""Leave management domain logic: day counting, balances, request lifecycle."""
import uuid
from datetime import date, timedelta
from pathlib import Path

from flask import current_app, url_for

from ..core import audit
from ..core.db import query, transaction
from ..core.utils import (ValidationError, clean_text, employment_types, fmt_date, fmt_days,
                          get_int_setting, parse_date, today, utcnow_iso,
                          weekend_days)
from . import cycles, workflow
from .notifications import notify

ALLOWED_UPLOADS = {
    # extension: (mime, magic-byte prefixes)
    "pdf": ("application/pdf", (b"%PDF-",)),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
}
MAX_SPAN_DAYS = 366


# ---------------------------------------------------------------- reference data
def leave_type(type_id, active_only=False):
    sql = "SELECT * FROM leave_types WHERE id = ?" + (" AND is_active = 1" if active_only else "")
    return query(sql, (type_id,), one=True)


def applicable_leave_types(employee):
    out = []
    for lt in query("SELECT * FROM leave_types WHERE is_active = 1 ORDER BY name"):
        eligible = [t.strip() for t in (lt["eligible_employment_types"] or "").split(",") if t.strip()]
        if eligible and (employee["employment_type"] or "") not in eligible:
            continue
        out.append(lt)
    return out


def employee(employee_id):
    return query(
        """SELECT e.*, d.name AS department, g.name AS designation, ro.full_name AS reporting_officer,
                  u.username, u.id AS user_id, r.name AS role_name, r.code AS role_code
             FROM employees e
             LEFT JOIN departments d ON d.id = e.department_id
             LEFT JOIN designations g ON g.id = e.designation_id
             LEFT JOIN employees ro ON ro.id = e.reporting_officer_id
             LEFT JOIN users u ON u.employee_id = e.id
             LEFT JOIN roles r ON r.id = u.role_id
            WHERE e.id = ?""", (employee_id,), one=True)


# ---------------------------------------------------------------- day counting
def holiday_dates(start, end):
    return {r["date"] for r in query("SELECT date FROM holidays WHERE date BETWEEN ? AND ?",
                                     (start.isoformat(), end.isoformat()))}


def count_days(lt, start, end):
    if end < start:
        return 0
    if lt["count_non_working_days"]:
        return (end - start).days + 1
    offs = weekend_days()
    hols = holiday_dates(start, end)
    n, d = 0, start
    while d <= end:
        if d.weekday() not in offs and d.isoformat() not in hols:
            n += 1
        d += timedelta(days=1)
    return n


# ---------------------------------------------------------------- balances
def default_entitlement(lt):
    """Credit-based types (comp-off) start at zero and grow with approved claims."""
    return 0 if lt["credit_based"] else lt["annual_entitlement"]


def ensure_balance(conn, employee_id, lt, year):
    row = conn.execute("SELECT * FROM leave_balances WHERE employee_id = ? AND leave_type_id = ? AND leave_year = ?",
                       (employee_id, lt["id"], year)).fetchone()
    if row is None:
        conn.execute("INSERT INTO leave_balances (employee_id, leave_type_id, leave_year, entitlement) VALUES (?, ?, ?, ?)",
                     (employee_id, lt["id"], year, default_entitlement(lt)))
        row = conn.execute("SELECT * FROM leave_balances WHERE employee_id = ? AND leave_type_id = ? AND leave_year = ?",
                           (employee_id, lt["id"], year)).fetchone()
    return row


def credited(lt, entitlement, is_override, cycle, joining_date=None, as_of=None):
    """Entitlement credited so far in a leave cycle, up to the annual entitlement.

    Contract cycles: one monthly credit for each completed month of the cycle (a month is complete on
    the day before the same date next month). Calendar cycles: a credit on the 1st of each month, from
    the joining month for mid-year joiners. An individually set entitlement (override) is taken as-is."""
    if entitlement is None or is_override or not lt["accrual_per_month"] or lt["credit_based"]:
        return entitlement
    as_of = min(as_of or today(), cycle.end)
    if cycle.kind == "CONTRACT":
        months = 0
        while months < 12 and cycles.add_months(cycle.start, months + 1) - timedelta(days=1) <= as_of:
            months += 1
    else:
        start = cycle.start
        if joining_date:
            joined = date.fromisoformat(joining_date)
            start = max(start, date(joined.year, joined.month, 1))
        if as_of < start:
            return 0
        months = (as_of.year - start.year) * 12 + as_of.month - start.month + 1
    return min(entitlement, months * lt["accrual_per_month"])


def _joining_date(employee_id):
    row = query("SELECT joining_date FROM employees WHERE id = ?", (employee_id,), one=True)
    return row["joining_date"] if row else None


def current_cycle(employee_id, on_date=None):
    return cycles.cycle_for(employee_id, on_date or today())


def available(balance_row, lt, employee_id):
    cycle = cycles.cycle_by_key(employee_id, balance_row["leave_year"])
    ent = credited(lt, balance_row["entitlement"], balance_row["is_override"], cycle, _joining_date(employee_id))
    if ent is None:
        return None
    return ent - balance_row["used"] - balance_row["pending"]


def balances_for(employee_id, cycle=None, types=None):
    """One row per active leave type (whether or not a balance row exists yet) for a leave cycle
    (default: the employee's current cycle)."""
    if cycle is None:
        cycle = current_cycle(employee_id)
    elif isinstance(cycle, int):
        cycle = cycles.cycle_by_key(employee_id, cycle)
    rows = []
    types = types if types is not None else query("SELECT * FROM leave_types WHERE is_active = 1 ORDER BY name")
    joining = _joining_date(employee_id)
    for lt in types:
        b = query("SELECT * FROM leave_balances WHERE employee_id = ? AND leave_type_id = ? AND leave_year = ?",
                  (employee_id, lt["id"], cycle.key), one=True)
        annual = b["entitlement"] if b else default_entitlement(lt)
        is_override = bool(b and b["is_override"])
        entitlement = credited(lt, annual, is_override, cycle, joining)
        used = b["used"] if b else 0
        pending = b["pending"] if b else 0
        rows.append({
            "leave_type": lt, "tracked": bool(lt["balance_tracked"]),
            "entitlement": entitlement, "annual": annual, "used": used, "pending": pending,
            "available": None if entitlement is None else entitlement - used - pending,
            "is_override": is_override, "credit_based": bool(lt["credit_based"]),
        })
    return rows


def rebuild_balances(employee_ids=None, remarks="Balances recalculated"):
    """Re-assign requests and comp-off claims to the correct leave cycle and recompute balances.
    Run after joining or renewal dates change. Individually set entitlements (overrides) are kept."""
    where, params = "", []
    if employee_ids is not None:
        ids = sorted(employee_ids) or [-1]
        where, params = f" WHERE employee_id IN ({','.join('?' for _ in ids)})", ids
    with transaction() as conn:
        marks_cache = {}

        def key(emp_id, iso):
            if emp_id not in marks_cache:
                marks_cache[emp_id] = cycles.milestones(emp_id)
            return cycles.cycle_for(emp_id, date.fromisoformat(iso), marks_cache[emp_id]).key

        for r in conn.execute(f"SELECT id, employee_id, from_date FROM leave_requests{where}", params).fetchall():
            conn.execute("UPDATE leave_requests SET leave_year = ? WHERE id = ?", (key(r["employee_id"], r["from_date"]), r["id"]))
        for r in conn.execute(f"SELECT id, employee_id, worked_date FROM compoff_claims{where}", params).fetchall():
            conn.execute("UPDATE compoff_claims SET leave_year = ? WHERE id = ?",
                         (key(r["employee_id"], r["worked_date"]), r["id"]))
        overrides = conn.execute(f"SELECT * FROM leave_balances{where}{' AND' if where else ' WHERE'} is_override = 1",
                                 params).fetchall()
        conn.execute(f"DELETE FROM leave_balances{where}", params)
        types = {t["id"]: t for t in conn.execute("SELECT * FROM leave_types").fetchall()}
        totals = {}
        for r in conn.execute(f"""SELECT employee_id, leave_type_id, leave_year, status, SUM(days) AS days
                                    FROM leave_requests{where} GROUP BY employee_id, leave_type_id, leave_year, status""",
                              params).fetchall():
            t = totals.setdefault((r["employee_id"], r["leave_type_id"], r["leave_year"]), {"used": 0, "pending": 0, "credit": 0})
            if r["status"] == "APPROVED":
                t["used"] += r["days"]
            elif r["status"] == "PENDING":
                t["pending"] += r["days"]
        for r in conn.execute(f"""SELECT employee_id, leave_type_id, leave_year, SUM(days) AS days FROM compoff_claims
                                    {where}{' AND' if where else ' WHERE'} status = 'APPROVED'
                                    GROUP BY employee_id, leave_type_id, leave_year""", params).fetchall():
            totals.setdefault((r["employee_id"], r["leave_type_id"], r["leave_year"]),
                              {"used": 0, "pending": 0, "credit": 0})["credit"] += r["days"]
        for o in overrides:  # keep individually set entitlements on the cycle they now fall in
            new_key = key(o["employee_id"], cycles.key_to_date(o["leave_year"]).isoformat())
            totals.setdefault((o["employee_id"], o["leave_type_id"], new_key), {"used": 0, "pending": 0, "credit": 0})["override"] = o["entitlement"]
        for (emp_id, type_id, k), t in totals.items():
            lt = types[type_id]
            ent = t.get("override", t["credit"] if lt["credit_based"] else default_entitlement(lt))
            conn.execute("""INSERT INTO leave_balances (employee_id, leave_type_id, leave_year, entitlement, is_override,
                                                        used, pending) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (emp_id, type_id, k, ent, 1 if "override" in t else 0, t["used"], t["pending"]))
        audit.log("BALANCES_RECALCULATED", "system", None,
                  new={"employees": "all" if employee_ids is None else len(employee_ids), "balances": len(totals)},
                  remarks=remarks)
    return len(totals)


# ---------------------------------------------------------------- uploads
def save_upload(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    name = file_storage.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_UPLOADS:
        raise ValidationError("Supporting document must be a PDF, PNG or JPEG file.")
    data = file_storage.read(current_app.config["MAX_UPLOAD_BYTES"] + 1)
    if len(data) > current_app.config["MAX_UPLOAD_BYTES"]:
        raise ValidationError("Supporting document is too large (max 5 MB).")
    if not data:
        raise ValidationError("Supporting document is empty.")
    mime, magics = ALLOWED_UPLOADS[ext]
    if not any(data.startswith(m) for m in magics):
        raise ValidationError("Supporting document content does not match its file type.")
    stored = f"{uuid.uuid4().hex}.{ext}"
    path = Path(current_app.config["UPLOAD_DIR"]) / stored
    path.write_bytes(data)
    safe_name = "".join(c for c in Path(name).name if c.isalnum() or c in "._- ")[:120] or f"document.{ext}"
    return stored, safe_name, mime


def _remove_upload(stored):
    try:
        (Path(current_app.config["UPLOAD_DIR"]) / stored).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- helpers
def _state(req):
    keys = ("status", "current_stage", "leave_type_id", "from_date", "to_date", "days", "decided_by",
            "decision_remarks")
    return {k: req[k] for k in keys if k in req.keys()}


def _period(req):
    days = f"{fmt_days(req['days'])} day{'s' if req['days'] != 1 else ''}"
    half = req["half_day"] if "half_day" in req.keys() else None
    if half:
        return f"{fmt_date(req['from_date'])} ({HALF_DAYS.get(half, 'half day')}, 0.5 day)"
    if req["from_date"] == req["to_date"]:
        return f"{fmt_date(req['from_date'])} ({days})"
    return f"{fmt_date(req['from_date'])} to {fmt_date(req['to_date'])} ({days})"


def _yours(req, lt_name):
    return f"Your {lt_name} application for {_period(req)}"


def _theirs(req, lt_name, emp_name):
    return f"{emp_name} has applied for {lt_name}, {_period(req)}."


def _request_context(conn, request_id):
    return conn.execute(
        """SELECT r.*, lt.name AS leave_type_name, e.full_name AS employee_name, u.id AS applicant_user_id
             FROM leave_requests r JOIN leave_types lt ON lt.id = r.leave_type_id
             JOIN employees e ON e.id = r.employee_id LEFT JOIN users u ON u.employee_id = e.id
            WHERE r.id = ?""", (request_id,)).fetchone()


def _link(request_id):
    return url_for("leave.detail", request_id=request_id)


# ---------------------------------------------------------------- apply
HALF_DAYS = {"FIRST": "first half", "SECOND": "second half"}


def _half_day_days(lt, start, end, half):
    """A half-day application covers one working day and counts as 0.5."""
    if start != end:
        raise ValidationError("A half day must be a single date: set From date and To date to the same day.")
    if count_days(lt, start, end) != 1:
        raise ValidationError("A half day can only be taken on a working day.")
    return 0.5


def preview(employee_row, type_id, from_s, to_s, half=None):
    lt = leave_type(type_id, active_only=True)
    if lt is None:
        raise ValidationError("Select a valid leave type.")
    start, end = parse_date(from_s, "From date"), parse_date(to_s, "To date")
    if end < start:
        raise ValidationError("To date cannot be before From date.")
    if half in HALF_DAYS:
        return _half_day_days(lt, start, end, half)
    return count_days(lt, start, end)


def apply(user, form, upload):
    emp = employee(user["employee_id"])
    if emp is None or emp["status"] != "ACTIVE":
        raise ValidationError("Your employee record is not active.")

    lt = leave_type(form.get("leave_type_id", "0") or 0, active_only=True)
    if lt is None:
        raise ValidationError("Select a valid leave type.")
    eligible = [t.strip() for t in (lt["eligible_employment_types"] or "").split(",") if t.strip()]
    if eligible and (emp["employment_type"] or "") not in eligible:
        raise ValidationError(f"{lt['name']} is not applicable to your employment type.")

    start = parse_date(form.get("from_date"), "From date")
    end = parse_date(form.get("to_date"), "To date")
    reason = clean_text(form.get("reason"), "Reason", required=True, max_len=1000)
    if end < start:
        raise ValidationError("To date cannot be before From date.")
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise ValidationError("A single application cannot span more than a year.")
    if emp["joining_date"] and start.isoformat() < emp["joining_date"]:
        raise ValidationError(f"The leave starts before your date of joining ({fmt_date(emp['joining_date'])}).")
    if emp["contract_end_date"] and end.isoformat() > emp["contract_end_date"]:
        raise ValidationError(f"Your contract ends on {fmt_date(emp['contract_end_date'])}. "
                              "Leave cannot be taken after that date.")
    half = form.get("half_day") or None
    if half is not None and half not in HALF_DAYS:
        raise ValidationError("Choose the first half or the second half of the day.")
    cycle = cycles.cycle_for(emp["id"], start)
    if not cycle.contains(end):
        raise ValidationError(f"The leave crosses into your next leave cycle, which starts on "
                              f"{fmt_date(cycle.end + timedelta(days=1))}. Please submit separate applications.")
    year = cycle.key
    t = today()
    if start < t and not lt["allow_backdated"]:
        raise ValidationError(f"{lt['name']} cannot be applied for past dates.")
    if lt["min_notice_days"] and (start - t).days < lt["min_notice_days"] and start >= t:
        raise ValidationError(f"{lt['name']} must be applied at least {lt['min_notice_days']} day(s) in advance.")

    days = _half_day_days(lt, start, end, half) if half else count_days(lt, start, end)
    if days <= 0:
        raise ValidationError("The selected period has no working days.")
    if lt["min_days"] is not None and days < lt["min_days"]:
        raise ValidationError(f"Minimum {fmt_days(lt['min_days'])} day(s) per application for {lt['name']}.")
    if lt["max_days"] is not None and days > lt["max_days"]:
        raise ValidationError(f"Maximum {fmt_days(lt['max_days'])} day(s) per application for {lt['name']}.")

    doc_needed = lt["requires_document"] or (lt["document_after_days"] is not None and days > lt["document_after_days"])
    has_file = bool(upload and upload.filename)
    if doc_needed and not has_file:
        raise ValidationError(f"A supporting document is mandatory for this {lt['name']} application.")

    wf = workflow.workflow_for_leave_type(lt)
    if wf is None:
        raise ValidationError("No approval workflow is configured. Please contact the administrator.")

    stored = save_upload(upload) if has_file else None
    try:
        with transaction() as conn:
            for other in conn.execute(
                    """SELECT id, from_date, to_date, half_day FROM leave_requests WHERE employee_id = ?
                         AND status IN ('PENDING', 'APPROVED') AND from_date <= ? AND to_date >= ?""",
                    (emp["id"], end.isoformat(), start.isoformat())).fetchall():
                # the other half of a day already taken as a half day is still free
                if half and other["half_day"] and other["half_day"] != half:
                    continue
                raise ValidationError(f"These dates overlap with your existing request #{other['id']}.")

            if lt["balance_tracked"]:
                bal = ensure_balance(conn, emp["id"], lt, year)
                if bal["entitlement"] is None:
                    raise ValidationError(f"Entitlement for {lt['name']} has not been configured yet. "
                                          "Please contact HR / the administrator.")
                avail = available(bal, lt, emp["id"])
                if avail < days:
                    raise ValidationError(f"Insufficient {lt['name']} balance: {fmt_days(avail)} "
                                          f"day(s) available, {fmt_days(days)} requested.")
                conn.execute("UPDATE leave_balances SET pending = pending + ? WHERE id = ?", (days, bal["id"]))

            cur = conn.execute(
                """INSERT INTO leave_requests (employee_id, leave_type_id, from_date, to_date, days, half_day, leave_year,
                                               reason, attachment_path, attachment_name, attachment_mime, status,
                                               workflow_id, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)""",
                (emp["id"], lt["id"], start.isoformat(), end.isoformat(), days, half, year, reason,
                 stored[0] if stored else None, stored[1] if stored else None, stored[2] if stored else None,
                 wf["id"], utcnow_iso()))
            req_id = cur.lastrowid
            first = workflow.snapshot_stages(conn, req_id, wf["id"], days)
            conn.execute("UPDATE leave_requests SET current_stage = ? WHERE id = ?", (first, req_id))

            req = _request_context(conn, req_id)
            audit.log("LEAVE_SUBMITTED", "leave_request", req_id, prev=None, new=_state(req),
                      remarks=reason)
            notify([user["id"]], "Leave application submitted",
                   f"{_yours(req, lt['name'])} has been submitted and is pending approval.", _link(req_id))
            stage = workflow.current_stage(req)
            approvers = workflow.eligible_approvers(stage, emp["id"])
            notify(approvers, "New leave request requires your action",
                   f"{_theirs(req, lt['name'], emp['full_name'])} Please review it ({stage['stage_name']}).",
                   _link(req_id))
    except Exception:
        if stored:
            _remove_upload(stored[0])
        raise
    return req_id


# ---------------------------------------------------------------- decisions
def _load_for_action(conn, user, request_id, stage_seq):
    req = _request_context(conn, request_id)
    if req is None:
        raise ValidationError("Leave request not found.")
    if req["status"] != "PENDING" or str(req["current_stage"]) != str(stage_seq):
        raise ValidationError("This request has already been acted upon. Please refresh.")
    if not workflow.can_act(user, req):
        raise PermissionError("You are not authorised to act on this request at its current stage.")
    return req


def approve(user, request_id, stage_seq, remarks):
    remarks = clean_text(remarks, "Remarks", max_len=1000) or None
    with transaction() as conn:
        req = _load_for_action(conn, user, request_id, stage_seq)
        now = utcnow_iso()
        conn.execute("UPDATE request_approvals SET status = 'APPROVED', acted_by = ?, acted_at = ?, remarks = ? "
                     "WHERE request_id = ? AND seq = ?", (user["id"], now, remarks, request_id, req["current_stage"]))
        stage_name = conn.execute("SELECT stage_name FROM request_approvals WHERE request_id = ? AND seq = ?",
                                  (request_id, req["current_stage"])).fetchone()["stage_name"]
        nxt = conn.execute("SELECT seq, stage_name FROM request_approvals WHERE request_id = ? AND status = 'WAITING' "
                           "ORDER BY seq LIMIT 1", (request_id,)).fetchone()
        lt_name = req["leave_type_name"]
        if nxt:
            conn.execute("UPDATE request_approvals SET status = 'PENDING' WHERE request_id = ? AND seq = ?",
                         (request_id, nxt["seq"]))
            conn.execute("UPDATE leave_requests SET current_stage = ? WHERE id = ?", (nxt["seq"], request_id))
            new = _request_context(conn, request_id)
            audit.log("LEAVE_STAGE_APPROVED", "leave_request", request_id, prev=_state(req), new=_state(new),
                      remarks=f"[{stage_name}] {remarks or ''}".strip())
            notify([req["applicant_user_id"]], "Leave request forwarded",
                   f"{_yours(req, lt_name)} was approved by {user['full_name']} ({stage_name}) and forwarded "
                   f"for {nxt['stage_name']}.", _link(request_id))
            stage = workflow.current_stage(new)
            notify(workflow.eligible_approvers(stage, req["employee_id"]), "New leave request requires your action",
                   f"{_theirs(req, lt_name, req['employee_name'])} It was approved by {user['full_name']} "
                   f"({stage_name}) and now needs your review ({nxt['stage_name']}).", _link(request_id))
            return "forwarded"

        conn.execute("""UPDATE leave_requests SET status = 'APPROVED', current_stage = NULL, decided_at = ?,
                        decided_by = ?, decision_remarks = ? WHERE id = ?""", (now, user["id"], remarks, request_id))
        lt = leave_type(req["leave_type_id"])
        if lt["balance_tracked"]:
            bal = ensure_balance(conn, req["employee_id"], lt, req["leave_year"])
            conn.execute("UPDATE leave_balances SET pending = MAX(pending - ?, 0), used = used + ? WHERE id = ?",
                         (req["days"], req["days"], bal["id"]))
        new = _request_context(conn, request_id)
        audit.log("LEAVE_APPROVED", "leave_request", request_id, prev=_state(req), new=_state(new),
                  remarks=f"[{stage_name}] {remarks or ''}".strip())
        notify([req["applicant_user_id"]], "Leave approved",
               f"{_yours(req, lt_name)} has been approved by {user['full_name']}."
               + (f" Remarks: {remarks}" if remarks else ""),
               _link(request_id))
        return "approved"


def reject(user, request_id, stage_seq, remarks):
    remarks = clean_text(remarks, "Remarks", required=True, max_len=1000)
    with transaction() as conn:
        req = _load_for_action(conn, user, request_id, stage_seq)
        now = utcnow_iso()
        conn.execute("UPDATE request_approvals SET status = 'REJECTED', acted_by = ?, acted_at = ?, remarks = ? "
                     "WHERE request_id = ? AND seq = ?", (user["id"], now, remarks, request_id, req["current_stage"]))
        conn.execute("UPDATE request_approvals SET status = 'CANCELLED' WHERE request_id = ? AND status = 'WAITING'",
                     (request_id,))
        conn.execute("""UPDATE leave_requests SET status = 'REJECTED', current_stage = NULL, decided_at = ?,
                        decided_by = ?, decision_remarks = ? WHERE id = ?""", (now, user["id"], remarks, request_id))
        lt = leave_type(req["leave_type_id"])
        if lt["balance_tracked"]:
            bal = ensure_balance(conn, req["employee_id"], lt, req["leave_year"])
            conn.execute("UPDATE leave_balances SET pending = MAX(pending - ?, 0) WHERE id = ?", (req["days"], bal["id"]))
        new = _request_context(conn, request_id)
        audit.log("LEAVE_REJECTED", "leave_request", request_id, prev=_state(req), new=_state(new), remarks=remarks)
        notify([req["applicant_user_id"]], "Leave rejected",
               f"{_yours(req, req['leave_type_name'])} has been rejected by {user['full_name']}. "
               f"Remarks: {remarks}", _link(request_id))


def can_cancel(user, req):
    if req["employee_id"] != user["employee_id"] or "leave.apply" not in user["permissions"]:
        return False
    if req["status"] == "PENDING":
        return True
    if req["status"] == "APPROVED":
        return bool(get_int_setting("allow_cancel_approved")) and req["from_date"] > today().isoformat()
    return False


def cancel(user, request_id, reason):
    reason = clean_text(reason, "Reason for cancellation", max_len=500) or None
    with transaction() as conn:
        req = _request_context(conn, request_id)
        if req is None or req["employee_id"] != user["employee_id"]:
            raise PermissionError("You can only cancel your own leave requests.")
        if not can_cancel(user, req):
            raise ValidationError("This request can no longer be withdrawn or cancelled.")
        was = req["status"]
        to_notify = [r["acted_by"] for r in conn.execute(
            "SELECT acted_by FROM request_approvals WHERE request_id = ? AND acted_by IS NOT NULL", (request_id,))]
        if was == "PENDING":
            stage = workflow.current_stage(req)
            if stage:
                to_notify += workflow.eligible_approvers(stage, req["employee_id"])
        conn.execute("UPDATE request_approvals SET status = 'CANCELLED' WHERE request_id = ? AND status IN ('WAITING', 'PENDING')",
                     (request_id,))
        conn.execute("""UPDATE leave_requests SET status = 'CANCELLED', current_stage = NULL, cancelled_at = ?,
                        cancelled_by = ?, cancel_reason = ? WHERE id = ?""",
                     (utcnow_iso(), user["id"], reason, request_id))
        lt = leave_type(req["leave_type_id"])
        if lt["balance_tracked"]:
            bal = ensure_balance(conn, req["employee_id"], lt, req["leave_year"])
            col = "pending" if was == "PENDING" else "used"
            conn.execute(f"UPDATE leave_balances SET {col} = MAX({col} - ?, 0) WHERE id = ?", (req["days"], bal["id"]))
        new = _request_context(conn, request_id)
        action = "LEAVE_WITHDRAWN" if was == "PENDING" else "LEAVE_CANCELLED"
        audit.log(action, "leave_request", request_id, prev=_state(req), new=_state(new), remarks=reason)
        verb = "withdrawn" if was == "PENDING" else "cancelled"
        notify([user["id"]], f"Leave {verb}", f"{_yours(req, req['leave_type_name'])} has been {verb}.",
               _link(request_id))
        notify([u for u in to_notify if u != user["id"]], f"Leave request {verb} by employee",
               f"{req['employee_name']} has {verb} the {req['leave_type_name']} application for {_period(req)}. "
               "No action is needed.", _link(request_id))


# ---------------------------------------------------------------- reads
REQUEST_SELECT = """
    SELECT r.*, lt.name AS leave_type_name, e.full_name AS employee_name, e.emp_code,
           d.name AS department, g.name AS designation, du.username AS decided_by_username,
           de.full_name AS decided_by_name,
           (SELECT stage_name FROM request_approvals ra WHERE ra.request_id = r.id AND ra.seq = r.current_stage)
               AS current_stage_name
      FROM leave_requests r
      JOIN leave_types lt ON lt.id = r.leave_type_id
      JOIN employees e ON e.id = r.employee_id
      LEFT JOIN departments d ON d.id = e.department_id
      LEFT JOIN designations g ON g.id = e.designation_id
      LEFT JOIN users du ON du.id = r.decided_by
      LEFT JOIN employees de ON de.id = du.employee_id
"""


def request_detail(request_id):
    return query(REQUEST_SELECT + " WHERE r.id = ?", (request_id,), one=True)


def requests_for_employee(employee_id, limit=None):
    sql = REQUEST_SELECT + " WHERE r.employee_id = ? ORDER BY r.submitted_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query(sql, (employee_id,))


def requests_in_scope(scope_ids, where="1=1", params=(), order="r.submitted_at DESC", limit=None):
    sql = REQUEST_SELECT + f" WHERE ({where})"
    params = list(params)
    if scope_ids is not None:
        ids = sorted(scope_ids) or [-1]
        sql += f" AND r.employee_id IN ({','.join('?' for _ in ids)})"
        params += ids
    sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query(sql, params)


def approval_trail(request_id):
    return query(
        """SELECT ra.*, ro.name AS role_name, fr.name AS fallback_role_name, au.username AS acted_by_username,
                  ae.full_name AS acted_by_name, tu.username AS target_username
             FROM request_approvals ra
             LEFT JOIN roles ro ON ro.id = ra.approver_role_id
             LEFT JOIN roles fr ON fr.id = ra.fallback_role_id
             LEFT JOIN users au ON au.id = ra.acted_by
             LEFT JOIN employees ae ON ae.id = au.employee_id
             LEFT JOIN users tu ON tu.id = ra.approver_user_id
            WHERE ra.request_id = ? ORDER BY ra.seq""", (request_id,))


def valid_employment_type(value):
    value = (value or "").strip()
    if value and value not in employment_types():
        raise ValidationError("Select a valid employment type.")
    return value or None
