"""A person's reporting line and projects, as set by HR on the person's page.

The reporting officer decides who approves the person's leave, so staff cannot change their own, and
nobody may edit their own reporting line. Every change is audited and the people affected are notified.
"""
from flask import url_for

from ..core import audit
from ..core.db import query, transaction
from ..core.utils import ValidationError
from .notifications import notify


def check_reporting(employee_id, ro_id):
    """Raises ValidationError if `ro_id` cannot be the reporting officer of `employee_id`."""
    if ro_id is None:
        return
    if ro_id == employee_id:
        raise ValidationError("A person cannot report to themselves.")
    seen, cur = set(), ro_id
    while cur:  # walk upwards; reaching this employee means a cycle
        if cur == employee_id:
            raise ValidationError("That would create a circular reporting line.")
        if cur in seen:
            break
        seen.add(cur)
        row = query("SELECT reporting_officer_id FROM employees WHERE id = ?", (cur,), one=True)
        cur = row["reporting_officer_id"] if row else None


def _active(emp_id, what):
    row = query("SELECT id, full_name, status FROM employees WHERE id = ?", (emp_id,), one=True)
    if row is None or row["status"] != "ACTIVE":
        raise ValidationError(f"Choose an active {what}.")
    return row


def _user_id(emp_id):
    row = query("SELECT id FROM users WHERE employee_id = ?", (emp_id,), one=True)
    return row["id"] if row else None


def _not_self(actor, employee_id):
    if employee_id == actor["employee_id"]:
        raise ValidationError("You cannot change your own reporting line or projects. Please ask another HR user "
                              "or the administrator.")


def _set_ro(conn, actor, emp, ro):
    """Set emp's reporting officer to ro (row or None), audit and notify."""
    old = conn.execute("""SELECT e.reporting_officer_id, r.full_name FROM employees e
                            LEFT JOIN employees r ON r.id = e.reporting_officer_id WHERE e.id = ?""",
                       (emp["id"],)).fetchone()
    new_id = ro["id"] if ro else None
    if old["reporting_officer_id"] == new_id:
        return False
    conn.execute("UPDATE employees SET reporting_officer_id = ? WHERE id = ?", (new_id, emp["id"]))
    audit.log("REPORTING_OFFICER_CHANGED", "employee", emp["id"],
              prev={"reporting_officer": old["full_name"]}, new={"reporting_officer": ro["full_name"] if ro else None},
              remarks=f"Changed by {actor['full_name']}")
    link = url_for("dashboards.profile")
    notify([_user_id(emp["id"])], "Your reporting officer has changed",
           f"Your reporting officer is now {ro['full_name']}." if ro else "You no longer have a reporting officer "
           "recorded; HR handles your approvals until one is set.", link)
    if ro:
        notify([_user_id(ro["id"])], "A team member now reports to you",
               f"{emp['full_name']} now reports to you. Their leave and comp-off requests will come to you.",
               url_for("dashboards.employee_history", employee_id=emp["id"]))
    return True


def set_reporting_officer(actor, employee_id, ro_value):
    _not_self(actor, employee_id)
    emp = _active(employee_id, "employee")
    ro = None
    if ro_value:
        if not str(ro_value).isdigit():
            raise ValidationError("Choose the reporting officer from the list.")
        ro = _active(int(ro_value), "reporting officer")
    check_reporting(employee_id, ro["id"] if ro else None)
    with transaction() as conn:
        return _set_ro(conn, actor, emp, ro)


def add_team_member(actor, officer_id, member_value):
    """Make `member` report to `officer_id`."""
    _not_self(actor, officer_id)
    officer = _active(officer_id, "employee")
    if not str(member_value or "").isdigit():
        raise ValidationError("Choose the person to add from the list.")
    member = _active(int(member_value), "person")
    if member["id"] == actor["employee_id"]:
        raise ValidationError("You cannot change your own reporting officer.")
    check_reporting(member["id"], officer_id)
    with transaction() as conn:
        if not _set_ro(conn, actor, member, officer):
            raise ValidationError(f"{member['full_name']} already reports to {officer['full_name']}.")
    return member


def remove_team_member(actor, officer_id, member_id):
    _not_self(actor, officer_id)
    if member_id == actor["employee_id"]:
        raise ValidationError("You cannot change your own reporting officer.")
    member = query("SELECT id, full_name, reporting_officer_id FROM employees WHERE id = ?", (member_id,), one=True)
    if member is None or member["reporting_officer_id"] != officer_id:
        raise ValidationError("That person does not report to this employee.")
    with transaction() as conn:
        _set_ro(conn, actor, member, None)
    return member


def add_project(actor, employee_id, project_value):
    _not_self(actor, employee_id)
    emp = _active(employee_id, "employee")
    p = query("SELECT * FROM timesheet_projects WHERE id = ?", (project_value,), one=True) \
        if str(project_value or "").isdigit() else None
    if p is None or p["status"] != "ACTIVE":
        raise ValidationError("Choose an active project from the list.")
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM project_members WHERE project_id = ? AND employee_id = ?",
                        (p["id"], employee_id)).fetchone():
            raise ValidationError(f"{emp['full_name']} is already on {p['name']}.")
        conn.execute("INSERT INTO project_members (project_id, employee_id) VALUES (?, ?)", (p["id"], employee_id))
        audit.log("PROJECT_MEMBER_ADDED", "employee", employee_id, new={"project": p["name"]},
                  remarks=f"Added by {actor['full_name']}")
        notify([_user_id(employee_id)], "You have been added to a project",
               f"You are now on the project {p['name']}. It is listed first when you book time.",
               url_for("timesheet.month"))
    return p


def remove_project(actor, employee_id, project_id):
    _not_self(actor, employee_id)
    p = query("SELECT name FROM timesheet_projects WHERE id = ?", (project_id,), one=True)
    with transaction() as conn:
        cur = conn.execute("DELETE FROM project_members WHERE project_id = ? AND employee_id = ?",
                           (project_id, employee_id))
        if not cur.rowcount or p is None:
            raise ValidationError("That person is not on this project.")
        audit.log("PROJECT_MEMBER_REMOVED", "employee", employee_id, prev={"project": p["name"]},
                  remarks=f"Removed by {actor['full_name']}")
    return p


# ---------------------------------------------------------------- reads
def team_of(employee_id):
    return query("""SELECT e.id, e.emp_code, e.full_name, g.name AS designation FROM employees e
                      LEFT JOIN designations g ON g.id = e.designation_id
                     WHERE e.reporting_officer_id = ? AND e.status = 'ACTIVE' ORDER BY e.full_name""", (employee_id,))


def projects_of(employee_id):
    return query("""SELECT p.id, p.name, p.code, p.status FROM project_members m
                      JOIN timesheet_projects p ON p.id = m.project_id
                     WHERE m.employee_id = ? ORDER BY p.name""", (employee_id,))


def active_people(exclude=()):
    return [r for r in query("""SELECT e.id, e.emp_code, e.full_name, g.name AS designation FROM employees e
                                  LEFT JOIN designations g ON g.id = e.designation_id
                                 WHERE e.status = 'ACTIVE' ORDER BY e.full_name""") if r["id"] not in exclude]
