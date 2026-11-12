"""System settings and database backups."""
from datetime import date, datetime
from pathlib import Path

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for

from ...core import audit
from ...core.db import query, transaction
from ...core.rbac import require
from ...core.utils import DEFAULT_SETTINGS, ValidationError
from . import bp

_INT_SETTINGS = {"leave_year_start_month": (1, 12), "allow_cancel_approved": (0, 1),
                 "compoff_non_working_only": (0, 1), "timesheet_daily_hours": (1, 12), "timesheet_holidays_expected": (0, 1),"resignation_notice_days": (0, 365),"password_min_length": (8, 64),
                 "password_max_age_days": (0, 3650), "max_failed_logins": (3, 20), "lockout_minutes": (1, 1440),
                 "backup_retention": (1, 365)}


def _validate(key, value):
    value = value.strip()
    if key in _INT_SETTINGS:
        lo, hi = _INT_SETTINGS[key]
        if not value.isdigit() or not lo <= int(value) <= hi:
            raise ValidationError(f"'{key}' must be a whole number between {lo} and {hi}.")
    elif key == "weekend_days":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if any(not p.isdigit() or int(p) > 6 for p in parts) or len(parts) > 3:
            raise ValidationError("Weekly off days must be numbers 0–6 (at most three).")
        value = ",".join(parts)
    elif key == "renewal_reminder_days":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts or any(not p.isdigit() or int(p) > 180 for p in parts) or len(parts) > 8:
            raise ValidationError("Reminder days must be up to eight numbers between 0 and 180, e.g. 30,15,7,1.")
        value = ",".join(str(d) for d in sorted({int(p) for p in parts}, reverse=True))
    elif key == "employment_types":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts or any(len(p) > 40 for p in parts):
            raise ValidationError("Provide at least one employment type (max 40 characters each).")
        value = ",".join(parts)
    elif key == "timesheet_start_date":
        if value:
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError:
                raise ValidationError("The timesheet start date must be a date like 2026-10-01, or blank.")
    elif key == "exit_clearance_items":
        parts = [p.strip() for p in value.split(";") if p.strip()]
        if not parts or len(parts) > 20 or any(len(p) > 120 for p in parts):
            raise ValidationError("Provide 1 to 20 clearance items separated by ; (max 120 characters each).")
        value = ";".join(parts)
    return value


@bp.route("/settings", methods=["GET", "POST"])
@require("settings.manage")
def settings():
    if request.method == "POST":
        try:
            changes = {}
            with transaction() as conn:
                for key in DEFAULT_SETTINGS:
                    if key not in request.form:
                        continue
                    new = _validate(key, request.form.get(key, ""))
                    old = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
                    if old is None or old["value"] != new:
                        conn.execute("INSERT INTO settings (key, value, description) VALUES (?, ?, ?) "
                                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                     (key, new, DEFAULT_SETTINGS[key][1]))
                        changes[key] = (old["value"] if old else None, new)
                if changes:
                    audit.log("SETTINGS_UPDATED", "settings", None, prev={k: v[0] for k, v in changes.items()},
                              new={k: v[1] for k, v in changes.items()})
        except ValidationError as exc:
            flash(str(exc), "error")
        else:
            flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    rows = {r["key"]: r for r in query("SELECT * FROM settings")}
    items = [(k, rows[k]["value"] if k in rows else v, d) for k, (v, d) in DEFAULT_SETTINGS.items()]
    return render_template("admin/settings.html", items=items)


@bp.route("/backups")
@require("settings.manage")
def backups():
    files = []
    for p in sorted(Path(current_app.config["BACKUP_DIR"]).glob("hrms-*.db"), reverse=True):
        st = p.stat()
        files.append({"name": p.name, "size_kb": round(st.st_size / 1024, 1),
                      "when": datetime.fromtimestamp(st.st_mtime).strftime("%d %b %Y, %H:%M")})
    return render_template("admin/backups.html", files=files)


@bp.route("/backups/create", methods=["POST"])
@require("settings.manage")
def backup_create():
    from ...cli import run_backup
    path = run_backup()
    flash(f"Backup created: {path.name}", "success")
    return redirect(url_for("admin.backups"))


@bp.route("/backups/<name>")
@require("settings.manage")
def backup_download(name):
    if not (name.startswith("hrms-") and name.endswith(".db")) or "/" in name or "\\" in name or ".." in name:
        abort(404)
    path = Path(current_app.config["BACKUP_DIR"]) / name
    if not path.is_file():
        abort(404)
    audit.log("BACKUP_DOWNLOADED", "system", None, new={"file": name})
    return send_file(path, as_attachment=True, download_name=name, mimetype="application/octet-stream")
