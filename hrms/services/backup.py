"""Full-system backup and restore.

A backup is one ZIP file::

    manifest.json            format version, app version/commit, schema version, timestamps, file list, SHA-256s
    database/hrms.db         consistent snapshot made with SQLite's online backup API (includes WAL contents)
    uploads/<uuid>.<ext>     leave attachments

Not included, by design: environment variables, the secret key, source code, other temp/backup files.

Restore validates the archive completely (ZIP integrity, entry names, manifest, checksums, SQLite integrity,
compatibility) before touching anything, takes a safety backup of the current system, replaces the database
with the backup API (atomic, works while connections are open) and the uploads folder by directory swap, runs
migrations, verifies, and rolls everything back on any failure. The server's secrets are never read or written.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from flask import current_app

from .. import version
from ..core import audit, maintenance
from ..core.db import SCHEMA_VERSION, query, transaction
from ..core.utils import get_int_setting, utcnow_iso

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DB_ARCNAME = "database/hrms.db"
UPLOAD_DIR_ARC = "uploads/"
UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(pdf|png|jpg|jpeg)$")
BACKUP_NAME_RE = re.compile(r"^hrms-(full|prerestore)-\d{8}-\d{6}\.zip$")
REQUIRED_TABLES = {"users", "employees", "roles", "role_permissions", "permissions", "settings", "audit_logs",
                   "leave_requests", "leave_balances"}
MAX_ENTRIES = 50000
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
PENDING_TTL = timedelta(minutes=30)
_history_lock = threading.Lock()
SUMMARY_COUNT_TABLES = ("users", "employees", "leave_requests", "salary_slips", "timesheet_entries", "audit_logs")


class BackupError(Exception):
    """The archive (or the operation) is not acceptable. `args[0]` is a user-readable message."""


# ---------------------------------------------------------------- paths
def backup_dir():
    p = Path(current_app.config["BACKUP_DIR"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def upload_dir():
    p = Path(current_app.config["UPLOAD_DIR"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def _work_dir(prefix):
    """Temp area next to the instance data (same filesystem as uploads; never inside the web root)."""
    base = Path(current_app.config["INSTANCE_DIR"]) / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def _open_readonly(path):
    """Open a scratch copy of a database for reading only (plain connection: also works for WAL-mode files)."""
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- history (a small JSON file next to the backups)
def _history_path():
    return backup_dir() / "history.json"


def _read_history():
    try:
        data = json.loads(_history_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _record_history(entry):
    with _history_lock:
        items = _read_history()
        items.append(entry)
        items = items[-200:]
        tmp = _history_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
        os.replace(tmp, _history_path())


def list_backups():
    """Backup history, newest first. `retained` is true only while the file still exists on this server."""
    rows, seen = [], set()
    for e in reversed(_read_history()):
        path = backup_dir() / e.get("name", "")
        rows.append({**e, "retained": bool(e.get("name")) and BACKUP_NAME_RE.match(e["name"]) is not None
                     and path.is_file()})
        seen.add(e.get("name"))
    for p in sorted(backup_dir().glob("hrms-*.db"), reverse=True):  # older database-only backups
        st = p.stat()
        rows.append({"name": p.name, "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                     .replace(microsecond=0).isoformat(), "kind": "database-only", "status": "success",
                     "size": st.st_size, "retained": True, "legacy_db": True})
    return rows


def last_events():
    """Last successful backup / restore, taken from the audit log (it travels with the database)."""
    def last(*actions):
        marks = ",".join("?" * len(actions))
        r = query(f"SELECT ts, action, remarks FROM audit_logs WHERE action IN ({marks}) "
                  "ORDER BY id DESC LIMIT 1", actions, one=True)
        return dict(r) if r else None
    return {"backup": last("BACKUP_FULL_CREATED", "DATABASE_BACKUP"),
            "restore": last("BACKUP_RESTORED", "BACKUP_RESTORE_FAILED")}


# ---------------------------------------------------------------- creating a backup
def _snapshot_db(dest):
    """Consistent copy of the live database via SQLite's online backup API (includes committed WAL pages)."""
    src = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")  # a single self-contained file, no -wal/-shm needed to open it
    finally:
        dst.close()
        src.close()


def _list_uploads():
    return sorted(p for p in upload_dir().iterdir() if p.is_file() and UPLOAD_NAME_RE.match(p.name))


def _db_facts(db_path):
    """Schema version and headline row counts of a database file (read-only)."""
    conn = _open_readonly(db_path)
    try:
        counts = {}
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in SUMMARY_COUNT_TABLES:
            if t in existing:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return conn.execute("PRAGMA user_version").fetchone()[0], counts
    finally:
        conn.close()


def create_backup(kind="full", created_by=None):
    """Build the ZIP, keep a copy in the backup folder and return its description.

    kind: 'full' (administrator request) or 'prerestore' (safety copy taken just before a restore)."""
    if kind not in ("full", "prerestore"):
        raise ValueError(kind)
    now = datetime.now(timezone.utc)
    name = f"hrms-{kind}-{now.strftime('%Y%m%d-%H%M%S')}.zip"
    final = backup_dir() / name
    work = _work_dir("bk-")
    try:
        snap = work / "hrms.db"
        _snapshot_db(snap)
        chk = sqlite3.connect(snap)
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
        if ok != "ok":
            raise BackupError(f"The database snapshot failed its integrity check ({ok}); no backup was written.")
        schema_version, counts = _db_facts(snap)
        files = []
        part = final.with_suffix(".zip.part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            def add(src_path, arcname):
                h, size = hashlib.sha256(), 0
                info = zipfile.ZipInfo(arcname, date_time=now.timetuple()[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with open(src_path, "rb") as fh, zf.open(info, "w", force_zip64=True) as out:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                files.append({"path": arcname, "size": size, "sha256": h.hexdigest()})

            add(snap, DB_ARCNAME)
            skipped = 0
            for up in _list_uploads():
                try:
                    add(up, UPLOAD_DIR_ARC + up.name)
                except FileNotFoundError:  # deleted while the backup ran
                    skipped += 1
            counts["uploads"] = sum(1 for f in files if f["path"].startswith(UPLOAD_DIR_ARC))
            manifest = {
                "app_name": version.APP_NAME,
                "backup_format_version": FORMAT_VERSION,
                "backup_type": kind,
                "created_at": now.replace(microsecond=0).isoformat(),
                "created_by": created_by,
                "app_version": version.VERSION,
                "git_commit": version.commit(),
                "deployment_id": version.deployment_id(),
                "database_file": DB_ARCNAME,
                "database_schema_version": schema_version,
                "sqlite_version": sqlite3.sqlite_version,
                "counts": counts,
                "files": files,
            }
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=1))
        os.replace(part, final)
        size = final.stat().st_size
        entry = {"id": secrets.token_hex(6), "name": name, "created_at": manifest["created_at"], "kind": kind,
                 "status": "success", "size": size, "files": len(files), "created_by": created_by}
        _record_history(entry)
        _prune(kind)
        return {**entry, "path": final, "manifest": manifest}
    except Exception as exc:
        for leftover in (final.with_suffix(".zip.part"),):
            leftover.unlink(missing_ok=True)
        _record_history({"id": secrets.token_hex(6), "name": name, "created_at": now.replace(microsecond=0).isoformat(),
                         "kind": kind, "status": "failed", "size": 0, "error": str(exc)[:300], "created_by": created_by})
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _prune(kind):
    keep = max(get_int_setting("backup_retention"), 1) if kind == "full" else 5
    for old in sorted(backup_dir().glob(f"hrms-{kind}-*.zip"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)


def retained_path(name):
    """Path of a retained backup, or None. Only names we generated are accepted (no path components)."""
    if not BACKUP_NAME_RE.match(name or ""):
        return None
    p = backup_dir() / name
    return p if p.is_file() else None


# ---------------------------------------------------------------- validating an archive
def _safe_member(name):
    """True for the only member names a backup may contain."""
    if name == MANIFEST_NAME or name == DB_ARCNAME:
        return True
    return name.startswith(UPLOAD_DIR_ARC) and UPLOAD_NAME_RE.match(name[len(UPLOAD_DIR_ARC):]) is not None


def _check_names(infos):
    names = set()
    if len(infos) > MAX_ENTRIES:
        raise BackupError("The archive has too many entries.")
    for i in infos:
        n = i.filename
        if i.is_dir():
            if n.rstrip("/") not in ("database", "uploads"):
                raise BackupError(f"Unexpected folder in archive: {n[:80]!r}.")
            continue
        p = PurePosixPath(n)
        if (n.startswith(("/", "\\")) or "\\" in n or "\x00" in n or re.match(r"^[A-Za-z]:", n)
                or ".." in p.parts or p.is_absolute()):
            raise BackupError(f"Unsafe path in archive: {n[:80]!r}. The backup was rejected.")
        if stat.S_ISLNK(i.external_attr >> 16):
            raise BackupError(f"Archive contains a symbolic link ({n[:80]!r}). The backup was rejected.")
        if not _safe_member(n):
            raise BackupError(f"Unexpected file in archive: {n[:80]!r}. Only the database, uploads and manifest "
                              "are allowed.")
        if n in names:
            raise BackupError(f"Duplicate entry in archive: {n[:80]!r}.")
        names.add(n)
    return names


def _read_manifest(zf):
    try:
        info = zf.getinfo(MANIFEST_NAME)
    except KeyError:
        raise BackupError("This is not an HRMS backup: manifest.json is missing.")
    if info.file_size > MAX_MANIFEST_BYTES:
        raise BackupError("The manifest is unreasonably large.")
    try:
        m = json.loads(zf.read(info).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("manifest.json is not valid JSON.")
    if not isinstance(m, dict):
        raise BackupError("manifest.json has an unexpected structure.")
    if m.get("app_name") != version.APP_NAME:
        raise BackupError(f"This backup belongs to another application ({str(m.get('app_name'))[:40]!r}).")
    fv = m.get("backup_format_version")
    if not isinstance(fv, int) or fv < 1:
        raise BackupError("The backup format version is missing or invalid.")
    if fv > FORMAT_VERSION:
        raise BackupError(f"This backup uses format {fv}, which is newer than this application understands "
                          f"(format {FORMAT_VERSION}). Update the application first.")
    files = m.get("files")
    if not isinstance(files, list) or not files:
        raise BackupError("The manifest lists no files.")
    for f in files:
        if not (isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("size"), int)
                and f["size"] >= 0 and isinstance(f.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", f["sha256"])):
            raise BackupError("The manifest contains a malformed file entry.")
    if m.get("database_file") != DB_ARCNAME:
        raise BackupError("The manifest does not point to the expected database file.")
    return m


def validate_archive(zip_path, keep_db_in=None):
    """Fully validate a backup ZIP. Raises BackupError with a readable message; otherwise returns a report.

    If `keep_db_in` (a directory) is given, the validated database is extracted there as report['db_path']."""
    zip_path = Path(zip_path)
    cfg = current_app.config
    if not zipfile.is_zipfile(zip_path):
        raise BackupError("The file is not a valid ZIP archive.")
    steps = []
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise BackupError("The file is not a valid ZIP archive.")
    with zf:
        infos = zf.infolist()
        names = _check_names(infos)
        steps.append("Archive structure and file names are safe")
        total = sum(i.file_size for i in infos)
        if total > cfg["RESTORE_MAX_UNCOMPRESSED_BYTES"]:
            raise BackupError("The archive expands to more than the permitted size "
                              f"({cfg['RESTORE_MAX_UNCOMPRESSED_BYTES'] // (1 << 20)} MB).")
        manifest = _read_manifest(zf)
        listed = {f["path"]: f for f in manifest["files"]}
        if len(listed) != len(manifest["files"]):
            raise BackupError("The manifest lists the same file twice.")
        if DB_ARCNAME not in names:
            raise BackupError("The archive does not contain the database.")
        if set(listed) != names - {MANIFEST_NAME}:
            extra = sorted((names - {MANIFEST_NAME}) ^ set(listed))[:3]
            raise BackupError(f"The manifest and the archive disagree about their contents ({', '.join(extra)}).")
        bad = zf.testzip()
        if bad:
            raise BackupError(f"The ZIP is damaged: {bad!r} failed its CRC check.")
        steps.append("ZIP integrity verified")
        work = Path(keep_db_in) if keep_db_in else _work_dir("val-")
        try:
            db_path = work / "hrms.db"
            for path, meta in listed.items():
                h, n = hashlib.sha256(), 0
                out = open(db_path, "wb") if path == DB_ARCNAME else None
                try:
                    with zf.open(path) as src:
                        for chunk in iter(lambda: src.read(1 << 20), b""):
                            n += len(chunk)
                            if n > meta["size"]:  # header lied about the size: stop reading
                                raise BackupError(f"{path} is larger than the manifest says.")
                            h.update(chunk)
                            if out:
                                out.write(chunk)
                finally:
                    if out:
                        out.close()
                if n != meta["size"] or h.hexdigest() != meta["sha256"]:
                    raise BackupError(f"Checksum mismatch for {path}: the backup is damaged or was modified.")
            steps.append(f"SHA-256 checksums verified for {len(listed)} file(s)")
            report = _check_database(db_path, manifest)
        except BaseException:
            if not keep_db_in:
                shutil.rmtree(work, ignore_errors=True)
            raise
    steps.append("Database integrity check passed")
    uploads = [p for p in listed if p.startswith(UPLOAD_DIR_ARC)]
    report.update({"manifest": manifest, "steps": steps, "upload_count": len(uploads),
                   "upload_bytes": sum(listed[p]["size"] for p in uploads),
                   "db_bytes": listed[DB_ARCNAME]["size"], "zip_bytes": zip_path.stat().st_size,
                   "db_path": db_path if keep_db_in else None})
    if not keep_db_in:
        shutil.rmtree(work, ignore_errors=True)
    return report


def _check_database(db_path, manifest):
    conn = _open_readonly(db_path)
    conn.row_factory = sqlite3.Row
    warnings = []
    try:
        try:
            res = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"The database inside the backup is not readable: {exc}")
        if [r[0] for r in res] != ["ok"]:
            raise BackupError("The database inside the backup failed PRAGMA integrity_check: "
                              + "; ".join(r[0] for r in res[:3]))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise BackupError("The database is not an HRMS database (missing tables: " + ", ".join(missing[:4]) + ").")
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
        if schema > SCHEMA_VERSION:
            raise BackupError(f"This backup has database schema version {schema}, but this application only "
                              f"supports up to {SCHEMA_VERSION}. Deploy the newer application version, then restore.")
        if manifest.get("database_schema_version") != schema:
            raise BackupError("The manifest's schema version does not match the database.")
        if conn.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0] < 1:
            raise BackupError("The backup contains no active user; restoring it would lock everyone out.")
        if not conn.execute("""SELECT 1 FROM users u JOIN role_permissions rp ON rp.role_id = u.role_id
                                WHERE u.is_active = 1 AND rp.permission_code = 'roles.manage'""").fetchone():
            raise BackupError("The backup has no active administrator (role-management rights); restoring it "
                              "would lock the administrators out.")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            warnings.append("The database contains foreign-key inconsistencies (they come from the original data "
                            "and will be restored as they are).")
        ok, count, bad_id = audit.verify_rows(conn.execute("SELECT * FROM audit_logs ORDER BY id"))
        if not ok:
            warnings.append(f"The audit-log hash chain in this backup is broken at entry #{bad_id}.")
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in SUMMARY_COUNT_TABLES if t in tables}
    finally:
        conn.close()
    if schema < SCHEMA_VERSION:
        warnings.append(f"The backup uses an older database schema ({schema}); it will be upgraded to "
                        f"{SCHEMA_VERSION} automatically after restoring.")
    if manifest.get("app_version") != version.VERSION:
        warnings.append(f"The backup was made by application version {manifest.get('app_version')!r}; this server "
                        f"runs {version.VERSION!r}.")
    return {"schema_version": schema, "counts": counts, "warnings": warnings}


# ---------------------------------------------------------------- pending (uploaded, awaiting confirmation) restores
def _pending_dir():
    p = backup_dir() / "pending"
    p.mkdir(exist_ok=True)
    return p


def purge_pending():
    now = datetime.now(timezone.utc)
    for meta in _pending_dir().glob("*.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            expired = now - datetime.fromisoformat(data["created"]) > PENDING_TTL
        except (OSError, ValueError, KeyError):
            expired = True
        if expired:
            meta.with_suffix(".zip").unlink(missing_ok=True)
            meta.unlink(missing_ok=True)


def stage_upload(file_storage, user):
    """Save an uploaded backup, validate it completely and keep it for the confirmation step."""
    purge_pending()
    if not file_storage or not file_storage.filename:
        raise BackupError("Choose a backup file first.")
    if not file_storage.filename.lower().endswith(".zip"):
        raise BackupError("A backup file is a .zip file.")
    token = secrets.token_urlsafe(18)
    dest = _pending_dir() / f"{token}.zip"
    try:
        file_storage.save(dest)
        report = validate_archive(dest)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    summary = _public_summary(report)
    summary.update({"filename": Path(file_storage.filename).name[:120]})
    (_pending_dir() / f"{token}.json").write_text(json.dumps(
        {"created": datetime.now(timezone.utc).isoformat(), "user_id": user["id"], "summary": summary}),
        encoding="utf-8")
    return token, summary


def _public_summary(report):
    m = report["manifest"]
    return {"created_at": m.get("created_at"), "app_version": m.get("app_version"), "git_commit": m.get("git_commit"),
            "backup_type": m.get("backup_type"), "schema_version": report["schema_version"],
            "counts": report["counts"], "uploads": report["upload_count"], "upload_bytes": report["upload_bytes"],
            "db_bytes": report["db_bytes"], "zip_bytes": report["zip_bytes"], "warnings": report["warnings"],
            "steps": report["steps"], "created_by": m.get("created_by")}


def load_pending(token, user):
    """(zip path, summary) for a staged upload that belongs to this user and has not expired, else BackupError."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", token or ""):
        raise BackupError("This restore request is not valid.")
    meta = _pending_dir() / f"{token}.json"
    zp = _pending_dir() / f"{token}.zip"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data["created"])
    except (OSError, ValueError, KeyError):
        raise BackupError("This restore request has expired. Upload the backup again.")
    if data.get("user_id") != user["id"] or age > PENDING_TTL or not zp.is_file():
        raise BackupError("This restore request has expired. Upload the backup again.")
    return zp, data["summary"]


def discard_pending(token):
    if re.fullmatch(r"[A-Za-z0-9_-]{20,40}", token or ""):
        (_pending_dir() / f"{token}.zip").unlink(missing_ok=True)
        (_pending_dir() / f"{token}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- restoring
def _replace_database(src_path):
    """Overwrite the live database with the contents of src_path using SQLite's backup API: one atomic
    transaction on the destination, safe while other connections exist (they are held off by maintenance mode)."""
    src = _open_readonly(src_path)
    dst = sqlite3.connect(current_app.config["DATABASE"], timeout=60)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _extract_uploads(zip_path, staging):
    staging.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith(UPLOAD_DIR_ARC):
                continue
            name = info.filename[len(UPLOAD_DIR_ARC):]
            if not UPLOAD_NAME_RE.match(name):  # already enforced; checked again at the point of writing
                raise BackupError("Unsafe upload file name in archive.")
            with zf.open(info) as src, open(staging / name, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def _live_checks(expected_users, expected_uploads):
    conn = sqlite3.connect(current_app.config["DATABASE"], timeout=30)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("The restored database failed its integrity check.")
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if n != expected_users:
            raise BackupError(f"Restored user count ({n}) does not match the backup ({expected_users}).")
        if not conn.execute("""SELECT 1 FROM users u JOIN role_permissions rp ON rp.role_id = u.role_id
                                WHERE u.is_active = 1 AND rp.permission_code = 'roles.manage'""").fetchone():
            raise BackupError("After restoring, no active administrator exists.")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise BackupError("The restored database was not upgraded to the current schema.")
    finally:
        conn.close()
    present = sum(1 for _ in _list_uploads())
    if present != expected_uploads:
        raise BackupError(f"Restored upload count ({present}) does not match the backup ({expected_uploads}).")


def restore_archive(zip_path, actor):
    """Validate, then replace database and uploads from the archive. Returns the restore report.

    On any failure the previous database and uploads are put back and BackupError/Exception is raised."""
    from ..cli import bootstrap  # local import: cli imports services lazily too

    work = _work_dir("rs-")
    try:
        report = validate_archive(zip_path, keep_db_in=work)  # raises before anything is touched
        lock = Path(current_app.config["INSTANCE_DIR"]) / "restore.lock"
        try:
            with maintenance.exclusive(lock):
                return _restore_locked(report, zip_path, actor, work, bootstrap)
        except maintenance.Busy as exc:
            raise BackupError(str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _restore_locked(report, zip_path, actor, work, bootstrap):
    uploads = upload_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    staging = uploads.parent / f".restore-staging-{stamp}"
    old_uploads = uploads.parent / f".restore-old-{stamp}"
    safety = create_backup("prerestore", created_by=actor["username"] if actor else None)  # abort if this fails
    swapped = False
    db_replaced = False
    try:
        _extract_uploads(zip_path, staging)
        db_replaced = True  # set first: even a failure inside the replacement is followed by a rollback
        _replace_database(report["db_path"])
        os.replace(uploads, old_uploads)
        os.replace(staging, uploads)
        swapped = True
        bootstrap()  # migrations: init_schema + permission/role/setting upgrades, never touches existing rows
        with transaction() as conn:  # end every session that existed before the restore
            conn.execute("UPDATE users SET session_version = session_version + 1")
        _live_checks(report["counts"]["users"], report["upload_count"])
        audit.log("BACKUP_RESTORED", "system", None,
                  new={"backup_created_at": report["manifest"].get("created_at"),
                       "backup_app_version": report["manifest"].get("app_version"),
                       "schema_from": report["schema_version"], "schema_to": SCHEMA_VERSION,
                       "users": report["counts"].get("users"), "uploads": report["upload_count"],
                       "safety_backup": safety["name"]},
                  remarks="System restored from an uploaded backup", actor=actor)
        shutil.rmtree(old_uploads, ignore_errors=True)
        return {**report, "safety_backup": safety["name"]}
    except BaseException as exc:
        problems = []
        if db_replaced:
            try:
                _rollback_database(safety["path"], work)
            except Exception as rb:  # noqa: BLE001
                problems.append(f"database rollback failed: {rb}")
        if swapped:
            try:
                shutil.rmtree(uploads, ignore_errors=True)
                os.replace(old_uploads, uploads)
            except OSError as rb:
                problems.append(f"uploads rollback failed: {rb}")
        shutil.rmtree(staging, ignore_errors=True)
        if not swapped and old_uploads.exists() and not uploads.exists():
            os.replace(old_uploads, uploads)
        try:
            audit.log("BACKUP_RESTORE_FAILED", "system", None, new={"error": str(exc)[:300]},
                      remarks="Restore failed; previous data put back" + (" (WITH PROBLEMS: " + "; ".join(problems) + ")" if problems else ""),
                      actor=actor)
        except Exception:  # noqa: BLE001
            pass
        if problems:
            raise BackupError(f"Restore failed and the automatic rollback was incomplete ({'; '.join(problems)}). "
                              f"A safety backup is on the server: {safety['name']}.")
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"Restore failed and the previous data was put back. Cause: {exc}")


def _rollback_database(safety_zip, work):
    out = work / "rollback.db"
    with zipfile.ZipFile(safety_zip) as zf, zf.open(DB_ARCNAME) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    _replace_database(out)


# ---------------------------------------------------------------- system information (real values only)
def system_info():
    import platform
    cfg = current_app.config
    db_path = Path(cfg["DATABASE"])
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        audit_ok, audit_n, audit_bad = audit.verify_rows(_rows(conn))
    finally:
        conn.close()
    wal = Path(str(db_path) + "-wal")
    ups = _list_uploads()
    try:
        free = shutil.disk_usage(db_path.parent).free
    except OSError:
        free = None
    ev = last_events()
    return {
        "app_version": version.VERSION, "commit": version.commit(), "deployment_id": version.deployment_id(),
        "on_render": version.on_render(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "schema_version": schema, "schema_expected": SCHEMA_VERSION, "journal_mode": journal.upper(),
        "db_ok": quick == "ok", "db_check": quick, "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "uploads_count": len(ups), "uploads_bytes": sum(p.stat().st_size for p in ups),
        "disk_free": free, "audit_ok": audit_ok, "audit_entries": audit_n, "audit_bad_id": audit_bad,
        "last_backup": ev["backup"], "last_restore": ev["restore"],
        "process_started": PROCESS_STARTED.isoformat(timespec="seconds"),
        "retained": [b for b in list_backups() if b["retained"]],
    }


def _rows(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM audit_logs ORDER BY id")


PROCESS_STARTED = datetime.now(timezone.utc)
