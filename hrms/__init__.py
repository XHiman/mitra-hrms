"""MITRA HRMS – pilot (Leave Management & Role-based Dashboards).

Module layout (each concern is isolated so new HRMS modules can be added alongside):
  core/      database, security (authentication/session/CSRF), RBAC, audit trail, utils
  services/  domain logic: leave, approval workflow engine, notifications
  modules/   web blueprints: auth, dashboards, leave, approvals, notifications, audit, admin
"""
import threading
from pathlib import Path

from flask import Flask, g, render_template

from .config import Config, _secret_key
from .core import db, security
from .core.rbac import has_perm
from .core.utils import fmt_date, fmt_days, fmt_size, leave_year_label, to_local
from .services import tasks
from .services.notifications import unread_count


def _daily_jobs_hook(app):
    """Runs the daily reminders on the first signed-in request of each day, so they go out even
    without a scheduled task. A scheduled `flask send-reminders` does the same on a server."""
    state, lock = {"day": None}, threading.Lock()

    def hook():
        if not app.config.get("DAILY_JOBS", not app.config.get("TESTING")) or g.get("user") is None:
            return None
        from .core.utils import today
        day = today()
        if state["day"] == day:
            return None
        with lock:
            if state["day"] == day:
                return None
            state["day"] = day
            try:
                from .services.contracts import send_reminders
                send_reminders(day)
            except Exception:  # reminders must never stop anyone using the portal
                app.logger.exception("Daily renewal reminders failed")
        return None
    return hook


def create_app(overrides=None):
    app = Flask(__name__, instance_path=Config.INSTANCE_DIR, instance_relative_config=False)
    app.config.from_object(Config)
    app.debug = False  # never run the debugger / reloader; this is the production entry point
    app.config["SECRET_KEY"] = _secret_key()
    if overrides:
        app.config.update(overrides)
    for key in ("UPLOAD_DIR", "BACKUP_DIR"):
        Path(app.config[key]).mkdir(parents=True, exist_ok=True)
    if app.config["TRUST_PROXY"]:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.teardown_appcontext(db.close_db)
    app.before_request(security.before_request)
    app.before_request(_daily_jobs_hook(app))
    app.after_request(security.after_request)

    from .modules import (admin, analytics, approvals, audit_views, auth, compoff, dashboards, exits, leave,
                          notifications, payroll, timesheet)
    for bp in (auth.bp, dashboards.bp, leave.bp, compoff.bp, approvals.bp, notifications.bp, audit_views.bp, admin.bp,
               payroll.bp, timesheet.bp, exits.bp, analytics.bp):
        app.register_blueprint(bp)

    from .services.payroll import fmt_money
    app.jinja_env.filters.update(date=fmt_date, days=fmt_days, dt=to_local, year_label=leave_year_label, money=fmt_money, filesize=fmt_size)
    app.jinja_env.globals.update(csrf_token=security.csrf_token, can=has_perm)  # usable inside imported macros

    @app.context_processor
    def inject():
        user = g.get("user")
        return {
            "current_user": user,
            "can": has_perm,
            "csrf_token": security.csrf_token,
            "unread_notifications": unread_count(user["id"]) if user else 0,
            "pending_actions": tasks.count(user) if user else 0,
            "org_name": app.config["ORG_NAME"],
        }

    def error_page(code, title, default_message):
        def handler(err):
            message = getattr(err, "description", None) or default_message
            if code in (403, 404, 500):
                message = default_message
            return render_template("error.html", code=code, title=title, message=message), code
        return handler

    app.register_error_handler(400, error_page(400, "Bad request", "The request could not be processed."))
    app.register_error_handler(403, error_page(403, "Access denied",
                                               "You are not authorised to access this page or perform this action."))
    app.register_error_handler(404, error_page(404, "Not found", "The page or record you requested does not exist."))
    app.register_error_handler(413, error_page(413, "File too large", "The uploaded file exceeds the size limit."))
    app.register_error_handler(500, error_page(500, "Something went wrong",
                                               "An unexpected error occurred. It has been logged."))

    @app.route("/healthz")
    def health():
        """Unauthenticated liveness check for the hosting platform. Reveals nothing beyond up/down."""
        try:
            db.scalar("SELECT 1")
        except Exception:  # noqa: BLE001
            return {"status": "error"}, 503
        return {"status": "ok"}

    from .cli import register_cli
    register_cli(app)
    return app
