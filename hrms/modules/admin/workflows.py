"""Approval workflow configuration (the approval hierarchy)."""
from flask import abort, flash, redirect, render_template, request, url_for

from ...core import audit
from ...core.db import query, transaction
from ...core.rbac import require
from ...core.utils import ValidationError, clean_text, opt_float
from ...services.workflow import APPROVER_TYPES, validate_workflow, workflow_stages
from . import bp

MAX_STAGES = 6


@bp.route("/workflows")
@require("workflow.manage")
def workflows():
    rows = query("""SELECT w.*, (SELECT COUNT(*) FROM leave_types lt WHERE lt.workflow_id = w.id) AS type_count
                      FROM approval_workflows w ORDER BY w.is_default DESC, w.name""")
    stages = {w["id"]: query("""SELECT s.*, r.name AS role_name, f.name AS fallback_name, u.username
                                  FROM workflow_stages s LEFT JOIN roles r ON r.id = s.approver_role_id
                                  LEFT JOIN roles f ON f.id = s.fallback_role_id
                                  LEFT JOIN users u ON u.id = s.approver_user_id
                                 WHERE s.workflow_id = ? ORDER BY s.seq""", (w["id"],)) for w in rows}
    return render_template("admin/workflows.html", rows=rows, stages=stages, approver_types=APPROVER_TYPES)


def _approver_choices():
    roles = query("""SELECT r.* FROM roles r WHERE EXISTS (SELECT 1 FROM role_permissions rp WHERE rp.role_id = r.id
                     AND rp.permission_code = 'leave.approve') ORDER BY r.id""")
    users = query("""SELECT u.id, u.username, e.full_name FROM users u JOIN employees e ON e.id = u.employee_id
                      JOIN role_permissions rp ON rp.role_id = u.role_id AND rp.permission_code = 'leave.approve'
                     WHERE u.is_active = 1 ORDER BY e.full_name""")
    return roles, users


def _read_stages(form, role_ids, user_ids):
    stages = []
    for i in range(MAX_STAGES):
        name = clean_text(form.get(f"stage_name_{i}"), "Stage name", max_len=80)
        if not name or form.get(f"stage_remove_{i}"):
            continue
        a_type = form.get(f"stage_type_{i}")
        if a_type not in APPROVER_TYPES:
            raise ValidationError(f"Stage '{name}': choose who approves.")

        def pick(field, allowed):
            v = form.get(field) or ""
            if not v:
                return None
            if not v.isdigit() or int(v) not in allowed:
                raise ValidationError(f"Stage '{name}': invalid selection.")
            return int(v)

        stages.append({
            "name": name, "approver_type": a_type,
            "approver_role_id": pick(f"stage_role_{i}", role_ids) if a_type == "ROLE" else None,
            "approver_user_id": pick(f"stage_user_{i}", user_ids) if a_type == "USER" else None,
            "fallback_role_id": pick(f"stage_fallback_{i}", role_ids),
            "min_days": opt_float(form.get(f"stage_min_days_{i}"), "Day threshold"),
            "order": opt_float(form.get(f"stage_order_{i}"), "Order") or (i + 1),
        })
    stages.sort(key=lambda s: s["order"])
    validate_workflow(stages)
    return stages


@bp.route("/workflows/<int:workflow_id>", methods=["GET", "POST"])
@bp.route("/workflows/new", methods=["GET", "POST"], defaults={"workflow_id": None})
@require("workflow.manage")
def workflow_edit(workflow_id):
    wf = query("SELECT * FROM approval_workflows WHERE id = ?", (workflow_id,), one=True) if workflow_id else None
    if workflow_id and wf is None:
        abort(404)
    roles, users = _approver_choices()
    error = None
    if request.method == "POST":
        try:
            name = clean_text(request.form.get("name"), "Workflow name", required=True, max_len=80)
            description = clean_text(request.form.get("description"), "Description", max_len=300)
            is_default = 1 if request.form.get("is_default") else 0
            is_active = 1 if request.form.get("is_active") else 0
            if is_default and not is_active:
                raise ValidationError("The default workflow must be active.")
            stages = _read_stages(request.form, {r["id"] for r in roles}, {u["id"] for u in users})
            with transaction() as conn:
                if conn.execute("SELECT 1 FROM approval_workflows WHERE name = ? AND id IS NOT ?",
                                (name, workflow_id)).fetchone():
                    raise ValidationError("A workflow with that name already exists.")
                prev = None
                if wf:
                    prev = {"name": wf["name"], "is_default": wf["is_default"], "is_active": wf["is_active"],
                            "stages": [dict(s) for s in workflow_stages(workflow_id)]}
                    if wf["is_default"] and not is_default:
                        raise ValidationError("Make another workflow the default instead of un-setting this one.")
                    conn.execute("UPDATE approval_workflows SET name = ?, description = ?, is_default = ?, is_active = ? "
                                 "WHERE id = ?", (name, description, is_default, is_active, workflow_id))
                    wid = workflow_id
                else:
                    wid = conn.execute("INSERT INTO approval_workflows (name, description, is_default, is_active) "
                                       "VALUES (?, ?, ?, ?)", (name, description, is_default, is_active)).lastrowid
                if is_default:
                    conn.execute("UPDATE approval_workflows SET is_default = 0 WHERE id != ?", (wid,))
                conn.execute("DELETE FROM workflow_stages WHERE workflow_id = ?", (wid,))
                for seq, s in enumerate(stages, start=1):
                    conn.execute("""INSERT INTO workflow_stages (workflow_id, seq, name, approver_type, approver_role_id,
                                    approver_user_id, fallback_role_id, min_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                 (wid, seq, s["name"], s["approver_type"], s["approver_role_id"],
                                  s["approver_user_id"], s["fallback_role_id"], s["min_days"]))
                audit.log("WORKFLOW_CREATED" if wf is None else "WORKFLOW_UPDATED", "workflow", wid, prev=prev,
                          new={"name": name, "is_default": is_default, "is_active": is_active,
                               "stages": [{k: v for k, v in s.items() if k != "order"} for s in stages]})
        except ValidationError as exc:
            error = str(exc)
        else:
            flash("Workflow saved. Requests already submitted keep the stages they were submitted with.", "success")
            return redirect(url_for("admin.workflows"))

    existing = [dict(s) for s in workflow_stages(workflow_id)] if wf else []
    slots = existing + [None] * (MAX_STAGES - len(existing))
    return render_template("admin/workflow_form.html", wf=wf, slots=slots, roles=roles, users=users,
                           approver_types=APPROVER_TYPES, error=error, form=request.form)
