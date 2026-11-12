"""Append-only audit trail.

Every row stores the SHA-256 of (previous row hash + its own content), so any edit made
outside the application (DB triggers already block UPDATE/DELETE through SQL) is
detectable with verify_chain().
"""
import hashlib
import json

from flask import g, has_request_context, request

from .db import query, transaction
from .utils import utcnow_iso

GENESIS = "0" * 64
_HASHED_FIELDS = ("ts", "user_id", "username", "role_code", "action", "entity", "entity_id",
                  "prev_state", "new_state", "remarks", "ip", "user_agent")


def _dump(state):
    if state is None:
        return None
    return json.dumps(state, sort_keys=True, default=str, ensure_ascii=False)


def _digest(prev_hash, record):
    payload = json.dumps([record.get(f) for f in _HASHED_FIELDS], default=str, ensure_ascii=False)
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


def log(action, entity, entity_id=None, prev=None, new=None, remarks=None, actor=None):
    """Record an audit event. Joins the caller's transaction if one is open."""
    if actor is None and has_request_context():
        actor = getattr(g, "user", None)
    record = {
        "ts": utcnow_iso(),
        "user_id": actor["id"] if actor else None,
        "username": actor["username"] if actor else None,
        "role_code": actor["role_code"] if actor else None,
        "action": action,
        "entity": entity,
        "entity_id": None if entity_id is None else str(entity_id),
        "prev_state": _dump(prev),
        "new_state": _dump(new),
        "remarks": remarks,
        "ip": request.remote_addr if has_request_context() else "cli",
        "user_agent": (request.user_agent.string or "")[:250] if has_request_context() else None,
    }
    with transaction() as conn:
        last = conn.execute("SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = last["hash"] if last else GENESIS
        record["prev_hash"] = prev_hash
        record["hash"] = _digest(prev_hash, record)
        cols = list(record)
        conn.execute(
            f"INSERT INTO audit_logs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [record[c] for c in cols],
        )


def verify_chain():
    """Returns (ok, rows_checked, first_bad_id)."""
    prev_hash = GENESIS
    count = 0
    for row in query("SELECT * FROM audit_logs ORDER BY id"):
        rec = dict(row)
        if rec["prev_hash"] != prev_hash or _digest(prev_hash, rec) != rec["hash"]:
            return False, count, rec["id"]
        prev_hash = rec["hash"]
        count += 1
    return True, count, None
