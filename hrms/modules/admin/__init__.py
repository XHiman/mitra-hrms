"""Admin / Developer area. Technical administration only – no leave decision rights."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, render_template

from ...core.db import query
from ...core.rbac import require
from ...core.utils import today
from ...services import workflow

bp = Blueprint("admin", __name__, url_prefix="/admin")


def system_alerts():
    alerts = []
    for lt in query("SELECT name FROM leave_types WHERE is_active = 1 AND balance_tracked = 1 "
                    "AND credit_based = 0 AND annual_entitlement IS NULL ORDER BY name"):
        alerts.append(("warning", f"Leave type '{lt['name']}' has no entitlement configured – employees cannot apply for it.",
                       "admin.leave_types"))
    if not query("SELECT 1 FROM approval_workflows WHERE is_default = 1 AND is_active = 1", one=True):
        alerts.append(("critical", "No active default approval workflow is configured.", "admin.workflows"))
    stuck = workflow.unresolvable_pending()
    if stuck:
        alerts.append(("critical", f"{len(stuck)} pending leave request(s) have no eligible approver at their current "
                                   "stage. Check reporting officers / workflow configuration.", "admin.workflows"))
    no_ro = query("""SELECT COUNT(*) AS n FROM employees e JOIN users u ON u.employee_id = e.id
                      JOIN roles r ON r.id = u.role_id
                     WHERE e.status = 'ACTIVE' AND e.reporting_officer_id IS NULL AND r.code != 'CEO'""", one=True)["n"]
    if no_ro:
        alerts.append(("warning", f"{no_ro} active employee(s) (other than the CEO) have no reporting officer mapped.",
                       "admin.users"))
    locked = query("SELECT COUNT(*) AS n FROM users WHERE locked_until > ?",
                   (datetime.now(timezone.utc).replace(microsecond=0).isoformat(),), one=True)["n"]
    if locked:
        alerts.append(("warning", f"{locked} user account(s) are temporarily locked after failed logins.", "admin.users"))
    backups = sorted(Path(current_app.config["BACKUP_DIR"]).glob("hrms-*.db"))
    if not backups or datetime.fromtimestamp(backups[-1].stat().st_mtime) < datetime.now() - timedelta(days=1):
        alerts.append(("warning", "No database backup in the last 24 hours.", "admin.backups"))
    return alerts


@bp.route("/")
@require("dashboard.admin")
def dashboard():
    stats = {
        "total_users": query("SELECT COUNT(*) AS n FROM users", one=True)["n"],
        "active_users": query("SELECT COUNT(*) AS n FROM users WHERE is_active = 1", one=True)["n"],
        "pending": query("SELECT COUNT(*) AS n FROM leave_requests WHERE status = 'PENDING'", one=True)["n"],
    }
    year = today().year
    by_status = {r["status"]: r["n"] for r in query(
        "SELECT status, COUNT(*) AS n FROM leave_requests WHERE substr(from_date, 1, 4) = ? GROUP BY status",
        (str(year),))}
    by_role = query("""SELECT r.name, COUNT(u.id) AS n FROM roles r LEFT JOIN users u ON u.role_id = r.id
                       AND u.is_active = 1 GROUP BY r.id ORDER BY r.id""")
    recent = query("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 12")
    return render_template("admin/dashboard.html", stats=stats, alerts=system_alerts(), by_status=by_status,
                           by_role=by_role, recent=recent, year=year)


from . import leave_config, org, system, users, workflows  # noqa: E402,F401  (register routes)
