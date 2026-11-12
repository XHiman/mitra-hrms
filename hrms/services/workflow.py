"""Approval workflow engine.

Workflows are configured as ordered stages. Each stage names *who* may act:
  REPORTING_OFFICER  – the applicant's current reporting officer
  ROLE               – any active user holding that role
  USER               – one specific user
Optionally a stage applies only above a day threshold, and has a fallback role used when
the primary approver cannot be resolved (no reporting officer, inactive user, or the
applicant themselves). An approver must also hold the 'leave.approve' permission and can
never act on their own request.
"""
from ..core.db import query
from ..core.utils import ValidationError

APPROVER_TYPES = {
    "REPORTING_OFFICER": "Reporting officer of the applicant",
    "ROLE": "Any user holding a role",
    "USER": "A specific user",
}


def workflow_for_leave_type(leave_type):
    if leave_type["workflow_id"]:
        wf = query("SELECT * FROM approval_workflows WHERE id = ? AND is_active = 1",
                   (leave_type["workflow_id"],), one=True)
        if wf:
            return wf
    return query("SELECT * FROM approval_workflows WHERE is_default = 1 AND is_active = 1", one=True)


def workflow_stages(workflow_id):
    return query("SELECT * FROM workflow_stages WHERE workflow_id = ? ORDER BY seq", (workflow_id,))


def plan_stages(workflow_id, days):
    """Returns [(stage_row, applies_bool)] for a request of `days` days."""
    return [(s, s["min_days"] is None or days > s["min_days"]) for s in workflow_stages(workflow_id)]


def _approver_users_for_role(role_id):
    return [r["id"] for r in query(
        """SELECT u.id FROM users u JOIN employees e ON e.id = u.employee_id
            WHERE u.role_id = ? AND u.is_active = 1 AND e.status = 'ACTIVE'
              AND EXISTS (SELECT 1 FROM role_permissions rp
                           WHERE rp.role_id = u.role_id AND rp.permission_code = 'leave.approve')""",
        (role_id,))]


def _user_can_approve(user_id):
    return query(
        """SELECT 1 FROM users u JOIN employees e ON e.id = u.employee_id
            JOIN role_permissions rp ON rp.role_id = u.role_id AND rp.permission_code = 'leave.approve'
            WHERE u.id = ? AND u.is_active = 1 AND e.status = 'ACTIVE'""", (user_id,), one=True) is not None


def _applicant_user_id(employee_id):
    row = query("SELECT id FROM users WHERE employee_id = ?", (employee_id,), one=True)
    return row["id"] if row else None


def eligible_approvers(stage, applicant_employee_id):
    """User ids allowed to act on this stage for this applicant."""
    applicant_uid = _applicant_user_id(applicant_employee_id)
    primary = []
    if stage["approver_type"] == "REPORTING_OFFICER":
        row = query(
            """SELECT u.id FROM employees e JOIN users u ON u.employee_id = e.reporting_officer_id
                WHERE e.id = ?""", (applicant_employee_id,), one=True)
        if row and _user_can_approve(row["id"]):
            primary = [row["id"]]
    elif stage["approver_type"] == "ROLE" and stage["approver_role_id"]:
        primary = _approver_users_for_role(stage["approver_role_id"])
    elif stage["approver_type"] == "USER" and stage["approver_user_id"]:
        if _user_can_approve(stage["approver_user_id"]):
            primary = [stage["approver_user_id"]]

    primary = [u for u in primary if u != applicant_uid]
    if primary or not stage["fallback_role_id"]:
        return primary
    return [u for u in _approver_users_for_role(stage["fallback_role_id"]) if u != applicant_uid]


def current_stage(request_row):
    if request_row["status"] != "PENDING":
        return None
    return query("SELECT * FROM request_approvals WHERE request_id = ? AND seq = ? AND status = 'PENDING'",
                 (request_row["id"], request_row["current_stage"]), one=True)


def can_act(user, request_row):
    if "leave.approve" not in user["permissions"]:
        return False
    if request_row["employee_id"] == user["employee_id"]:
        return False
    stage = current_stage(request_row)
    if stage is None:
        return False
    return user["id"] in eligible_approvers(stage, request_row["employee_id"])


def actionable_requests(user):
    """Pending requests whose current stage this user may act on."""
    if "leave.approve" not in user["permissions"]:
        return []
    pending = query("SELECT * FROM leave_requests WHERE status = 'PENDING' AND employee_id != ? ORDER BY submitted_at",
                    (user["employee_id"],))
    return [r for r in pending if can_act(user, r)]


def unresolvable_pending():
    """Pending requests where nobody is currently able to act (admin alert)."""
    out = []
    for r in query("SELECT * FROM leave_requests WHERE status = 'PENDING'"):
        stage = current_stage(r)
        if stage is None or not eligible_approvers(stage, r["employee_id"]):
            out.append(r)
    return out


def snapshot_stages(conn, request_id, workflow_id, days):
    """Copy applicable stages onto the request. Returns the first pending seq."""
    plan = plan_stages(workflow_id, days)
    first = None
    for stage, applies in plan:
        if applies and first is None:
            status, first = "PENDING", stage["seq"]
        else:
            status = "WAITING" if applies else "SKIPPED"
        conn.execute(
            """INSERT INTO request_approvals (request_id, seq, stage_name, approver_type, approver_role_id,
                                              approver_user_id, fallback_role_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (request_id, stage["seq"], stage["name"], stage["approver_type"], stage["approver_role_id"],
             stage["approver_user_id"], stage["fallback_role_id"], status))
    if first is None:
        raise ValidationError("No approval stage applies to this request. Please contact the administrator.")
    return first


def validate_workflow(stages):
    """stages: list of dicts. Raises ValidationError if the configuration is unusable."""
    if not stages:
        raise ValidationError("A workflow needs at least one stage.")
    if stages[0].get("min_days") is not None:
        raise ValidationError("The first stage must apply to every request (leave its day threshold blank).")
    for s in stages:
        if s["approver_type"] == "ROLE" and not s.get("approver_role_id"):
            raise ValidationError(f"Stage '{s['name']}': choose the approving role.")
        if s["approver_type"] == "USER" and not s.get("approver_user_id"):
            raise ValidationError(f"Stage '{s['name']}': choose the approving user.")


# ---------------------------------------------------------------- visibility scope
def subordinate_employee_ids(employee_id):
    """All employees in the reporting chain below employee_id (direct and indirect)."""
    rows = query(
        """WITH RECURSIVE chain(id) AS (
               SELECT id FROM employees WHERE reporting_officer_id = ?
               UNION SELECT e.id FROM employees e JOIN chain c ON e.reporting_officer_id = c.id)
           SELECT id FROM chain""", (employee_id,))
    return {r["id"] for r in rows}


def visible_employee_ids(user):
    """None means 'all employees'."""
    if "leave.view_all" in user["permissions"]:
        return None
    ids = {user["employee_id"]}
    if "leave.view_team" in user["permissions"]:
        ids |= subordinate_employee_ids(user["employee_id"])
    return ids


def involved_in_request(user, request_row):
    if query("SELECT 1 FROM request_approvals WHERE request_id = ? AND acted_by = ?",
             (request_row["id"], user["id"]), one=True):
        return True
    return can_act(user, request_row)


def can_view_request(user, request_row):
    if request_row["employee_id"] == user["employee_id"]:
        return "leave.view_own" in user["permissions"]
    if "leave.view_all" in user["permissions"]:
        return True
    if "leave.view_team" in user["permissions"]:
        if request_row["employee_id"] in subordinate_employee_ids(user["employee_id"]):
            return True
        return involved_in_request(user, request_row)
    return False


def can_view_employee(user, employee_id):
    if employee_id == user["employee_id"]:
        return True
    scope = visible_employee_ids(user)
    if scope is None:
        return True
    if employee_id in scope:
        return True
    # an approver may see the history of anyone whose request is waiting on them
    return any(r["employee_id"] == employee_id for r in actionable_requests(user))
