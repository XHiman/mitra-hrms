"""Everything waiting on the signed-in user, for the Pending actions page and the header count."""
from flask import g

from ..core.db import query
from ..core.rbac import has_perm
from ..core.utils import today


def for_user(user):
    """Computed once per request."""
    cached = g.get("_tasks")
    if cached is not None:
        return cached
    from . import compoff, contracts, exits, leave, workflow
    out = {"leave": [], "compoff": [], "renewals_missing": 0, "end_missing": 0, "slip_drafts": 0, "resignations": []}
    if has_perm("leave.approve", user) or has_perm("exit.manage", user):
        out["resignations"] = exits.actionable(user)
    if has_perm("leave.approve", user):
        out["leave"] = [leave.request_detail(r["id"]) for r in workflow.actionable_requests(user)]
    if has_perm("compoff.approve", user):
        out["compoff"] = compoff.actionable_claims(user)
    if has_perm("contracts.manage", user):
        out["renewals_missing"] = sum(1 for r in contracts.overview() if r["s"]["state"] == "missing")
        out["end_missing"] = contracts.missing_end_count()
    if has_perm("payroll.manage", user):
        out["slip_drafts"] = query("SELECT COUNT(*) AS n FROM salary_slips WHERE status = 'DRAFT' AND employee_id != ?",
                                   (user["employee_id"],), one=True)["n"]
    out["month"] = today().strftime("%Y-%m")
    out["total"] = len(out["leave"]) + len(out["compoff"]) + len(out["resignations"]) + (1 if out["renewals_missing"] else 0) + (1 if out["end_missing"] else 0) + \
        (1 if out["slip_drafts"] else 0)
    g._tasks = out
    return out


def count(user):
    if not user or not any(has_perm(p, user) for p in ("leave.approve", "compoff.approve", "contracts.manage",
                                                         "payroll.manage", "exit.manage")):
        return 0
    return for_user(user)["total"]
