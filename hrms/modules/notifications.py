"""In-system notification centre."""
from flask import Blueprint, g, redirect, render_template, request, url_for

from ..core.db import query
from ..services import notifications as svc

bp = Blueprint("notifications", __name__, url_prefix="/notifications")


@bp.route("/")
def index():
    rows = query("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 200", (g.user["id"],))
    return render_template("notifications/index.html", rows=rows)


@bp.route("/<int:notification_id>/open", methods=["POST"])
def open_one(notification_id):
    row = query("SELECT * FROM notifications WHERE id = ? AND user_id = ?", (notification_id, g.user["id"]), one=True)
    if row is None:
        return redirect(url_for("notifications.index"))
    svc.mark_read(g.user["id"], notification_id)
    link = row["link"] or ""
    return redirect(link if link.startswith("/") and not link.startswith("//") else url_for("notifications.index"))


@bp.route("/read-all", methods=["POST"])
def read_all():
    svc.mark_read(g.user["id"])
    return redirect(request.referrer if request.referrer and request.host_url in request.referrer
                    else url_for("notifications.index"))
