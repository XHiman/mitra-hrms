"""User management (employee master + accounts) and role/permission management."""
import re

from flask import abort, flash, g, redirect, render_template, request, url_for

from ...core import audit
from ...core.db import query, transaction
from ...core.rbac import PERMISSIONS, PERMISSION_CODES, require, sod_violations
from ...core.security import hash_password, temporary_password
from ...core.utils import (ValidationError, clean_text, employment_types, leave_year_of, opt_float, parse_date,
                           today, utcnow_iso)
from ...services import contracts, cycles
from ...services import leave as leave_svc
from . import bp

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")


def _lookups():
    return {
        "departments": query("SELECT * FROM departments WHERE is_active = 1 ORDER BY name"),
        "designations": query("SELECT * FROM designations WHERE is_active = 1 ORDER BY level, name"),
        "roles": query("SELECT * FROM roles ORDER BY id"),
        "officers": query("""SELECT e.id, e.full_name, e.emp_code, g.name AS designation FROM employees e
                             LEFT JOIN designations g ON g.id = e.designation_id
                             WHERE e.status = 'ACTIVE' ORDER BY e.full_name"""),
        "employment_types": employment_types(),
    }


def _user_row(user_id):
    return query("""SELECT u.*, e.emp_code, e.full_name, e.email, e.phone, e.designation_id, e.department_id,
                           e.reporting_officer_id, e.joining_date, e.date_of_birth, e.employment_type, e.status AS employee_status,
                           r.code AS role_code, r.name AS role_name
                      FROM users u JOIN employees e ON e.id = u.employee_id JOIN roles r ON r.id = u.role_id
                     WHERE u.id = ?""", (user_id,), one=True)


def _snapshot(user_id):
    row = _user_row(user_id)
    keys = ("username", "role_code", "is_active", "emp_code", "full_name", "email", "phone", "designation_id",
            "department_id", "reporting_officer_id", "joining_date", "date_of_birth", "employment_type", "employee_status")
    return {k: row[k] for k in keys}


def _read_form(form, employee_id=None):
    data = {
        "username": clean_text(form.get("username"), "Username", required=True, max_len=40),
        "emp_code": clean_text(form.get("emp_code"), "Employee ID", required=True, max_len=20),
        "full_name": clean_text(form.get("full_name"), "Name", required=True, max_len=120),
        "email": clean_text(form.get("email"), "Email", max_len=120) or None,
        "phone": clean_text(form.get("phone"), "Phone", max_len=20) or None,
        "employment_type": leave_svc.valid_employment_type(form.get("employment_type")),
    }
    if not USERNAME_RE.match(data["username"]):
        raise ValidationError("Username may contain letters, digits, '.', '_' or '-' (3–40 characters).")
    if data["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", data["email"]):
        raise ValidationError("Email address is not valid.")
    data["joining_date"] = parse_date(form.get("joining_date"), "Joining date").isoformat() \
        if form.get("joining_date") else None
    data["date_of_birth"] = parse_date(form.get("date_of_birth"), "Date of birth").isoformat() \
        if form.get("date_of_birth") else None

    def ref(field, table, label):
        value = form.get(field) or ""
        if not value:
            return None
        if not value.isdigit() or not query(f"SELECT 1 FROM {table} WHERE id = ?", (int(value),), one=True):
            raise ValidationError(f"Select a valid {label}.")
        return int(value)

    data["department_id"] = ref("department_id", "departments", "department")
    data["designation_id"] = ref("designation_id", "designations", "designation")
    data["role_id"] = ref("role_id", "roles", "role")
    if data["role_id"] is None:
        raise ValidationError("Select a role.")
    data["reporting_officer_id"] = ref("reporting_officer_id", "employees", "reporting officer")

    if employee_id:
        from ...services.people import check_reporting
        check_reporting(employee_id, data["reporting_officer_id"])
    return data


def _active_admins_after(change_user_id=None, new_role_id=None, deactivate=False):
    """Count active users holding roles.manage if the given change were applied."""
    n = 0
    for u in query("SELECT id, role_id FROM users WHERE is_active = 1"):
        role_id = u["role_id"]
        if u["id"] == change_user_id:
            if deactivate:
                continue
            role_id = new_role_id or role_id
        if query("SELECT 1 FROM role_permissions WHERE role_id = ? AND permission_code = 'roles.manage'",
                 (role_id,), one=True):
            n += 1
    return n


@bp.route("/users")
@require("users.manage")
def users():
    status = request.args.get("status", "")
    q = (request.args.get("q") or "").strip()[:60]
    where, params = ["1=1"], []
    if status == "active":
        where.append("u.is_active = 1")
    elif status == "inactive":
        where.append("u.is_active = 0")
    if q:
        where.append("(e.full_name LIKE ? OR u.username LIKE ? OR e.emp_code LIKE ?)")
        params += [f"%{q}%"] * 3
    rows = query(f"""SELECT u.id, u.username, u.is_active, u.last_login_at, u.locked_until, e.emp_code, e.full_name,
                            e.employment_type, r.name AS role_name, d.name AS department, g.name AS designation,
                            ro.full_name AS reporting_officer
                       FROM users u JOIN employees e ON e.id = u.employee_id JOIN roles r ON r.id = u.role_id
                       LEFT JOIN departments d ON d.id = e.department_id
                       LEFT JOIN designations g ON g.id = e.designation_id
                       LEFT JOIN employees ro ON ro.id = e.reporting_officer_id
                      WHERE {' AND '.join(where)} ORDER BY e.full_name""", params)
    return render_template("admin/users.html", rows=rows, status=status, q=q)


@bp.route("/users/new", methods=["GET", "POST"])
@require("users.manage")
def user_new():
    error = None
    if request.method == "POST":
        try:
            data = _read_form(request.form)
            password = temporary_password()
            with transaction() as conn:
                if conn.execute("SELECT 1 FROM users WHERE username = ?", (data["username"],)).fetchone():
                    raise ValidationError("That username is already taken.")
                if conn.execute("SELECT 1 FROM employees WHERE emp_code = ?", (data["emp_code"],)).fetchone():
                    raise ValidationError("That Employee ID already exists.")
                now = utcnow_iso()
                emp_id = conn.execute(
                    """INSERT INTO employees (emp_code, full_name, email, phone, designation_id, department_id,
                                              reporting_officer_id, joining_date, date_of_birth, employment_type,
                                              status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)""",
                    (data["emp_code"], data["full_name"], data["email"], data["phone"], data["designation_id"],
                     data["department_id"], data["reporting_officer_id"], data["joining_date"],
                     data["date_of_birth"], data["employment_type"], now, now)).lastrowid
                uid = conn.execute(
                    """INSERT INTO users (employee_id, username, password_hash, role_id, must_change_password,
                                          password_changed_at, created_at) VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (emp_id, data["username"], hash_password(password), data["role_id"], now, now)).lastrowid
                audit.log("USER_CREATED", "user", uid, new=_snapshot(uid))
        except ValidationError as exc:
            error = str(exc)
        else:
            return render_template("admin/credentials.html", username=data["username"], password=password,
                                   heading="User created", user_id=uid)
    return render_template("admin/user_form.html", user=None, form=request.form, error=error, **_lookups())


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@require("users.manage")
def user_edit(user_id):
    user = _user_row(user_id)
    if user is None:
        abort(404)
    error = None
    if request.method == "POST":
        try:
            data = _read_form(request.form, employee_id=user["employee_id"])
            if user_id == g.user["id"] and data["role_id"] != user["role_id"]:
                raise ValidationError("You cannot change your own role.")
            if _active_admins_after(user_id, data["role_id"]) == 0:
                raise ValidationError("At least one active user must keep role-management rights.")
            before = _snapshot(user_id)
            with transaction() as conn:
                if conn.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (data["username"], user_id)).fetchone():
                    raise ValidationError("That username is already taken.")
                if conn.execute("SELECT 1 FROM employees WHERE emp_code = ? AND id != ?",
                                (data["emp_code"], user["employee_id"])).fetchone():
                    raise ValidationError("That Employee ID already exists.")
                conn.execute("""UPDATE employees SET emp_code = ?, full_name = ?, email = ?, phone = ?, designation_id = ?,
                                department_id = ?, reporting_officer_id = ?, joining_date = ?, date_of_birth = ?,
                                employment_type = ?, updated_at = ? WHERE id = ?""",
                             (data["emp_code"], data["full_name"], data["email"], data["phone"], data["designation_id"],
                              data["department_id"], data["reporting_officer_id"], data["joining_date"],
                              data["date_of_birth"], data["employment_type"], utcnow_iso(), user["employee_id"]))
                role_changed = data["role_id"] != user["role_id"]
                conn.execute("UPDATE users SET username = ?, role_id = ?, session_version = session_version + ? WHERE id = ?",
                             (data["username"], data["role_id"], 1 if role_changed else 0, user_id))
                after = _snapshot(user_id)
                changed = {k: v for k, v in after.items() if before[k] != v}
                if changed:
                    audit.log("ROLE_ASSIGNED" if role_changed else "USER_UPDATED", "user", user_id,
                              prev={k: before[k] for k in changed}, new=changed)
        except ValidationError as exc:
            error = str(exc)
        else:
            if data["joining_date"] != user["joining_date"]:
                leave_svc.rebuild_balances([user["employee_id"]], remarks=f"Joining date changed for {data['emp_code']}")
            flash("User details saved.", "success")
            return redirect(url_for("admin.user_edit", user_id=user_id))
    cycle = leave_svc.current_cycle(user["employee_id"])
    return render_template("admin/user_form.html", user=user,
                           form=request.form if request.method == "POST" else dict(user),
                           error=error, balances=leave_svc.balances_for(user["employee_id"], cycle), cycle=cycle,
                           contract=cycles.contract_summary(user["employee_id"], today()),
                           renewals=query("SELECT * FROM contract_renewals WHERE employee_id = ? ORDER BY renewal_date",
                                          (user["employee_id"],)),
                           **_lookups())


@bp.route("/users/<int:user_id>/renewals", methods=["POST"])
@require("users.manage")
def user_renewal_add(user_id):
    user = _user_row(user_id)
    if user is None:
        abort(404)
    try:
        contracts.add_renewal(user["employee_id"], request.form.get("renewal_date"), request.form.get("note"))
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Renewal date added. Leave balances follow the new contract term.", "success")
    return redirect(url_for("admin.user_edit", user_id=user_id) + "#contract")


@bp.route("/users/<int:user_id>/renewals/<int:renewal_id>/delete", methods=["POST"])
@require("users.manage")
def user_renewal_delete(user_id, renewal_id):
    user = _user_row(user_id)
    if user is None:
        abort(404)
    try:
        contracts.remove_renewal(user["employee_id"], renewal_id)
    except ValidationError:
        abort(404)
    flash("Renewal date removed.", "success")
    return redirect(url_for("admin.user_edit", user_id=user_id) + "#contract")


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@require("users.manage")
def user_toggle(user_id):
    user = _user_row(user_id)
    if user is None:
        abort(404)
    activate = not user["is_active"]
    if user_id == g.user["id"]:
        flash("You cannot deactivate your own account.", "error")
    elif not activate and _active_admins_after(user_id, deactivate=True) == 0:
        flash("At least one active user must keep role-management rights.", "error")
    else:
        with transaction() as conn:
            conn.execute("UPDATE users SET is_active = ?, session_version = session_version + 1 WHERE id = ?",
                         (1 if activate else 0, user_id))
            conn.execute("UPDATE employees SET status = ?, updated_at = ? WHERE id = ?",
                         ("ACTIVE" if activate else "INACTIVE", utcnow_iso(), user["employee_id"]))
            audit.log("USER_ACTIVATED" if activate else "USER_DEACTIVATED", "user", user_id,
                      prev={"is_active": user["is_active"]}, new={"is_active": 1 if activate else 0})
        flash(f"User {'activated' if activate else 'deactivated'}.", "success")
    return redirect(url_for("admin.user_edit", user_id=user_id))


@bp.route("/users/<int:user_id>/reset", methods=["POST"])
@require("users.manage")
def user_reset(user_id):
    user = _user_row(user_id)
    if user is None:
        abort(404)
    if user_id == g.user["id"]:
        flash("Use 'Change password' to change your own password.", "error")
        return redirect(url_for("admin.user_edit", user_id=user_id))
    password = temporary_password()
    with transaction() as conn:
        conn.execute("""UPDATE users SET password_hash = ?, must_change_password = 1, failed_attempts = 0,
                        locked_until = NULL, password_changed_at = ?, session_version = session_version + 1
                        WHERE id = ?""", (hash_password(password), utcnow_iso(), user_id))
        audit.log("ACCESS_RESET", "user", user_id, remarks="Temporary password issued; sessions revoked; lock cleared")
    return render_template("admin/credentials.html", username=user["username"], password=password,
                           heading="Access reset", user_id=user_id)


@bp.route("/users/<int:user_id>/balance", methods=["POST"])
@require("users.manage")
def user_balance(user_id):
    """Entitlement override for one employee (e.g. pro-rata for mid-year joiners)."""
    user = _user_row(user_id)
    if user is None:
        abort(404)
    try:
        lt = leave_svc.leave_type(request.form.get("leave_type_id", "0") or 0)
        if lt is None or not lt["balance_tracked"]:
            raise ValidationError("Select a valid leave type.")
        if lt["credit_based"]:
            raise ValidationError(f"{lt['name']} balance changes only through approved comp-off claims.")
        value = opt_float(request.form.get("entitlement"), "Entitlement")
        remarks = clean_text(request.form.get("remarks"), "Remarks", required=True, max_len=500)
        cycle = leave_svc.current_cycle(user["employee_id"])
        year = cycle.key
        with transaction() as conn:
            bal = leave_svc.ensure_balance(conn, user["employee_id"], lt, year)
            new_ent = lt["annual_entitlement"] if value is None else value
            if new_ent is not None and new_ent < bal["used"] + bal["pending"]:
                raise ValidationError("Entitlement cannot be lower than leave already used or pending.")
            conn.execute("UPDATE leave_balances SET entitlement = ?, is_override = ? WHERE id = ?",
                         (new_ent, 0 if value is None else 1, bal["id"]))
            audit.log("BALANCE_ADJUSTED", "leave_balance", bal["id"],
                      prev={"employee": user["emp_code"], "leave_type": lt["code"], "year": year,
                            "entitlement": bal["entitlement"], "override": bal["is_override"]},
                      new={"entitlement": new_ent, "override": 0 if value is None else 1}, remarks=remarks)
    except ValidationError as exc:
        flash(str(exc), "error")
    else:
        flash("Entitlement updated.", "success")
    return redirect(url_for("admin.user_edit", user_id=user_id) + "#balances")


# ---------------------------------------------------------------- roles & permissions
@bp.route("/roles")
@require("roles.manage")
def roles():
    role_rows = query("SELECT r.*, (SELECT COUNT(*) FROM users u WHERE u.role_id = r.id) AS user_count "
                      "FROM roles r ORDER BY r.id")
    grants = {(r["role_id"], r["permission_code"]) for r in query("SELECT * FROM role_permissions")}
    return render_template("admin/roles.html", roles=role_rows, permissions=PERMISSIONS, grants=grants)


@bp.route("/roles/<int:role_id>", methods=["GET", "POST"])
@bp.route("/roles/new", methods=["GET", "POST"], defaults={"role_id": None})
@require("roles.manage")
def role_edit(role_id):
    role = query("SELECT * FROM roles WHERE id = ?", (role_id,), one=True) if role_id else None
    if role_id and role is None:
        abort(404)
    current = {r["permission_code"] for r in query("SELECT permission_code FROM role_permissions WHERE role_id = ?",
                                                     (role_id,))} if role else set()
    error = None
    if request.method == "POST":
        selected = set(request.form.getlist("perm")) & PERMISSION_CODES
        try:
            name = clean_text(request.form.get("name"), "Role name", required=True, max_len=80)
            description = clean_text(request.form.get("description"), "Description", max_len=200)
            code = role["code"] if role else clean_text(request.form.get("code"), "Role code", required=True, max_len=30).upper()
            if not re.match(r"^[A-Z][A-Z0-9_]{1,29}$", code):
                raise ValidationError("Role code must be letters, digits or underscores.")
            problems = sod_violations(selected)
            if problems:
                raise ValidationError("Segregation of duties: " + "; ".join(problems) + ".")
            with transaction() as conn:
                if role is None:
                    if conn.execute("SELECT 1 FROM roles WHERE code = ?", (code,)).fetchone():
                        raise ValidationError("A role with that code already exists.")
                    rid = conn.execute("INSERT INTO roles (code, name, description) VALUES (?, ?, ?)",
                                       (code, name, description)).lastrowid
                else:
                    rid = role_id
                    conn.execute("UPDATE roles SET name = ?, description = ? WHERE id = ?", (name, description, rid))
                conn.execute("DELETE FROM role_permissions WHERE role_id = ?", (rid,))
                for p in sorted(selected):
                    conn.execute("INSERT INTO role_permissions (role_id, permission_code) VALUES (?, ?)", (rid, p))
                if not conn.execute("""SELECT 1 FROM users u JOIN role_permissions rp ON rp.role_id = u.role_id
                                       WHERE u.is_active = 1 AND rp.permission_code = 'roles.manage'""").fetchone():
                    raise ValidationError("At least one active user must keep role-management rights.")
                conn.execute("UPDATE users SET session_version = session_version + 1 WHERE role_id = ?", (rid,))
                audit.log("ROLE_CREATED" if role is None else "ROLE_PERMISSIONS_CHANGED", "role", rid,
                          prev=None if role is None else {"name": role["name"], "permissions": sorted(current)},
                          new={"name": name, "permissions": sorted(selected)})
        except ValidationError as exc:
            error = str(exc)
            current = selected
        else:
            flash("Role saved. Users holding this role must sign in again.", "success")
            return redirect(url_for("admin.roles"))
    return render_template("admin/role_form.html", role=role, permissions=PERMISSIONS, current=current, error=error,
                           form=request.form)
