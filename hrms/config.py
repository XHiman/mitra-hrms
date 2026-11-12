import os
import secrets
from datetime import timedelta
from pathlib import Path

ON_RENDER = os.environ.get("RENDER", "").lower() == "true"  # Render sets RENDER=true on its services
BASE_DIR = Path(__file__).resolve().parent.parent
_INSTANCE = Path(os.environ.get("HRMS_INSTANCE_DIR") or BASE_DIR / "instance")


def _secret_key():
    if os.environ.get("HRMS_SECRET_KEY"):
        return os.environ["HRMS_SECRET_KEY"]
    if ON_RENDER:  # a file key would be regenerated on every restart: all sessions would be lost each time
        import sys
        print("WARNING: HRMS_SECRET_KEY is not set; using a temporary key. Set it in the Render dashboard.",
              file=sys.stderr)
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
    SESSION_COOKIE_SECURE = os.environ.get("HRMS_SECURE_COOKIES", "1" if ON_RENDER else "0") == "1"  # set to 1 behind HTTPS
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    SESSION_IDLE_MINUTES = int(os.environ.get("HRMS_SESSION_IDLE_MINUTES", "30"))
    SESSION_ABSOLUTE_HOURS = 12

    # Behind a reverse proxy (IIS/nginx/PythonAnywhere) set to 1 so audit logs record the real client IP.
    TRUST_PROXY = os.environ.get("HRMS_TRUST_PROXY", "1" if ON_RENDER else "0") == "1"
    # WAL suits a local disk; use DELETE on network file systems (e.g. PythonAnywhere home directories).
    SQLITE_JOURNAL_MODE = os.environ.get("HRMS_SQLITE_JOURNAL", "WAL").upper()

    DEBUG = False
    MAX_UPLOAD_BYTES = 5 * 1024 * 1024
    # Backup archives: upload limit for the restore form, and a cap on what an archive may expand to.
    RESTORE_MAX_UPLOAD_BYTES = int(os.environ.get("HRMS_RESTORE_MAX_UPLOAD_MB", "200")) * 1024 * 1024
    RESTORE_MAX_UNCOMPRESSED_BYTES = int(os.environ.get("HRMS_RESTORE_MAX_UNCOMPRESSED_MB", "1024")) * 1024 * 1024
    MAX_CONTENT_LENGTH = 6 * 1024 * 1024
    DISPLAY_UTC_OFFSET_MINUTES = 330  # IST
    ORG_NAME = "MITRA"
