"""Resignation and exit.

1. The employee submits a resignation with a reason and the last working day they ask for.
2. Their reporting officer adds remarks and forwards it to HR. If there is no reporting officer
   who can act (none recorded, inactive, or without supervisory rights) it goes straight to HR.
3. HR (any user holding 'exit.manage', never the applicant) accepts it and fixes the last
   working day. If the only HR user is the one resigning, the CEO handles it.
4. HR ticks off the exit clearance checklist (configurable in settings) and, on or after the
   last working day, relieves the employee: the login is closed and the employee is marked
   inactive. Every step is audited.

The employee may withdraw until HR accepts; after that HR may revoke the acceptance (the
employee stays). There is no notice-period rule: the configured period is only shown.
"""
from datetime import date, timedelta

from flask import url_for

from ..core import audit
from ..core.db import query, transaction
from ..core.utils import (ValidationError, clean_text, fmt_date, get_int_setting, get_setting, parse_date, today,
                          utcnow_iso)
from . import workflow
from .notifications import notify

REASONS = ["Personal reasons", "Higher studies", "Another job opportunity", "Health", "Relocation",
           "End of assignment", "Other"]
OPEN = ("WITH_OFFICER", "WITH_HR", "ACCEPTED")
STATUS_LABELS = {"WITH_OFFICER": "With reporting officer", "WITH_HR": "With HR", "ACCEPTED": "Accepted",
                 "RELIEVED": "Relieved", "WITHDRAWN": "Withdrawn", "REVOKED": "Acceptance revoked"}


def notice_days():
    n = get_int_setting("resignation_notice_days")
    return n if n > 0 else None


def clearance_items():
    return [p.strip() for p in get_setting("exit_clearance_items").split(";") if p.strip()]


def _user_id_of(employee_id):
    row = query("SELECT id FROM users WHERE employee_id = ?", (employee_id,), one=True)
    return row["id"] if row else None


def officer_for(employee_id):
    """User id of the reporting officer, if they can act on the resignation."""
    row = query("""SELECT u.id FROM employees e JOIN users u ON u.employee_id = e.reporting_officer_id
                    WHERE e.id = ?""", (employee_id,), one=True)
    if row and row["id"] != _user_id_of(employee_id) and workflow._user_can_approve(row["id"]):
        return row["id"]
    return None


def _active_with(where, params=()):
    return [r["id"] for r in query(
        f"""SELECT u.id FROM users u JOIN employees e ON e.id = u.employee_id
             WHERE u.is_active = 1 AND e.status = 'ACTIVE' AND {where}""", params)]


def hr_handlers(employee_id):
    applicant = _user_id_of(employee_id)
    hr = [u for u in _active_with("""EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_id = u.role_id
                                             AND rp.permission_code = 'exit.manage')""") if u != applicant]
    if hr:
        return hr
    return [u for u in _active_with("u.role_id IN (SELECT id FROM roles WHERE code = 'CEO')") if u != applicant]


def stage_actors(res):
    """Users who may act on the resignation in its current state."""
    if res["status"] == "WITH_OFFICER":
        officer = res["officer_user_id"]
        if officer and officer != _user_id_of(res["employee_id"]) and workflow._user_can_approve(officer):
            return [officer]
        return hr_handlers(res["employee_id"])
    if res["status"] in ("WITH_HR", "ACCEPTED"):
        return hr_handlers(res["employee_id"])
    return []


def can_act(user, res):
    return res["employee_id"] != user["employee_id"] and user["id"] in stage_actors(res)


def can_view(user, res):
    if res["employee_id"] == user["employee_id"] or "exit.manage" in user["permissions"]:
        return True
    if user["id"] in (res["officer_user_id"], res["accepted_by"], res["relieved_by"]) or can_act(user, res):
        return True
    return "leave.view_team" in user["permissions"] and \
        res["employee_id"] in workflow.subordinate_employee_ids(user["employee_id"])


def _state(res):
    return {k: res[k] for k in ("status", "proposed_lwd", "accepted_lwd")}


def _load(conn, rid):
    res = conn.execute("""SELECT r.*, e.full_name AS employee_name, u.id AS applicant_user_id
                            FROM resignations r JOIN employees e ON e.id = r.employee_id
                            LEFT JOIN users u ON u.employee_id = e.id WHERE r.id = ?""", (rid,)).fetchone()
    if res is None:
        raise ValidationError("Resignation not found.")
    return res


def _for_action(conn, user, rid, statuses):
    res = _load(conn, rid)
    if res["status"] not in statuses:
        raise ValidationError("This resignation has already moved on. Please refresh the page.")
    if not can_act(user, res):
        raise PermissionError("You are not authorised to act on this resignation.")
    return res


def _link(rid):
    return url_for("exits.detail", rid=rid)


def _date_in_range(value, label, lo, hi):
    d = parse_date(value, label)
    if d < lo:
        raise ValidationError(f"{label} cannot be before {fmt_date(lo)}.")
    if d > hi:
        raise ValidationError(f"{label} cannot be after {fmt_date(hi)}.")
    return d


# ---------------------------------------------------------------- employee
def open_resignation(employee_id):
    return query(f"SELECT * FROM resignations WHERE employee_id = ? AND status IN ({','.join('?' * len(OPEN))}) "
                 "ORDER BY id DESC LIMIT 1", (employee_id, *OPEN), one=True)


def submit(user, form):
    emp = query("SELECT * FROM employees WHERE id = ?", (user["employee_id"],), one=True)
    if emp is None or emp["status"] != "ACTIVE":
        raise ValidationError("Your employee record is not active.")
    category = form.get("reason_category", "")
    if category not in REASONS:
        raise ValidationError("Select the reason for leaving.")
    reason = clean_text(form.get("reason"), "Details", required=True, max_len=2000)
    lwd = _date_in_range(form.get("proposed_lwd"), "Last working day", today(), today() + timedelta(days=365))
    if form.get("confirm") != "1":
        raise ValidationError("Tick the box to confirm that you wish to resign.")
    officer = officer_for(emp["id"])
    status = "WITH_OFFICER" if officer else "WITH_HR"
    with transaction() as conn:
        if conn.execute(f"SELECT 1 FROM resignations WHERE employee_id = ? AND status IN ({','.join('?' * len(OPEN))})",
                        (emp["id"], *OPEN)).fetchone():
            raise ValidationError("You already have a resignation in progress.")
        rid = conn.execute(
            """INSERT INTO resignations (employee_id, status, reason_category, reason, proposed_lwd, notice_days,
                                         submitted_at, officer_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (emp["id"], status, category, reason, lwd.isoformat(), notice_days(), utcnow_iso(), officer)).lastrowid
        res = _load(conn, rid)
        audit.log("RESIGNATION_SUBMITTED", "resignation", rid, new=_state(res), remarks=category)
        notify([user["id"]], "Resignation submitted",
               f"Your resignation with a requested last working day of {fmt_date(lwd)} has been sent to "
               f"{'your reporting officer' if officer else 'HR'}. You can withdraw it until HR accepts it.", _link(rid))
        notify(stage_actors(res), "Resignation received",
               f"{emp['full_name']} has submitted a resignation ({category}) with a requested last working day of "
               f"{fmt_date(lwd)}. Please review it.", _link(rid))
    return rid


def withdraw(user, rid, remarks):
    remarks = clean_text(remarks, "Remarks", max_len=1000) or None
    with transaction() as conn:
        res = _load(conn, rid)
        if res["employee_id"] != user["employee_id"]:
            raise PermissionError("You can only withdraw your own resignation.")
        if res["status"] not in ("WITH_OFFICER", "WITH_HR"):
            raise ValidationError("A resignation can be withdrawn only until HR accepts it. Please speak to HR.")
        actors = stage_actors(res)
        conn.execute("""UPDATE resignations SET status = 'WITHDRAWN', closed_remarks = ?, closed_by = ?, closed_at = ?
                        WHERE id = ?""", (remarks, user["id"], utcnow_iso(), rid))
        audit.log("RESIGNATION_WITHDRAWN", "resignation", rid, prev=_state(res),
                  new=_state(_load(conn, rid)), remarks=remarks)
        notify(actors + [res["officer_user_id"]], "Resignation withdrawn",
               f"{res['employee_name']} has withdrawn their resignation. No action is needed.", _link(rid))


# ---------------------------------------------------------------- reporting officer
def forward(user, rid, remarks):
    remarks = clean_text(remarks, "Remarks", required=True, max_len=1000)
    with transaction() as conn:
        res = _for_action(conn, user, rid, ("WITH_OFFICER",))
        conn.execute("""UPDATE resignations SET status = 'WITH_HR', officer_remarks = ?, officer_at = ?,
                        officer_user_id = ? WHERE id = ?""", (remarks, utcnow_iso(), user["id"], rid))
        new = _load(conn, rid)
        audit.log("RESIGNATION_FORWARDED", "resignation", rid, prev=_state(res), new=_state(new), remarks=remarks)
        notify([res["applicant_user_id"]], "Resignation forwarded to HR",
               f"{user['full_name']} has forwarded your resignation to HR.", _link(rid))
        notify(stage_actors(new), "Resignation awaiting acceptance",
               f"{user['full_name']} has forwarded the resignation of {res['employee_name']} "
               f"(requested last working day {fmt_date(res['proposed_lwd'])}). Remarks: {remarks}", _link(rid))


# ---------------------------------------------------------------- HR
def accept(user, rid, lwd_value, remarks):
    remarks = clean_text(remarks, "Remarks", max_len=1000) or None
    with transaction() as conn:
        res = _for_action(conn, user, rid, ("WITH_HR",))
        lwd = _date_in_range(lwd_value, "Last working day", date.fromisoformat(res["submitted_at"][:10]),
                             today() + timedelta(days=365))
        conn.execute("""UPDATE resignations SET status = 'ACCEPTED', accepted_lwd = ?, hr_remarks = ?, accepted_by = ?,
                        accepted_at = ? WHERE id = ?""", (lwd.isoformat(), remarks, user["id"], utcnow_iso(), rid))
        for seq, item in enumerate(clearance_items(), 1):
            conn.execute("INSERT OR IGNORE INTO exit_clearance (resignation_id, seq, item) VALUES (?, ?, ?)",
                         (rid, seq, item))
        audit.log("RESIGNATION_ACCEPTED", "resignation", rid, prev=_state(res), new=_state(_load(conn, rid)),
                  remarks=remarks)
        notify([res["applicant_user_id"], res["officer_user_id"]], "Resignation accepted",
               f"The resignation of {res['employee_name']} has been accepted by {user['full_name']}. "
               f"Last working day: {fmt_date(lwd)}." + (f" Remarks: {remarks}" if remarks else ""), _link(rid))


def set_clearance(user, rid, seq, done, note):
    note = clean_text(note, "Note", max_len=300) or None
    with transaction() as conn:
        res = _for_action(conn, user, rid, ("ACCEPTED",))
        row = conn.execute("SELECT * FROM exit_clearance WHERE resignation_id = ? AND seq = ?", (rid, seq)).fetchone()
        if row is None:
            raise ValidationError("Clearance item not found.")
        if done:
            conn.execute("UPDATE exit_clearance SET done_by = ?, done_at = ?, note = ? WHERE id = ?",
                         (user["id"], utcnow_iso(), note, row["id"]))
        else:
            conn.execute("UPDATE exit_clearance SET done_by = NULL, done_at = NULL, note = ? WHERE id = ?",
                         (note, row["id"]))
        audit.log("EXIT_CLEARANCE_UPDATED", "resignation", rid, prev={"item": row["item"], "done": bool(row["done_at"])},
                  new={"item": row["item"], "done": bool(done)}, remarks=note)
    return res


def relieve_blockers(res):
    """Reasons the employee cannot be relieved yet (empty list = ready)."""
    out = []
    if res["status"] != "ACCEPTED":
        return ["The resignation has not been accepted."]
    if today().isoformat() < res["accepted_lwd"]:
        out.append(f"The last working day ({fmt_date(res['accepted_lwd'])}) has not been reached.")
    left = query("SELECT COUNT(*) AS n FROM exit_clearance WHERE resignation_id = ? AND done_at IS NULL",
                 (res["id"],), one=True)["n"]
    if left:
        out.append(f"{left} exit clearance item{'s are' if left != 1 else ' is'} not complete.")
    n = query("SELECT COUNT(*) AS n FROM leave_requests WHERE employee_id = ? AND status = 'PENDING'",
              (res["employee_id"],), one=True)["n"]
    if n:
        out.append(f"{n} leave request{'s are' if n != 1 else ' is'} still pending a decision.")
    n = query("SELECT COUNT(*) AS n FROM compoff_claims WHERE employee_id = ? AND status = 'PENDING'",
              (res["employee_id"],), one=True)["n"]
    if n:
        out.append(f"{n} comp-off claim{'s are' if n != 1 else ' is'} still pending a decision.")
    uid = _user_id_of(res["employee_id"])
    if uid and query("""SELECT 1 FROM users u JOIN role_permissions rp ON rp.role_id = u.role_id
                         WHERE u.id = ? AND rp.permission_code = 'roles.manage'""", (uid,), one=True) and \
            not query("""SELECT 1 FROM users u JOIN role_permissions rp ON rp.role_id = u.role_id
                          WHERE u.is_active = 1 AND u.id != ? AND rp.permission_code = 'roles.manage'""", (uid,), one=True):
        out.append("This person is the only active user with role-management rights. Give another user that right first.")
    return out


def reportees(employee_id):
    return query("SELECT id, full_name, emp_code FROM employees WHERE reporting_officer_id = ? AND status = 'ACTIVE' "
                 "ORDER BY full_name", (employee_id,))


def relieve(user, rid, remarks):
    remarks = clean_text(remarks, "Remarks", max_len=1000) or None
    with transaction() as conn:
        res = _for_action(conn, user, rid, ("ACCEPTED",))
        blockers = relieve_blockers(res)
        if blockers:
            raise ValidationError(" ".join(blockers))
        now = utcnow_iso()
        conn.execute("UPDATE resignations SET status = 'RELIEVED', relieved_by = ?, relieved_at = ?, "
                     "closed_remarks = COALESCE(?, closed_remarks) WHERE id = ?", (user["id"], now, remarks, rid))
        conn.execute("UPDATE employees SET status = 'INACTIVE', updated_at = ? WHERE id = ?", (now, res["employee_id"]))
        if res["applicant_user_id"]:
            conn.execute("UPDATE users SET is_active = 0, session_version = session_version + 1 WHERE id = ?",
                         (res["applicant_user_id"],))
            audit.log("USER_DEACTIVATED", "user", res["applicant_user_id"], prev={"is_active": 1},
                      new={"is_active": 0}, remarks=f"Relieved on resignation #{rid}")
        audit.log("EMPLOYEE_RELIEVED", "resignation", rid, prev=_state(res), new=_state(_load(conn, rid)),
                  remarks=remarks)
        notify([res["officer_user_id"]], "Employee relieved",
               f"{res['employee_name']} has been relieved with effect from {fmt_date(res['accepted_lwd'])}.", _link(rid))


def revoke(user, rid, remarks):
    remarks = clean_text(remarks, "Remarks", required=True, max_len=1000)
    with transaction() as conn:
        res = _for_action(conn, user, rid, ("ACCEPTED",))
        conn.execute("""UPDATE resignations SET status = 'REVOKED', closed_remarks = ?, closed_by = ?, closed_at = ?
                        WHERE id = ?""", (remarks, user["id"], utcnow_iso(), rid))
        audit.log("RESIGNATION_REVOKED", "resignation", rid, prev=_state(res), new=_state(_load(conn, rid)),
                  remarks=remarks)
        notify([res["applicant_user_id"], res["officer_user_id"]], "Resignation acceptance revoked",
               f"HR has revoked the acceptance of the resignation of {res['employee_name']}; the employee continues "
               f"in service. Remarks: {remarks}", _link(rid))


# ---------------------------------------------------------------- reads
SELECT = """
    SELECT r.*, e.full_name AS employee_name, e.emp_code, e.joining_date, d.name AS department, g.name AS designation,
           oe.full_name AS officer_name, ae.full_name AS accepted_by_name, re.full_name AS relieved_by_name,
           ce.full_name AS closed_by_name,
           (SELECT COUNT(*) FROM exit_clearance x WHERE x.resignation_id = r.id) AS items,
           (SELECT COUNT(*) FROM exit_clearance x WHERE x.resignation_id = r.id AND x.done_at IS NOT NULL) AS items_done
      FROM resignations r
      JOIN employees e ON e.id = r.employee_id
      LEFT JOIN departments d ON d.id = e.department_id
      LEFT JOIN designations g ON g.id = e.designation_id
      LEFT JOIN users ou ON ou.id = r.officer_user_id LEFT JOIN employees oe ON oe.id = ou.employee_id
      LEFT JOIN users au ON au.id = r.accepted_by LEFT JOIN employees ae ON ae.id = au.employee_id
      LEFT JOIN users ru ON ru.id = r.relieved_by LEFT JOIN employees re ON re.id = ru.employee_id
      LEFT JOIN users cu ON cu.id = r.closed_by LEFT JOIN employees ce ON ce.id = cu.employee_id
"""


def detail(rid):
    return query(SELECT + " WHERE r.id = ?", (rid,), one=True)


def clearance(rid):
    return query("""SELECT x.*, e.full_name AS done_by_name FROM exit_clearance x
                      LEFT JOIN users u ON u.id = x.done_by LEFT JOIN employees e ON e.id = u.employee_id
                     WHERE x.resignation_id = ? ORDER BY x.seq""", (rid,))


def for_employee(employee_id):
    return query(SELECT + " WHERE r.employee_id = ? ORDER BY r.id DESC", (employee_id,))


def notice_note(res):
    """Plain description of the notice given compared with the configured period."""
    given = (date.fromisoformat(res["accepted_lwd"] or res["proposed_lwd"]) -
             date.fromisoformat(res["submitted_at"][:10])).days
    if not res["notice_days"]:
        return f"{given} days' notice given. No notice period is configured."
    if given >= res["notice_days"]:
        return f"{given} days' notice given; the notice period is {res['notice_days']} days."
    return f"{given} days' notice given, {res['notice_days'] - given} days short of the {res['notice_days']}-day notice period."


def actionable(user):
    """Open resignations waiting on this user. Accepted ones count only once relieving is due."""
    rows = query(SELECT + f" WHERE r.status IN ({','.join('?' * len(OPEN))}) AND r.employee_id != ? ORDER BY r.id",
                 (*OPEN, user["employee_id"]))
    out = []
    for r in rows:
        if not can_act(user, r):
            continue
        if r["status"] == "ACCEPTED" and today().isoformat() < r["accepted_lwd"]:
            continue
        out.append(r)
    return out


def in_scope(user):
    """Every resignation this user may see on the Resignations page."""
    if "exit.manage" in user["permissions"]:
        return query(SELECT + " ORDER BY CASE WHEN r.status IN ('WITH_OFFICER', 'WITH_HR', 'ACCEPTED') THEN 0 ELSE 1 END, "
                              "r.id DESC LIMIT 500")
    rows = query(SELECT + " WHERE r.employee_id != ? ORDER BY r.id DESC LIMIT 500", (user["employee_id"],))
    return [r for r in rows if can_view(user, r)]


def visible_ids(user):
    if "exit.manage" in user["permissions"]:
        return {r["id"] for r in query("SELECT id FROM resignations")}
    return {r["id"] for r in query(SELECT + " WHERE 1 = 1") if can_view(user, r)}
