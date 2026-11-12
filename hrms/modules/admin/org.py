"""Organisation configuration: departments/teams, designations, reporting hierarchy."""
from flask import flash, redirect, render_template, request, url_for

from ...core import audit
from ...core.db import query, transaction
from ...core.rbac import require
from ...core.utils import ValidationError, clean_text, opt_int
from . import bp


@bp.route("/org")
@require("org.manage")
def org():
    departments = query("""SELECT d.*, p.name AS parent_name,
                                  (SELECT COUNT(*) FROM employees e WHERE e.department_id = d.id AND e.status = 'ACTIVE')
                                      AS headcount
                             FROM departments d LEFT JOIN departments p ON p.id = d.parent_id ORDER BY d.name""")
    designations = query("""SELECT g.*, (SELECT COUNT(*) FROM employees e WHERE e.designation_id = g.id
                                          AND e.status = 'ACTIVE') AS headcount
                              FROM designations g ORDER BY g.level, g.name""")
    people = query("""SELECT e.id, e.full_name, e.reporting_officer_id, g.name AS designation, d.name AS department
                        FROM employees e LEFT JOIN designations g ON g.id = e.designation_id
                        LEFT JOIN departments d ON d.id = e.department_id
                       WHERE e.status = 'ACTIVE' ORDER BY e.full_name""")
    children = {}
    for p in people:
        children.setdefault(p["reporting_officer_id"], []).append(p)
    ids = {p["id"] for p in people}
    roots = [p for p in people if p["reporting_officer_id"] not in ids]

    def tree(node, depth=0, seen=None):
        seen = seen or set()
        if node["id"] in seen:
            return []
        seen.add(node["id"])
        out = [(depth, node)]
        for c in children.get(node["id"], []):
            out += tree(c, depth + 1, seen)
        return out

    hierarchy = [row for r in roots for row in tree(r)]
    return render_template("admin/org.html", departments=departments, designations=designations, hierarchy=hierarchy)


@bp.route("/org/departments", methods=["POST"])
@require("org.manage")
def department_save():
    try:
        dept_id = opt_int(request.form.get("id"), "Department")
        name = clean_text(request.form.get("name"), "Department name", required=True, max_len=120)
        code = clean_text(request.form.get("code"), "Code", max_len=20) or None
        parent = opt_int(request.form.get("parent_id"), "Parent")
        active = 1 if request.form.get("is_active", "1") == "1" else 0
        if parent and parent == dept_id:
            raise ValidationError("A department cannot be its own parent.")
        with transaction() as conn:
            if conn.execute("SELECT 1 FROM departments WHERE name = ? AND id IS NOT ?", (name, dept_id)).fetchone():
                raise ValidationError("A department with that name already exists.")
            if dept_id:
                prev = dict(conn.execute("SELECT * FROM departments WHERE id = ?", (dept_id,)).fetchone() or {})
                if not prev:
                    raise ValidationError("Department not found.")
                conn.execute("UPDATE departments SET name = ?, code = ?, parent_id = ?, is_active = ? WHERE id = ?",
                             (name, code, parent, active, dept_id))
                audit.log("DEPARTMENT_UPDATED", "department", dept_id, prev=prev,
                          new={"name": name, "code": code, "parent_id": parent, "is_active": active})
            else:
                new_id = conn.execute("INSERT INTO departments (name, code, parent_id, is_active) VALUES (?, ?, ?, ?)",
                                      (name, code, parent, active)).lastrowid
                audit.log("DEPARTMENT_CREATED", "department", new_id, new={"name": name, "code": code, "parent_id": parent})
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Department saved.", "success")
    return redirect(url_for("admin.org"))


@bp.route("/org/designations", methods=["POST"])
@require("org.manage")
def designation_save():
    try:
        des_id = opt_int(request.form.get("id"), "Designation")
        name = clean_text(request.form.get("name"), "Designation", required=True, max_len=120)
        level = opt_int(request.form.get("level"), "Level", 0, 99)
        active = 1 if request.form.get("is_active", "1") == "1" else 0
        with transaction() as conn:
            if conn.execute("SELECT 1 FROM designations WHERE name = ? AND id IS NOT ?", (name, des_id)).fetchone():
                raise ValidationError("That designation already exists.")
            if des_id:
                prev = dict(conn.execute("SELECT * FROM designations WHERE id = ?", (des_id,)).fetchone() or {})
                if not prev:
                    raise ValidationError("Designation not found.")
                conn.execute("UPDATE designations SET name = ?, level = ?, is_active = ? WHERE id = ?",
                             (name, level, active, des_id))
                audit.log("DESIGNATION_UPDATED", "designation", des_id, prev=prev,
                          new={"name": name, "level": level, "is_active": active})
            else:
                new_id = conn.execute("INSERT INTO designations (name, level, is_active) VALUES (?, ?, ?)",
                                      (name, level, active)).lastrowid
                audit.log("DESIGNATION_CREATED", "designation", new_id, new={"name": name, "level": level})
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Designation saved.", "success")
    return redirect(url_for("admin.org"))
