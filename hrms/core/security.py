"""Authentication plumbing: password policy, session lifecycle, CSRF, security headers."""
import hmac
import re
import secrets
import time

from flask import Response, abort, current_app, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import maintenance
from .db import query
from .rbac import permissions_for_role
from .utils import get_int_setting

PUBLIC_ENDPOINTS = {"auth.login", "static", "auth.logout", "health"}
PASSWORD_CHANGE_ENDPOINTS = {"auth.change_password", "auth.logout", "static"}


# ---------------------------------------------------------------- passwords
def hash_password(raw):
    return generate_password_hash(raw, method="scrypt")


def verify_password(pw_hash, raw):
    return check_password_hash(pw_hash, raw)


def password_problems(raw, username=""):
    min_len = get_int_setting("password_min_length")
    problems = []
    if len(raw) < min_len:
        problems.append(f"at least {min_len} characters")
    if len(raw) > 128:
        problems.append("at most 128 characters")
    if not re.search(r"[A-Z]", raw):
        problems.append("an uppercase letter")
    if not re.search(r"[a-z]", raw):
        problems.append("a lowercase letter")
    if not re.search(r"\d", raw):
        problems.append("a digit")
    if not re.search(r"[^A-Za-z0-9]", raw):
        problems.append("a special character")
    if username and username.lower() in raw.lower():
        problems.append("must not contain the username")
    return problems


def temporary_password():
    """Policy-compliant one-time password shown once to the administrator."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    core = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"{core}#{secrets.randbelow(90) + 10}Aa"


# ---------------------------------------------------------------- user loading
def load_user(user_id):
    row = query(
        """SELECT u.id, u.username, u.employee_id, u.role_id, u.is_active, u.must_change_password,
                  u.session_version, u.password_changed_at,
                  r.code AS role_code, r.name AS role_name,
                  e.full_name, e.status AS employee_status
             FROM users u JOIN roles r ON r.id = u.role_id JOIN employees e ON e.id = u.employee_id
            WHERE u.id = ?""",
        (user_id,), one=True)
    if row is None:
        return None
    user = dict(row)
    user["permissions"] = permissions_for_role(user["role_id"])
    return user


def start_session(user):
    session.clear()
    now = int(time.time())
    session["uid"] = user["id"]
    session["sv"] = user["session_version"]
    session["created"] = now
    session["last"] = now
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


# ---------------------------------------------------------------- request hooks
def before_request():
    g.user = None
    if maintenance.active() and request.endpoint != "health":
        return Response("The system is being restored from a backup. Please try again in a minute.", 503,
                        headers={"Retry-After": "60", "Content-Type": "text/plain; charset=utf-8"})
    if request.endpoint == "admin.restore_upload":  # backup archives are larger than the global request limit
        request.max_content_length = current_app.config["RESTORE_MAX_UPLOAD_BYTES"]
    uid = session.get("uid")
    if uid:
        now = int(time.time())
        idle = current_app.config["SESSION_IDLE_MINUTES"] * 60
        absolute = current_app.config["SESSION_ABSOLUTE_HOURS"] * 3600
        user = load_user(uid)
        expired = (now - session.get("last", 0) > idle) or (now - session.get("created", 0) > absolute)
        valid = (user is not None and user["is_active"] and user["employee_status"] == "ACTIVE"
                 and user["session_version"] == session.get("sv"))
        if expired or not valid:
            session.clear()
            if request.endpoint not in PUBLIC_ENDPOINTS:
                return redirect(url_for("auth.login", expired=1))
        else:
            session["last"] = now
            g.user = user

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, description="Invalid or missing security token. Please reload the page and try again.")

    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if g.user is None:
        return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
    if g.user["must_change_password"] and request.endpoint not in PASSWORD_CHANGE_ENDPOINTS:
        return redirect(url_for("auth.change_password"))
    return None


def after_request(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    if g.get("user") is not None:
        resp.headers["Cache-Control"] = "no-store"
    return resp
