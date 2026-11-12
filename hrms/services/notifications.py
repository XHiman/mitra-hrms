"""In-system notifications. Delivery channels (email/SMS) can be added here later."""
from ..core.db import execute, query, scalar
from ..core.utils import utcnow_iso


def notify(user_ids, title, message, link=None):
    now = utcnow_iso()
    for uid in sorted(set(u for u in user_ids if u)):
        execute("INSERT INTO notifications (user_id, title, message, link, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, title, message, link, now))


def unread_count(user_id):
    return scalar("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (user_id,)) or 0


def recent(user_id, limit=10):
    return query("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))


def mark_read(user_id, notification_id=None):
    if notification_id is None:
        execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,))
    else:
        execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND id = ?", (user_id, notification_id))
