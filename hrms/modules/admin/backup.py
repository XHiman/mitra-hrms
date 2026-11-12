"""System Administration -> Backup & Restore: full-system backup download, history, validated restore."""
from flask import abort, current_app, flash, g, redirect, render_template, request, send_file, session, url_for

from ...core import audit
from ...core.rbac import require
from ...services import backup as svc
from . import bp


@bp.route("/backup-restore")
@require("settings.manage")
def backup_restore():
    svc.purge_pending()
    return render_template("admin/backup_restore.html", info=svc.system_info(), history=svc.list_backups()[:30],
                           max_mb=current_app.config["RESTORE_MAX_UPLOAD_BYTES"] // (1 << 20))


@bp.route("/backup-restore/download", methods=["POST"])
@require("settings.manage")
def backup_full_download():
    """Build a fresh full backup and send it to the browser (a copy is also kept on the server for now)."""
    try:
        info = svc.create_backup("full", created_by=g.user["username"])
    except Exception as exc:  # noqa: BLE001
        audit.log("BACKUP_FULL_FAILED", "system", None, new={"error": str(exc)[:300]})
        flash(f"The backup could not be created: {exc}", "error")
        return redirect(url_for("admin.backup_restore"))
    audit.log("BACKUP_FULL_CREATED", "system", None,
              new={"file": info["name"], "size": info["size"], "files": info["files"],
                   "schema_version": info["manifest"]["database_schema_version"]},
              remarks="Full system backup downloaded")
    return send_file(info["path"], mimetype="application/zip", as_attachment=True, download_name=info["name"])


@bp.route("/backup-restore/files/<name>")
@require("settings.manage")
def backup_file(name):
    path = svc.retained_path(name)
    if path is None:
        abort(404)
    audit.log("BACKUP_DOWNLOADED", "system", None, new={"file": name})
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=name)


@bp.route("/backup-restore/restore", methods=["POST"])
@require("settings.manage")
def restore_upload():
    """Step 1: receive the ZIP and validate it completely. Nothing is changed yet."""
    try:
        token, _summary = svc.stage_upload(request.files.get("backup"), g.user)
    except svc.BackupError as exc:
        audit.log("BACKUP_REJECTED", "system", None, new={"reason": str(exc)[:300]},
                  remarks="Uploaded backup failed validation")
        flash(f"This backup cannot be used: {exc}", "error")
        return redirect(url_for("admin.backup_restore") + "#restore")
    return redirect(url_for("admin.restore_review", token=token))


@bp.route("/backup-restore/restore/<token>")
@require("settings.manage")
def restore_review(token):
    """Step 2: show what will be restored and ask for explicit confirmation."""
    try:
        _zip, summary = svc.load_pending(token, g.user)
    except svc.BackupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.backup_restore") + "#restore")
    return render_template("admin/restore_confirm.html", token=token, s=summary, info=svc.system_info())


@bp.route("/backup-restore/restore/<token>/confirm", methods=["POST"])
@require("settings.manage")
def restore_confirm(token):
    """Step 3: both confirmations given -> restore."""
    try:
        zip_path, _summary = svc.load_pending(token, g.user)
    except svc.BackupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.backup_restore") + "#restore")
    if request.form.get("understand") != "1" or (request.form.get("confirm_text") or "").strip() != "RESTORE":
        flash("Tick the box and type RESTORE exactly to confirm. Nothing was changed.", "error")
        return redirect(url_for("admin.restore_review", token=token))
    actor = dict(g.user)
    try:
        report = svc.restore_archive(zip_path, actor)
    except svc.BackupError as exc:
        flash(f"Restore failed. {exc}", "error")
        return redirect(url_for("admin.backup_restore") + "#restore")
    svc.discard_pending(token)
    session.clear()  # every session was ended by the restore; sign in with the restored accounts
    flash("The backup was restored successfully (" + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in
          report["counts"].items() if k in ("users", "employees", "leave_requests")) +
          f", {report['upload_count']} uploaded files). Sign in with an account from the backup. "
          f"A safety copy of the previous data was kept as {report['safety_backup']}.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/backup-restore/restore/<token>/cancel", methods=["POST"])
@require("settings.manage")
def restore_cancel(token):
    svc.discard_pending(token)
    flash("Restore cancelled. Nothing was changed.", "info")
    return redirect(url_for("admin.backup_restore"))
