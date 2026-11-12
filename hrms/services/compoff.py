"""Compensatory off: claiming credit for work on a non-working day.

The employee names the officer who assigned the work (any user holding 'compoff.approve',
e.g. CEO, Joint CEO, Joint Secretary, HR or a reporting manager). Only that officer may
approve or reject; if they are no longer able to act, HR users take over. Approved days
are credited to the balance of the credit-based leave type, which is then availed through
the normal leave workflow.
"""
from flask import url_for

from ..core import audit
from ..core.db import query, transaction
from ..core.utils import (ValidationError, clean_text, fmt_date, fmt_days, get_int_setting, parse_date,
                          today, utcnow_iso, weekend_days)
from . import cycles
from . import leave as leave_svc
from . import workflow
from .notifications import notify

DAY_OPTIONS = {"1": 1.0, "0.5": 0.5}


def credit_leave_type():
    return query("SELECT * FROM leave_types WHERE credit_based = 1 AND is_active = 1 ORDER BY id LIMIT 1", one=True)


def _approver_ids_with_perm(extra_where="", params=()):
    return [r["id"] for r in query(
        f"""SELECT u.id FROM users u JOIN employees e ON e.id = u.employee_id
             WHERE u.is_active = 1 AND e.status = 'ACTIVE'
               AND EXISTS (SELECT 1 FROM role_permissions rp
                            WHERE rp.role_id = u.role_id AND rp.permission_code = 'compoff.approve') {extra_where}""",
        params)]


def approver_choices(employee_id):
    """Officers an employee may name as having assigned the work; reporting officer first."""
    ro = query("SELECT reporting_officer_id FROM employees WHERE id = ?", (employee_id,), one=True)
    ro_emp = ro["reporting_officer_id"] if ro else None
    rows = query(
        """SELECT u.id, e.id AS employee_id, e.full_name, g.name AS designation, r.name AS role_name
             FROM users u JOIN employees e ON e.id = u.employee_id JOIN roles r ON r.id = u.role_id
             LEFT JOIN designations g ON g.id = e.designation_id
            WHERE u.is_active = 1 AND e.status = 'ACTIVE' AND e.id != ?
              AND EXISTS (SELECT 1 FROM role_permissions rp
                           WHERE rp.role_id = u.role_id AND rp.permission_code = 'compoff.approve')
            ORDER BY e.full_name""", (employee_id,))
    out = [dict(r, is_ro=r["employee_id"] == ro_emp) for r in rows]
    return sorted(out, key=lambda r: not r["is_ro"])


def eligible_approvers(claim):
    """The assigning officer; HR users if that officer can no longer act."""
    applicant = query("SELECT id FROM users WHERE employee_id = ?", (claim["employee_id"],), one=True)
    applicant_uid = applicant["id"] if applicant else None
    primary = [u for u in _approver_ids_with_perm("AND u.id = ?", (claim["approver_user_id"],)) if u != applicant_uid]
    if primary:
        return primary
    return [u for u in _approver_ids_with_perm(
        "AND u.role_id IN (SELECT id FROM roles WHERE code = 'HR')") if u != applicant_uid]


def can_act(user, claim):
    return (claim["status"] == "PENDING" and "compoff.approve" in user["permissions"]
            and claim["employee_id"] != user["employee_id"] and user["id"] in eligible_approvers(claim))


def _worked(claim):
    span = "half day" if claim["days"] == 0.5 else "full day"
    return f"{fmt_date(claim['worked_date'])} ({span})"


def _state(claim):
    return {k: claim[k] for k in ("status", "worked_date", "days", "approver_user_id", "decided_by", "decision_remarks")}


# ---------------------------------------------------------------- claim
def submit(user, form):
    emp = leave_svc.employee(user["employee_id"])
    if emp is None or emp["status"] != "ACTIVE":
        raise ValidationError("Your employee record is not active.")
    lt = credit_leave_type()
    if lt is None:
        raise ValidationError("Compensatory off is not configured. Please contact the administrator.")

    worked = parse_date(form.get("worked_date"), "Date worked")
    if worked > today():
        raise ValidationError("Comp-off can only be claimed for a date on which the work has already been done.")
    if worked.isoformat() < (emp["joining_date"] or ""):
        raise ValidationError("The date worked is before your joining date.")
    if get_int_setting("compoff_non_working_only"):
        if worked.weekday() not in weekend_days() and not leave_svc.holiday_dates(worked, worked):
            raise ValidationError("Comp-off can only be claimed for work done on a weekly off or a holiday.")
    days = DAY_OPTIONS.get(form.get("days", ""))
    if days is None:
        raise ValidationError("Select full day or half day.")
    details = clean_text(form.get("work_details"), "Work done", required=True, max_len=1000)
    approver = form.get("approver_user_id", "")
    choices = {c["id"]: c for c in approver_choices(emp["id"])}
    if not approver.isdigit() or int(approver) not in choices:
        raise ValidationError("Select the officer who assigned the work.")
    approver = int(approver)

    with transaction() as conn:
        dup = conn.execute("""SELECT id FROM compoff_claims WHERE employee_id = ? AND worked_date = ?
                              AND status IN ('PENDING', 'APPROVED')""", (emp["id"], worked.isoformat())).fetchone()
        if dup:
            raise ValidationError(f"You already have a comp-off claim (#{dup['id']}) for {fmt_date(worked)}.")
        on_leave = conn.execute("""SELECT id FROM leave_requests WHERE employee_id = ? AND status = 'APPROVED'
                                   AND from_date <= ? AND to_date >= ?""",
                                (emp["id"], worked.isoformat(), worked.isoformat())).fetchone()
        if on_leave:
            raise ValidationError(f"You were on approved leave (#{on_leave['id']}) on {fmt_date(worked)}.")
        claim_id = conn.execute(
            """INSERT INTO compoff_claims (employee_id, leave_type_id, worked_date, days, leave_year, work_details,
                                           approver_user_id, status, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
            (emp["id"], lt["id"], worked.isoformat(), days, cycles.cycle_for(emp["id"], worked).key, details, approver,
             utcnow_iso())).lastrowid
        claim = conn.execute("SELECT * FROM compoff_claims WHERE id = ?", (claim_id,)).fetchone()
        audit.log("COMPOFF_CLAIMED", "compoff_claim", claim_id, new=_state(claim), remarks=details)
        notify([user["id"]], "Comp-off claim submitted",
               f"Your comp-off claim for work on {_worked(claim)} has been sent to "
               f"{choices[approver]['full_name']} for approval.", url_for("compoff.index"))
        notify(eligible_approvers(claim), "New comp-off claim requires your action",
               f"{emp['full_name']} has claimed comp-off for work on {_worked(claim)}: \"{details[:200]}\". "
               "Please confirm the work and approve or reject the claim.", url_for("compoff.approvals"))
    return claim_id


def _load_for_action(conn, user, claim_id):
    claim = conn.execute("""SELECT c.*, e.full_name AS employee_name, u.id AS applicant_user_id
                              FROM compoff_claims c JOIN employees e ON e.id = c.employee_id
                              LEFT JOIN users u ON u.employee_id = e.id WHERE c.id = ?""", (claim_id,)).fetchone()
    if claim is None:
        raise ValidationError("Comp-off claim not found.")
    if claim["status"] != "PENDING":
        raise ValidationError("This claim has already been acted upon. Please refresh.")
    if not can_act(user, claim):
        raise PermissionError("You are not authorised to act on this comp-off claim.")
    return claim


def approve(user, claim_id, remarks):
    remarks = clean_text(remarks, "Remarks", max_len=1000) or None
    with transaction() as conn:
        claim = _load_for_action(conn, user, claim_id)
        conn.execute("""UPDATE compoff_claims SET status = 'APPROVED', decided_at = ?, decided_by = ?,
                        decision_remarks = ? WHERE id = ?""", (utcnow_iso(), user["id"], remarks, claim_id))
        lt = leave_svc.leave_type(claim["leave_type_id"])
        bal = leave_svc.ensure_balance(conn, claim["employee_id"], lt, claim["leave_year"])
        conn.execute("UPDATE leave_balances SET entitlement = COALESCE(entitlement, 0) + ? WHERE id = ?",
                     (claim["days"], bal["id"]))
        new = conn.execute("SELECT * FROM compoff_claims WHERE id = ?", (claim_id,)).fetchone()
        audit.log("COMPOFF_APPROVED", "compoff_claim", claim_id, prev=_state(claim), new=_state(new),
                  remarks=remarks)
        audit.log("BALANCE_CREDITED", "leave_balance", bal["id"],
                  prev={"entitlement": bal["entitlement"]},
                  new={"entitlement": (bal["entitlement"] or 0) + claim["days"], "leave_type": lt["code"],
                       "year": claim["leave_year"]}, remarks=f"Comp-off claim #{claim_id}")
        notify([claim["applicant_user_id"]], "Comp-off approved",
               f"Your comp-off claim for work on {_worked(claim)} has been approved by {user['full_name']}. "
               f"{fmt_days(claim['days'])} day(s) added to your {lt['name']} balance."
               + (f" Remarks: {remarks}" if remarks else ""), url_for("compoff.index"))


def reject(user, claim_id, remarks):
    remarks = clean_text(remarks, "Remarks", required=True, max_len=1000)
    with transaction() as conn:
        claim = _load_for_action(conn, user, claim_id)
        conn.execute("""UPDATE compoff_claims SET status = 'REJECTED', decided_at = ?, decided_by = ?,
                        decision_remarks = ? WHERE id = ?""", (utcnow_iso(), user["id"], remarks, claim_id))
        new = conn.execute("SELECT * FROM compoff_claims WHERE id = ?", (claim_id,)).fetchone()
        audit.log("COMPOFF_REJECTED", "compoff_claim", claim_id, prev=_state(claim), new=_state(new), remarks=remarks)
        notify([claim["applicant_user_id"]], "Comp-off rejected",
               f"Your comp-off claim for work on {_worked(claim)} has been rejected by {user['full_name']}. "
               f"Remarks: {remarks}",
               url_for("compoff.index"))


def withdraw(user, claim_id):
    with transaction() as conn:
        claim = conn.execute("SELECT * FROM compoff_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None or claim["employee_id"] != user["employee_id"]:
            raise PermissionError("You can only withdraw your own claims.")
        if claim["status"] != "PENDING":
            raise ValidationError("Only pending claims can be withdrawn.")
        approvers = eligible_approvers(claim)
        conn.execute("UPDATE compoff_claims SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                     (utcnow_iso(), claim_id))
        new = conn.execute("SELECT * FROM compoff_claims WHERE id = ?", (claim_id,)).fetchone()
        audit.log("COMPOFF_WITHDRAWN", "compoff_claim", claim_id, prev=_state(claim), new=_state(new))
        notify(approvers, "Comp-off claim withdrawn by employee",
               f"{user['full_name']} has withdrawn the comp-off claim for {_worked(claim)}. No action is needed.",
               url_for("compoff.approvals"))


# ---------------------------------------------------------------- reads
CLAIM_SELECT = """
    SELECT c.*, e.full_name AS employee_name, e.emp_code, d.name AS department, g.name AS designation,
           ae.full_name AS approver_name, de.full_name AS decided_by_name
      FROM compoff_claims c
      JOIN employees e ON e.id = c.employee_id
      LEFT JOIN departments d ON d.id = e.department_id
      LEFT JOIN designations g ON g.id = e.designation_id
      LEFT JOIN users au ON au.id = c.approver_user_id
      LEFT JOIN employees ae ON ae.id = au.employee_id
      LEFT JOIN users du ON du.id = c.decided_by
      LEFT JOIN employees de ON de.id = du.employee_id
"""


def claims_for_employee(employee_id, limit=None):
    sql = CLAIM_SELECT + " WHERE c.employee_id = ? ORDER BY c.submitted_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query(sql, (employee_id,))


def actionable_claims(user):
    if "compoff.approve" not in user["permissions"]:
        return []
    rows = query(CLAIM_SELECT + " WHERE c.status = 'PENDING' AND c.employee_id != ? ORDER BY c.submitted_at",
                 (user["employee_id"],))
    return [r for r in rows if user["id"] in eligible_approvers(r)]


def decided_by(user, limit=20):
    return query(CLAIM_SELECT + " WHERE c.decided_by = ? ORDER BY c.decided_at DESC LIMIT ?", (user["id"], limit))


def claims_in_scope(scope_ids, limit=300):
    sql, params = CLAIM_SELECT, []
    if scope_ids is not None:
        ids = sorted(scope_ids) or [-1]
        sql += f" WHERE c.employee_id IN ({','.join('?' for _ in ids)})"
        params = ids
    return query(sql + f" ORDER BY c.submitted_at DESC LIMIT {int(limit)}", params)


def visible_claim_ids(user):
    """Claims whose audit history this user may see (own, assigned/decided, or in scope)."""
    ids = {r["id"] for r in query("SELECT id FROM compoff_claims WHERE employee_id = ? OR approver_user_id = ? "
                                  "OR decided_by = ?", (user["employee_id"], user["id"], user["id"]))}
    if "audit.view_scope" in user["permissions"]:
        ids |= {r["id"] for r in claims_in_scope(workflow.visible_employee_ids(user), limit=100000)}
    return ids
