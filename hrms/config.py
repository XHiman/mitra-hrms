import os
import secrets
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_INSTANCE = Path(os.environ.get("HRMS_INSTANCE_DIR") or BASE_DIR / "instance")


def _secret_key():
    if os.environ.get("HRMS_SECRET_KEY"):
        return os.environ["HRMS_SECRET_KEY"]
    _INSTANCE.mkdir(parents=True, exist_ok=True)
    key_file = _INSTANCE / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


class Config:
    INSTANCE_DIR = str(_INSTANCE)
    DATABASE = os.environ.get("HRMS_DATABASE", str(_INSTANCE / "hrms.db"))
    UPLOAD_DIR = str(_INSTANCE / "uploads")
    BACKUP_DIR = str(_INSTANCE / "backups")

    SECRET_KEY = None  # resolved in create_app
    SESSION_COOKIE_NAME = "mitra_hrms"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("HRMS_SECURE_COOKIES", "0") == "1"  # set to 1 behind HTTPS
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    SESSION_IDLE_MINUTES = int(os.environ.get("HRMS_SESSION_IDLE_MINUTES", "30"))
    SESSION_ABSOLUTE_HOURS = 12

    # Behind a reverse proxy (IIS/nginx/PythonAnywhere) set to 1 so audit logs record the real client IP.
    TRUST_PROXY = os.environ.get("HRMS_TRUST_PROXY", "0") == "1"
    # WAL suits a local disk; use DELETE on network file systems (e.g. PythonAnywhere home directories).
    SQLITE_JOURNAL_MODE = os.environ.get("HRMS_SQLITE_JOURNAL", "WAL").upper()

    MAX_UPLOAD_BYTES = 5 * 1024 * 1024
    MAX_CONTENT_LENGTH = 6 * 1024 * 1024
    DISPLAY_UTC_OFFSET_MINUTES = 330  # IST
    ORG_NAME = "MITRA"
