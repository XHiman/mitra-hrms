"""Audit log viewer, scoped by permission: own / authorised scope / full system."""
from flask import Blueprint, g, render_template, request

from ..core.audit import verify_chain
from ..core.db import query
from ..core.rbac import has_perm, require
from ..services import compoff as compoff_svc
from ..services import exits as exit_svc
from ..services import workflow

bp = Blueprint("audit", __name__, url_prefix="/audit")


def _visible_request_ids(user):
    ids = {r["id"] for r in query("SELECT id FROM leave_requests WHERE employee_id = ?", (user["employee_id"],))}
    if has_perm("audit.view_scope", user):
        scope = workflow.visible_employee_ids(user)
        if scope is None:
            ids |= {r["id"] for r in query("SELECT id FROM leave_requests")}
        else:
            marks = ",".join("?" for _ in scope) or "-1"
            ids |= {r["id"] for r in query(f"SELECT id FROM leave_requests WHERE employee_id IN ({marks})", sorted(scope))}
        ids |= {r["request_id"] for r in query("SELECT DISTINCT request_id FROM request_approvals WHERE acted_by = ?",
                                               (user["id"],))}
        ids |= {r["id"] for r in workflow.actionable_requests(user)}
    return ids


@bp.route("/")
@require("audit.view_own", "audit.view_scope", "audit.view_all")
def index():
    full = has_perm("audit.view_all")
    where, params = [], []
    if not full:
        ids = sorted(str(i) for i in _visible_request_ids(g.user)) or ["-1"]
        claims = sorted(str(i) for i in compoff_svc.visible_claim_ids(g.user)) or ["-1"]
        exits = sorted(str(i) for i in exit_svc.visible_ids(g.user)) or ["-1"]
        where.append(f"((entity = 'leave_request' AND entity_id IN ({','.join('?' for _ in ids)})) "
                     f"OR (entity = 'compoff_claim' AND entity_id IN ({','.join('?' for _ in claims)})) "
                     f"OR (entity = 'resignation' AND entity_id IN ({','.join('?' for _ in exits)})) OR user_id = ?)")
        params += ids + claims + exits + [g.user["id"]]

    f = {k: (request.args.get(k) or "").strip()[:64] for k in ("user", "action", "entity", "entity_id", "from", "to")}
    if f["user"]:
        where.append("username LIKE ?")
        params.append(f"%{f['user']}%")
    if f["action"]:
        where.append("action = ?")
        params.append(f["action"])
    if f["entity"]:
        where.append("entity = ?")
        params.append(f["entity"])
    if f["entity_id"]:
        where.append("entity_id = ?")
        params.append(f["entity_id"])
    if f["from"]:
        where.append("ts >= ?")
        params.append(f["from"])
    if f["to"]:
        where.append("ts < date(?, '+1 day')")
        params.append(f["to"])

    sql = "SELECT * FROM audit_logs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 500"
    rows = query(sql, params)
    actions = [r["action"] for r in query("SELECT DISTINCT action FROM audit_logs ORDER BY action")]
    entities = [r["entity"] for r in query("SELECT DISTINCT entity FROM audit_logs ORDER BY entity")]
    integrity = verify_chain() if full and request.args.get("verify") else None
    return render_template("audit/index.html", rows=rows, f=f, actions=actions, entities=entities, full=full,
                           integrity=integrity)
