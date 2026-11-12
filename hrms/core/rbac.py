"""Role-based access control: permission catalogue, default roles, segregation-of-duties rules.

Roles and their permissions live in the database and are editable by the system
administrator; this module only defines the catalogue and the seed defaults.
"""
from functools import wraps

from flask import abort, g

from .db import query

PERMISSIONS = [
    # code, module, description
    ("profile.view_own", "Employee", "View own employee profile"),
    ("leave.apply", "Leave", "Apply for leave and withdraw/cancel own requests"),
    ("leave.view_own", "Leave", "View own leave balance and history"),
    ("leave.approve", "Leave", "Approve / reject leave requests assigned by the workflow"),
    ("compoff.approve", "Leave", "Approve / reject compensatory-off claims for work the user assigned"),
    ("leave.view_team", "Leave", "View leave data of own reporting chain and assigned requests"),
    ("leave.view_all", "Leave", "View leave data of all employees (read only)"),
    ("dashboard.supervisory", "Dashboards", "Access the supervisory dashboard"),
    ("dashboard.admin", "Dashboards", "Access the admin / developer dashboard"),
    ("analytics.view_team", "Dashboards", "Charts and comparisons of own team's time, projects and leave"),
    ("analytics.view_office", "Dashboards", "Charts and comparisons for the whole office and any department"),
    ("users.manage", "Administration", "Create, edit, activate/deactivate users and reset access"),
    ("roles.manage", "Administration", "Manage roles and role permissions"),
    ("org.manage", "Administration", "Manage departments, designations and hierarchy"),
    ("leave_config.manage", "Administration", "Manage leave types, entitlements, rules and holidays"),
    ("workflow.manage", "Administration", "Configure approval workflows"),
    ("settings.manage", "Administration", "Manage system settings and database backups"),
    ("payroll.manage", "Payroll", "Maintain pay structures and generate / issue salary slips"),
    ("contracts.manage", "Employee", "Record contract renewal dates and receive renewal reminders"),
    ("exit.manage", "Employee", "Process resignations: accept, fix the last working day, exit clearance and relieving"),
    ("people.manage", "Employee", "Set a person's reporting officer, the people who report to them, and their projects"),
    ("timesheet.manage", "Timesheets", "Maintain the timesheet project list and view all timesheets and reports"),
    ("audit.view_own", "Audit", "View audit history of own leave requests"),
    ("audit.view_scope", "Audit", "View audit history of leave requests within authorised scope"),
    ("audit.view_all", "Audit", "View the complete system audit log"),
]
PERMISSION_CODES = {p[0] for p in PERMISSIONS}

# Segregation of duties: a role that decides leave may not also administer the system
# that governs those decisions (and vice versa).
_ADMIN_POWERS = {"users.manage", "roles.manage", "workflow.manage", "leave_config.manage"}
SOD_CONFLICTS = [
    ("leave.approve", _ADMIN_POWERS),
    ("compoff.approve", _ADMIN_POWERS),
    ("payroll.manage", _ADMIN_POWERS | {"settings.manage"}),  # system administrators must not handle pay
    ("exit.manage", _ADMIN_POWERS),
]

# Permissions introduced after the first release: when one first appears in an existing
# database it is granted to every role already holding the permission named here.
INHERITED_ON_UPGRADE = {"compoff.approve": "leave.approve", "analytics.view_team": "dashboard.supervisory"}
# ... or to the roles named here.
GRANTED_ON_UPGRADE = {"payroll.manage": ["HR", "ACCOUNTS"], "timesheet.manage": ["HR", "SYSADMIN"],
                      "contracts.manage": ["HR", "SYSADMIN"], "exit.manage": ["HR"],
                      "analytics.view_office": ["CEO", "JOINT_CEO", "HR"],
                      "people.manage": ["HR", "SYSADMIN"]}

_EMPLOYEE = ["profile.view_own", "leave.apply", "leave.view_own", "audit.view_own"]
_SUPERVISOR = _EMPLOYEE + ["leave.approve", "compoff.approve", "leave.view_team", "dashboard.supervisory",
                           "audit.view_scope", "analytics.view_team"]

DEFAULT_ROLES = [
    ("EMPLOYEE", "Employee", "Employee self-service", _EMPLOYEE),
    ("CEO", "Chief Executive Officer", "Supervisory authority", _SUPERVISOR + ["leave.view_all", "analytics.view_office"]),
    ("JOINT_CEO", "Joint CEO", "Supervisory authority", _SUPERVISOR + ["analytics.view_office"]),
    ("JOINT_SECRETARY", "Joint Secretary", "Supervisory authority", _SUPERVISOR),
    ("HR", "Human Resources", "Supervisory authority (HR)", _SUPERVISOR + ["leave.view_all", "payroll.manage", "timesheet.manage",
                                                                 "contracts.manage", "exit.manage", "analytics.view_office",
                                                                 "people.manage"]),
    ("ACCOUNTS", "Accounts Officer", "Employee self-service plus salary slips", _EMPLOYEE + ["payroll.manage"]),
    ("REPORTING_MANAGER", "Reporting Manager", "Supervisory authority for own team (reporting chain only)",
     _SUPERVISOR),
    ("SYSADMIN", "System Administrator / Developer",
     "Technical administration. Holds no leave decision rights.",
     _EMPLOYEE + ["dashboard.admin", "users.manage", "roles.manage", "org.manage",
                  "leave_config.manage", "workflow.manage", "settings.manage",
                  "audit.view_all", "leave.view_all", "timesheet.manage", "contracts.manage", "people.manage"]),
]


def sod_violations(perm_codes):
    perm_codes = set(perm_codes)
    problems = []
    for perm, conflicts in SOD_CONFLICTS:
        if perm in perm_codes and perm_codes & conflicts:
            problems.append(f"'{perm}' cannot be combined with {', '.join(sorted(perm_codes & conflicts))}")
    return problems


def permissions_for_role(role_id):
    return {r["permission_code"] for r in
            query("SELECT permission_code FROM role_permissions WHERE role_id = ?", (role_id,))}


def has_perm(code, user=None):
    user = user if user is not None else getattr(g, "user", None)
    if not user:
        return False
    return code in user["permissions"]


def has_any(*codes):
    return any(has_perm(c) for c in codes)


def require(*codes):
    """Backend authorisation guard: user must hold at least one of the permissions."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if getattr(g, "user", None) is None:
                abort(401)
            if not has_any(*codes):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator
