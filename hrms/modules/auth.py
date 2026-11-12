"""Authentication: login, logout, password change."""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from ..core import audit
from ..core.db import execute, query, transaction
from ..core.security import hash_password, load_user, password_problems, start_session, verify_password
from ..core.utils import get_int_setting, utcnow_iso

bp = Blueprint("auth", __name__)

# Equalises timing for unknown usernames so they cannot be enumerated.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("dashboards.home")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboards.home"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()[:64]
        password = request.form.get("password") or ""
        row = query("SELECT u.*, e.status AS employee_status FROM users u JOIN employees e ON e.id = u.employee_id "
                    "WHERE u.username = ?", (username,), one=True)
        now = datetime.now(timezone.utc)
        error = "Invalid username or password."
        if row is None:
            verify_password(_DUMMY_HASH, password)
            audit.log("LOGIN_FAILED", "user", None, remarks=f"Unknown username '{username}'")
        elif row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
            error = "This account is temporarily locked after repeated failed logins. Try again later."
            audit.log("LOGIN_BLOCKED", "user", row["id"], remarks="Account locked")
        elif not verify_password(row["password_hash"], password):
            attempts = row["failed_attempts"] + 1
            locked = None
            if attempts >= get_int_setting("max_failed_logins"):
                locked = (now + timedelta(minutes=get_int_setting("lockout_minutes"))).replace(microsecond=0).isoformat()
                attempts = 0
            with transaction():
                execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (attempts, locked, row["id"]))
                audit.log("LOGIN_FAILED", "user", row["id"], remarks="Wrong password" + (" – account locked" if locked else ""))
        elif not row["is_active"] or row["employee_status"] != "ACTIVE":
            error = "This account is deactivated. Please contact the administrator."
            audit.log("LOGIN_BLOCKED", "user", row["id"], remarks="Inactive account")
        else:
            max_age = get_int_setting("password_max_age_days")
            expired = bool(max_age and row["password_changed_at"] and
                           datetime.fromisoformat(row["password_changed_at"]) < now - timedelta(days=max_age))
            with transaction():
                execute("""UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ?,
                           must_change_password = MAX(must_change_password, ?) WHERE id = ?""",
                        (utcnow_iso(), 1 if expired else 0, row["id"]))
                user = load_user(row["id"])
                audit.log("LOGIN_SUCCESS", "user", row["id"], actor=user)
            start_session(user)
            return redirect(_safe_next(request.args.get("next")))
    return render_template("auth/login.html", error=error, expired=request.args.get("expired"))


@bp.route("/logout", methods=["POST"])
def logout():
    if g.user:
        audit.log("LOGOUT", "user", g.user["id"])
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
def change_password():
    errors = []
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        row = query("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],), one=True)
        first_time = bool(g.user["must_change_password"])
        if not verify_password(row["password_hash"], current):
            errors.append("The temporary password you typed is not correct. Type it exactly as printed on your slip."
                          if first_time else "The current password you typed is not correct.")
        if new != confirm:
            errors.append("The two new passwords you typed do not match.")
        if new and new == current:
            errors.append("The new password must be different from the " +
                          ("temporary" if first_time else "current") + " password.")
        problems = password_problems(new, g.user["username"])
        if problems:
            errors.append("The new password needs: " + "; ".join(problems) + ".")
        if errors:
            # Reasons only, never the passwords, so the administrator can help users who are stuck.
            audit.log("PASSWORD_CHANGE_FAILED", "user", g.user["id"], remarks=" ".join(errors))
        if not errors:
            with transaction():
                execute("""UPDATE users SET password_hash = ?, must_change_password = 0, password_changed_at = ?,
                           session_version = session_version + 1 WHERE id = ?""",
                        (hash_password(new), utcnow_iso(), g.user["id"]))
                audit.log("PASSWORD_CHANGED", "user", g.user["id"])
            start_session(load_user(g.user["id"]))  # other sessions are invalidated
            flash("Password changed successfully.", "success")
            return redirect(url_for("dashboards.home"))
    return render_template("auth/change_password.html", errors=errors,
                           min_length=get_int_setting("password_min_length"))
