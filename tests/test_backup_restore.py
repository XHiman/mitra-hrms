"""Backup, restore and deployment start-up tests. Fictional demo data only.

Run:  python -m unittest tests.test_backup_restore -v
"""
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrms import create_app  # noqa: E402
from hrms.cli import DEMO_PASSWORD  # noqa: E402
from hrms.core import maintenance  # noqa: E402
from hrms.core.db import SCHEMA_VERSION  # noqa: E402
from hrms.services import backup as svc  # noqa: E402

PDF = b"%PDF-1.4\n% fictional test document\n"


def make_app(root, seed=True, **extra):
    root = Path(root)
    cfg = {"INSTANCE_DIR": str(root), "DATABASE": str(root / "hrms.db"), "UPLOAD_DIR": str(root / "uploads"),
           "BACKUP_DIR": str(root / "backups"), "TESTING": True, "SECRET_KEY": "test-secret-key-" + root.name}
    cfg.update(extra)
    root.mkdir(parents=True, exist_ok=True)
    app = create_app(cfg)
    runner = app.test_cli_runner()
    res = runner.invoke(args=["seed-demo" if seed else "render-init"])
    assert res.exit_code == 0, res.output
    return app


def login(app, username, password=DEMO_PASSWORD):
    c = app.test_client()
    page = c.get("/login").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    r = c.post("/login", data={"username": username, "password": password, "_csrf": token})
    assert r.status_code == 302, f"login failed for {username}"
    return c


def csrf(c, url="/admin/backup-restore"):
    return re.search(r'name="_csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def sql(app, query, params=()):
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def sql_write(app, query, params=()):
    conn = sqlite3.connect(app.config["DATABASE"], timeout=30)
    try:
        conn.execute(query, params)
        conn.commit()
    finally:
        conn.close()


def add_upload(app, content=PDF, ext="pdf"):
    name = f"{uuid.uuid4().hex}.{ext}"
    (Path(app.config["UPLOAD_DIR"]) / name).write_bytes(content)
    return name


def download_backup(c):
    r = c.post("/admin/backup-restore/download", data={"_csrf": csrf(c)})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.mimetype == "application/zip"
    return r.get_data()


def upload_for_restore(c, data, filename="backup.zip"):
    return c.post("/admin/backup-restore/restore", content_type="multipart/form-data",
                  data={"_csrf": csrf(c), "backup": (io.BytesIO(data), filename)})


def rewrite_zip(data, mutate):
    """Unpack a backup into {name: bytes}, let `mutate(entries, manifest)` change it, repack with the
    manifest's size/sha256 of every listed file recomputed (unless mutate changes them on purpose)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        entries = {i.filename: zf.read(i) for i in zf.infolist()}
    manifest = json.loads(entries.pop("manifest.json"))
    mutate(entries, manifest)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, content in entries.items():
            zf.writestr(name, content)
    return out.getvalue()


def refresh_manifest(entries, manifest):
    manifest["files"] = [{"path": n, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                         for n, b in entries.items()]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = make_app(Path(self.tmp) / "a")
        self.admin = login(self.app, "admin")


class BackupCreation(Base):
    def test_backup_zip_contents_manifest_and_checksums(self):
        up = add_upload(self.app)
        data = download_backup(self.admin)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIsNone(zf.testzip())
            names = set(zf.namelist())
            self.assertEqual(names, {"manifest.json", "database/hrms.db", f"uploads/{up}"})
            m = json.loads(zf.read("manifest.json"))
            self.assertEqual(m["app_name"], "MITRA HRMS")
            self.assertEqual(m["backup_format_version"], 1)
            self.assertEqual(m["database_schema_version"], SCHEMA_VERSION)
            self.assertEqual(m["database_file"], "database/hrms.db")
            self.assertTrue(m["created_at"] and m["app_version"])
            self.assertIn("git_commit", m)
            self.assertEqual({f["path"] for f in m["files"]}, names - {"manifest.json"})
            for f in m["files"]:
                body = zf.read(f["path"])
                self.assertEqual(len(body), f["size"])
                self.assertEqual(hashlib.sha256(body).hexdigest(), f["sha256"])
            db = Path(self.tmp) / "check.db"
            db.write_bytes(zf.read("database/hrms.db"))
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 9)
        conn.close()

    def test_backup_excludes_secrets_and_source(self):
        (Path(self.app.config["INSTANCE_DIR"]) / "secret_key").write_text("do-not-leak")
        (Path(self.app.config["INSTANCE_DIR"]) / ".env").write_text("TOKEN=do-not-leak")
        data = download_backup(self.admin)
        self.assertNotIn(b"do-not-leak", data)
        self.assertNotIn(b"test-secret-key", data)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertFalse([n for n in zf.namelist() if not n.startswith(("uploads/", "database/")) and n != "manifest.json"])

    def test_ignores_files_in_uploads_that_are_not_attachments(self):
        add_upload(self.app)
        (Path(self.app.config["UPLOAD_DIR"]) / "evil.py").write_text("print('x')")
        with zipfile.ZipFile(io.BytesIO(download_backup(self.admin))) as zf:
            self.assertNotIn("uploads/evil.py", zf.namelist())

    def test_backup_consistent_while_database_is_being_written(self):
        stop = threading.Event()
        written = []

        def writer():
            n = 0
            while not stop.is_set():
                try:
                    sql_write(self.app, "INSERT INTO notifications (user_id, title, message, created_at) VALUES (1, ?, 'm', 'now')",
                              (f"t{n}",))
                    written.append(n)
                    n += 1
                except sqlite3.OperationalError:
                    pass
        t = threading.Thread(target=writer)
        t.start()
        try:
            snapshots = [download_backup(self.admin) for _ in range(3)]
        finally:
            stop.set()
            t.join()
        self.assertGreater(len(written), 0)
        for data in snapshots:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                p = Path(self.tmp) / "snap.db"
                p.write_bytes(zf.read("database/hrms.db"))
            conn = sqlite3.connect(p)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            conn.close()

    def test_includes_committed_wal_transactions(self):
        # a row committed through the app (WAL mode) must be in the backup, whether or not it was checkpointed
        self.assertEqual(sql(self.app, "PRAGMA journal_mode")[0][0].lower(), "wal")
        sql_write(self.app, "INSERT INTO departments (name) VALUES ('Fictional WAL Dept')")
        with zipfile.ZipFile(io.BytesIO(download_backup(self.admin))) as zf:
            p = Path(self.tmp) / "w.db"
            p.write_bytes(zf.read("database/hrms.db"))
        conn = sqlite3.connect(p)
        self.assertTrue(conn.execute("SELECT 1 FROM departments WHERE name = 'Fictional WAL Dept'").fetchone())
        conn.close()

    def test_history_and_retained_download(self):
        download_backup(self.admin)
        page = self.admin.get("/admin/backup-restore").get_data(as_text=True)
        self.assertIn("Full system backup", page)
        self.assertIn("Completed", page)
        name = re.search(r"/admin/backup-restore/files/(hrms-full-[0-9-]+\.zip)", page).group(1)
        r = self.admin.get(f"/admin/backup-restore/files/{name}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(r.get_data())))
        self.assertEqual(self.admin.get("/admin/backup-restore/files/..%2F..%2Fhrms.db").status_code, 404)
        self.assertEqual(self.admin.get("/admin/backup-restore/files/hrms-full-1.zip").status_code, 404)

    def test_system_status_shows_real_values(self):
        download_backup(self.admin)
        page = self.admin.get("/admin/backup-restore").get_data(as_text=True)
        info = svc_info(self.app)
        self.assertTrue(info["db_ok"] and info["audit_ok"])
        self.assertEqual(info["schema_version"], SCHEMA_VERSION)
        self.assertIsNotNone(info["last_backup"])
        self.assertIn("Intact", page)
        self.assertIn("version %d" % SCHEMA_VERSION, page)


def svc_info(app):
    with app.test_request_context():
        return svc.system_info()


class Permissions(Base):
    ROUTES = [("GET", "/admin/backup-restore"), ("POST", "/admin/backup-restore/download"),
              ("POST", "/admin/backup-restore/restore"), ("GET", "/admin/backup-restore/restore/abc"),
              ("POST", "/admin/backup-restore/restore/abc/confirm"), ("POST", "/admin/backup-restore/restore/abc/cancel"),
              ("GET", "/admin/backup-restore/files/hrms-full-20260101-000000.zip")]

    def test_only_system_administrators(self):
        download_backup(self.admin)  # a retained file exists
        for user in ("rahul", "hr", "ceo", "jtceo"):
            c = login(self.app, user)
            for method, url in self.ROUTES:
                r = c.get(url) if method == "GET" else c.post(url, data={"_csrf": "x"})
                self.assertIn(r.status_code, (400, 403), f"{user} {method} {url} -> {r.status_code}")
        anon = self.app.test_client()
        for method, url in self.ROUTES:
            r = anon.get(url) if method == "GET" else anon.post(url, data={})
            self.assertIn(r.status_code, (302, 400), f"anonymous {method} {url}")
            if r.status_code == 302:
                self.assertIn("/login", r.headers["Location"])

    def test_csrf_required(self):
        r = self.admin.post("/admin/backup-restore/download", data={})
        self.assertEqual(r.status_code, 400)
        r = self.admin.post("/admin/backup-restore/restore", data={"backup": (io.BytesIO(b"x"), "b.zip")},
                            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_pending_restore_belongs_to_one_user(self):
        data = download_backup(self.admin)
        r = upload_for_restore(self.admin, data)
        token = r.headers["Location"].rstrip("/").split("/")[-1]
        other = login(self.app, "admin")  # same user: allowed
        self.assertEqual(other.get(f"/admin/backup-restore/restore/{token}").status_code, 200)
        sql_write(self.app, "UPDATE users SET role_id = (SELECT id FROM roles WHERE code='SYSADMIN') WHERE username='hr'")
        # a second administrator may not take over someone else's staged upload
        hr = login(self.app, "hr")
        r = hr.get(f"/admin/backup-restore/restore/{token}")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/backup-restore", r.headers["Location"])
        self.assertNotIn(token, r.headers["Location"])


class ArchiveValidation(Base):
    def setUp(self):
        super().setUp()
        add_upload(self.app)
        self.good = download_backup(self.admin)

    def reject(self, data, fragment=None, filename="backup.zip"):
        before = sql(self.app, "SELECT COUNT(*) FROM users")[0][0]
        r = upload_for_restore(self.admin, data, filename)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("#restore"), r.headers["Location"])
        page = self.admin.get("/admin/backup-restore").get_data(as_text=True)
        self.assertIn("cannot be used", page)
        if fragment:
            self.assertIn(fragment, page)
        self.assertEqual(sql(self.app, "SELECT COUNT(*) FROM users")[0][0], before)

    def test_good_backup_is_accepted(self):
        r = upload_for_restore(self.admin, self.good)
        self.assertIn("/admin/backup-restore/restore/", r.headers["Location"])
        page = self.admin.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Confirm restore", page)
        self.assertIn("SHA-256 checksums verified", page)
        self.assertIn("Database integrity check passed", page)

    def test_rejects_non_zip_and_wrong_extension(self):
        self.reject(b"this is not a zip", "not a valid ZIP")
        self.reject(self.good, "is a .zip file", filename="backup.txt")

    def test_rejects_missing_or_foreign_manifest(self):
        self.reject(rewrite_zip(self.good, lambda e, m: m.update(app_name="Other App")), "another application")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("database/hrms.db", b"x")
        self.reject(out.getvalue(), "manifest.json is missing")

    def test_rejects_path_traversal_and_unsafe_names(self):
        for bad in ("../evil.py", "/etc/cron.d/x", "uploads/../../evil", "C:/evil.exe", "uploads\\..\\evil",
                    "database/../hrms.db", "uploads/evil.py", "app.py"):
            def add(entries, manifest, bad=bad):
                entries[bad] = b"payload"
                refresh_manifest(entries, manifest)
            self.reject(rewrite_zip(self.good, add), None)

    def test_rejects_symlink_entries(self):
        with zipfile.ZipFile(io.BytesIO(self.good)) as zf:
            entries = {i.filename: zf.read(i) for i in zf.infolist()}
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for n, b in entries.items():
                zf.writestr(n, b)
            info = zipfile.ZipInfo(f"uploads/{uuid.uuid4().hex}.pdf")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
        self.reject(out.getvalue(), "symbolic link")

    def test_rejects_tampered_content(self):
        def tamper(entries, manifest):
            name = next(n for n in entries if n.startswith("uploads/"))
            entries[name] = entries[name] + b"tampered"  # manifest keeps the old size and hash
        self.reject(rewrite_zip(self.good, tamper), "larger than the manifest")
        def flip(entries, manifest):
            name = next(n for n in entries if n.startswith("uploads/"))
            entries[name] = entries[name][:-1] + b"X"  # same size, different content
        self.reject(rewrite_zip(self.good, flip), "mismatch")

    def test_rejects_database_listed_but_checksum_wrong(self):
        def tamper(entries, manifest):
            entries["database/hrms.db"] = entries["database/hrms.db"][:-1] + b"\x00"
        self.reject(rewrite_zip(self.good, tamper), "larger than the manifest")
        def flip(entries, manifest):
            name = next(n for n in entries if n.startswith("uploads/"))
            entries[name] = entries[name][:-1] + b"X"  # same size, different content
        self.reject(rewrite_zip(self.good, flip), "mismatch")

    def test_rejects_file_missing_from_manifest_or_archive(self):
        self.reject(rewrite_zip(self.good, lambda e, m: m["files"].pop()), "disagree")
        def drop(entries, manifest):
            entries.pop(next(n for n in entries if n.startswith("uploads/")))
        self.reject(rewrite_zip(self.good, drop), "disagree")

    def test_rejects_corrupt_sqlite_database(self):
        def corrupt(entries, manifest):
            entries["database/hrms.db"] = b"SQLite format 3\x00" + os.urandom(4096)
            refresh_manifest(entries, manifest)
        self.reject(rewrite_zip(self.good, corrupt), "database")

    def test_rejects_database_failing_integrity_check(self):
        def damage(entries, manifest):
            p = Path(self.tmp) / "dmg.db"
            p.write_bytes(entries["database/hrms.db"])
            conn = sqlite3.connect(p)
            conn.execute("CREATE INDEX ix_dmg ON audit_logs(action)")
            conn.commit()
            conn.close()
            raw = bytearray(p.read_bytes())
            for off in range(8192, min(len(raw), 40000), 512):  # scribble over table pages
                raw[off:off + 64] = os.urandom(64)
            entries["database/hrms.db"] = bytes(raw)
            refresh_manifest(entries, manifest)
        self.reject(rewrite_zip(self.good, damage), "database")

    def test_rejects_newer_schema_and_newer_format(self):
        def newer_schema(entries, manifest):
            p = Path(self.tmp) / "n.db"
            p.write_bytes(entries["database/hrms.db"])
            conn = sqlite3.connect(p)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            conn.close()
            entries["database/hrms.db"] = p.read_bytes()
            manifest["database_schema_version"] = SCHEMA_VERSION + 1
            refresh_manifest(entries, manifest)
        self.reject(rewrite_zip(self.good, newer_schema), "schema version")
        self.reject(rewrite_zip(self.good, lambda e, m: m.update(backup_format_version=99)), "newer than this application")

    def test_rejects_backup_without_an_administrator(self):
        def no_admin(entries, manifest):
            p = Path(self.tmp) / "na.db"
            p.write_bytes(entries["database/hrms.db"])
            conn = sqlite3.connect(p)
            conn.execute("UPDATE users SET is_active = 0 WHERE role_id = (SELECT id FROM roles WHERE code='SYSADMIN')")
            conn.commit()
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.close()
            entries["database/hrms.db"] = p.read_bytes()
            refresh_manifest(entries, manifest)
        self.reject(rewrite_zip(self.good, no_admin), "no active administrator")

    def test_rejects_zip_bomb(self):
        self.app.config["RESTORE_MAX_UNCOMPRESSED_BYTES"] = 1000
        self.reject(self.good, "permitted size")

    def test_oversized_upload_is_refused(self):
        self.app.config["RESTORE_MAX_UPLOAD_BYTES"] = 2000
        r = upload_for_restore(self.admin, self.good)
        self.assertEqual(r.status_code, 413)


class Restore(Base):
    def stage_and_confirm(self, client, data, **form):
        r = upload_for_restore(client, data)
        loc = r.headers["Location"]
        self.assertIn("/restore/", loc, client.get("/admin/backup-restore").get_data(as_text=True)[-1500:])
        token = loc.rstrip("/").split("/")[-1]
        body = {"_csrf": csrf(client, loc), "understand": "1", "confirm_text": "RESTORE"}
        body.update(form)
        return client.post(f"/admin/backup-restore/restore/{token}/confirm", data=body)

    def test_restore_brings_back_database_and_uploads(self):
        keep = add_upload(self.app, PDF + b"keep")
        data = download_backup(self.admin)
        # changes made after the backup
        sql_write(self.app, "INSERT INTO departments (name) VALUES ('Created After Backup')")
        sql_write(self.app, "UPDATE employees SET full_name = 'Changed Name' WHERE emp_code = 'M101'")
        (Path(self.app.config["UPLOAD_DIR"]) / keep).unlink()
        extra = add_upload(self.app, PDF + b"extra")
        secret_before = self.app.config["SECRET_KEY"]

        r = self.stage_and_confirm(self.admin, data)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

        self.assertFalse(sql(self.app, "SELECT 1 FROM departments WHERE name = 'Created After Backup'"))
        self.assertEqual(sql(self.app, "SELECT full_name FROM employees WHERE emp_code = 'M101'")[0][0], "Rahul Joshi")
        up = Path(self.app.config["UPLOAD_DIR"])
        self.assertEqual((up / keep).read_bytes(), PDF + b"keep")
        self.assertFalse((up / extra).exists())
        self.assertEqual(self.app.config["SECRET_KEY"], secret_before)
        conn = sqlite3.connect(self.app.config["DATABASE"])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        conn.close()
        # audit trail records it and the chain is intact
        acts = [r[0] for r in sql(self.app, "SELECT action FROM audit_logs ORDER BY id")]
        self.assertIn("BACKUP_RESTORED", acts)
        with self.app.test_request_context():
            from hrms.core.audit import verify_chain
            self.assertTrue(verify_chain()[0])
        # a safety copy of the pre-restore state exists and holds the post-backup changes
        safety = sorted(Path(self.app.config["BACKUP_DIR"]).glob("hrms-prerestore-*.zip"))
        self.assertEqual(len(safety), 1)
        with zipfile.ZipFile(safety[0]) as zf:
            self.assertIn(f"uploads/{extra}", zf.namelist())
        # old sessions are dead; the restored accounts work
        self.assertEqual(self.admin.get("/admin/backup-restore").status_code, 302)
        login(self.app, "admin")
        login(self.app, "rahul")

    def test_both_confirmations_are_required(self):
        data = download_backup(self.admin)
        sql_write(self.app, "INSERT INTO departments (name) VALUES ('Keep Me')")
        for form in ({"understand": ""}, {"confirm_text": "restore"}, {"confirm_text": ""}):
            r = self.stage_and_confirm(self.admin, data, **form)
            self.assertEqual(r.status_code, 302)
            self.assertIn("/restore/", r.headers["Location"])
            self.assertTrue(sql(self.app, "SELECT 1 FROM departments WHERE name = 'Keep Me'"))
        self.assertFalse(list(Path(self.app.config["BACKUP_DIR"]).glob("hrms-prerestore-*")))

    def test_cancel_discards_the_staged_upload(self):
        r = upload_for_restore(self.admin, download_backup(self.admin))
        token = r.headers["Location"].rstrip("/").split("/")[-1]
        c = self.admin.post(f"/admin/backup-restore/restore/{token}/cancel", data={"_csrf": csrf(self.admin)})
        self.assertEqual(c.status_code, 302)
        self.assertFalse(list((Path(self.app.config["BACKUP_DIR"]) / "pending").glob("*")))
        self.assertEqual(self.admin.get(f"/admin/backup-restore/restore/{token}").status_code, 302)

    def test_failure_rolls_back_database_and_uploads(self):
        keep = add_upload(self.app, PDF + b"keep")
        data = download_backup(self.admin)
        sql_write(self.app, "INSERT INTO departments (name) VALUES ('Current Data')")
        (Path(self.app.config["UPLOAD_DIR"]) / keep).write_bytes(PDF + b"current version")
        with mock.patch.object(svc, "_live_checks", side_effect=svc.BackupError("simulated verification failure")):
            r = self.stage_and_confirm(self.admin, data)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("#restore"))
        self.assertTrue(sql(self.app, "SELECT 1 FROM departments WHERE name = 'Current Data'"))
        self.assertEqual((Path(self.app.config["UPLOAD_DIR"]) / keep).read_bytes(), PDF + b"current version")
        self.assertFalse(maintenance.active())
        self.assertIn("BACKUP_RESTORE_FAILED", [r[0] for r in sql(self.app, "SELECT action FROM audit_logs")])
        self.assertFalse([p for p in Path(self.app.config["UPLOAD_DIR"]).parent.iterdir() if p.name.startswith(".restore-")])
        # still signed in: the failed restore did not end sessions
        self.assertEqual(self.admin.get("/admin/backup-restore").status_code, 200)

    def test_failure_during_database_replacement_rolls_back(self):
        data = download_backup(self.admin)
        sql_write(self.app, "INSERT INTO departments (name) VALUES ('Current Data')")
        real = svc._replace_database
        calls = []

        def flaky(path):
            calls.append(path)
            real(path)
            if len(calls) == 1:
                raise RuntimeError("disk error after the copy")
        with mock.patch.object(svc, "_replace_database", side_effect=flaky):
            r = self.stage_and_confirm(self.admin, data)
        self.assertTrue(r.headers["Location"].endswith("#restore"))
        self.assertGreaterEqual(len(calls), 2)  # the rollback ran
        self.assertTrue(sql(self.app, "SELECT 1 FROM departments WHERE name = 'Current Data'"))

    def test_restore_into_a_fresh_deployment_and_repeat_deployments(self):
        """The real workflow: back up, deploy a new empty server, restore, back up again, deploy again."""
        up = add_upload(self.app, PDF + b"letter")
        sql_write(self.app, "INSERT INTO departments (name) VALUES ('Fictional Dept')")
        backup1 = download_backup(self.admin)
        users = sql(self.app, "SELECT COUNT(*) FROM users")[0][0]

        previous = backup1
        for n in range(2, 4):  # two further "deployments"
            root = Path(self.tmp) / f"deploy{n}"
            with mock.patch.dict(os.environ, {"HRMS_ADMIN_PASSWORD": "Fresh#Pass2026x"}):
                fresh = make_app(root, seed=False)  # render-init on an empty instance: new admin only
            self.assertEqual(sql(fresh, "SELECT COUNT(*) FROM users")[0][0], 1)
            fresh_secret = fresh.config["SECRET_KEY"]
            c = login(fresh, "admin", "Fresh#Pass2026x")
            r = c.get("/")  # forced password change redirect until changed: restore does not need it
            self.assertEqual(r.status_code, 302)
            c.get("/account/password")
            # the first admin must change the password before using the system
            page = c.get("/account/password").get_data(as_text=True)
            tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
            c.post("/account/password", data={"_csrf": tok, "current_password": "Fresh#Pass2026x",
                                              "new_password": "Another#Pass2026y", "confirm_password": "Another#Pass2026y"})
            r = upload_for_restore(c, previous)
            self.assertIn("/restore/", r.headers["Location"])
            token = r.headers["Location"].rstrip("/").split("/")[-1]
            r = c.post(f"/admin/backup-restore/restore/{token}/confirm",
                       data={"_csrf": csrf(c, r.headers["Location"]), "understand": "1", "confirm_text": "RESTORE"})
            self.assertIn("/login", r.headers["Location"])
            self.assertEqual(sql(fresh, "SELECT COUNT(*) FROM users")[0][0], users)
            self.assertTrue(sql(fresh, "SELECT 1 FROM departments WHERE name = 'Fictional Dept'"))
            self.assertEqual((Path(fresh.config["UPLOAD_DIR"]) / up).read_bytes(), PDF + b"letter")
            self.assertEqual(fresh.config["SECRET_KEY"], fresh_secret)
            self.assertNotEqual(fresh_secret, self.app.config["SECRET_KEY"])
            login(fresh, "admin")  # accounts of the backup are back
            login(fresh, "rahul")
            previous = download_backup(login(fresh, "admin"))

    def test_restore_older_schema_runs_migrations(self):
        data = download_backup(self.admin)

        def make_old(entries, manifest):
            p = Path(self.tmp) / "old.db"
            p.write_bytes(entries["database/hrms.db"])
            conn = sqlite3.connect(p)
            conn.execute("ALTER TABLE employees DROP COLUMN contract_end_date")
            conn.execute("ALTER TABLE leave_types DROP COLUMN is_paid")
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.close()
            entries["database/hrms.db"] = p.read_bytes()
            manifest["database_schema_version"] = 0
            refresh_manifest(entries, manifest)
        old = rewrite_zip(data, make_old)
        r = upload_for_restore(self.admin, old)
        page = self.admin.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("older database schema", page)
        r = self.stage_and_confirm(self.admin, old)
        self.assertIn("/login", r.headers["Location"])
        cols = {row[1] for row in sql(self.app, "PRAGMA table_info(employees)")}
        self.assertIn("contract_end_date", cols)
        self.assertIn("is_paid", {row[1] for row in sql(self.app, "PRAGMA table_info(leave_types)")})
        self.assertEqual(sql(self.app, "PRAGMA user_version")[0][0], SCHEMA_VERSION)
        login(self.app, "rahul")

    def test_restore_adds_permissions_missing_from_old_backup(self):
        data = download_backup(self.admin)

        def strip(entries, manifest):
            p = Path(self.tmp) / "perm.db"
            p.write_bytes(entries["database/hrms.db"])
            conn = sqlite3.connect(p)
            conn.execute("DELETE FROM role_permissions WHERE permission_code = 'analytics.view_team'")
            conn.execute("DELETE FROM permissions WHERE code = 'analytics.view_team'")
            conn.commit()
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.close()
            entries["database/hrms.db"] = p.read_bytes()
            refresh_manifest(entries, manifest)
        r = self.stage_and_confirm(self.admin, rewrite_zip(data, strip))
        self.assertIn("/login", r.headers["Location"])
        self.assertTrue(sql(self.app, "SELECT 1 FROM permissions WHERE code = 'analytics.view_team'"))
        self.assertTrue(sql(self.app, "SELECT 1 FROM role_permissions WHERE permission_code = 'analytics.view_team'"))

    def test_other_requests_get_503_during_restore_but_health_stays_up(self):
        lock = Path(self.app.config["INSTANCE_DIR"]) / "restore.lock"
        with maintenance.exclusive(lock):
            c = self.app.test_client()
            self.assertEqual(c.get("/login").status_code, 503)
            self.assertEqual(self.admin.get("/admin/backup-restore").status_code, 503)
            self.assertEqual(c.get("/healthz").status_code, 200)
            with self.assertRaises(maintenance.Busy):
                with maintenance.exclusive(lock):
                    pass
        self.assertEqual(c.get("/login").status_code, 200)

    def test_large_backup_above_normal_upload_limit(self):
        # normal requests are capped at 6 MB; the restore form accepts larger archives
        big = PDF + os.urandom(7 * 1024 * 1024)
        add_upload(self.app, big)
        data = download_backup(self.admin)
        self.assertGreater(len(data), 6 * 1024 * 1024)
        r = upload_for_restore(self.admin, data)
        self.assertIn("/restore/", r.headers["Location"])
        r = self.admin.post("/leave/apply", data={"_csrf": "x", "f": (io.BytesIO(big), "x.pdf")},
                            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)  # the ordinary limit still applies elsewhere


class Deployment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_render_init_creates_admin_only_on_empty_database_and_is_repeatable(self):
        app = make_app(Path(self.tmp) / "x", seed=False)
        self.assertEqual(sql(app, "SELECT COUNT(*) FROM users")[0][0], 1)
        row = sql(app, "SELECT username, must_change_password FROM users")[0]
        self.assertEqual((row[0], row[1]), ("admin", 1))
        out = app.test_cli_runner().invoke(args=["render-init"])
        self.assertIn("already has users", out.output)
        self.assertEqual(sql(app, "SELECT COUNT(*) FROM users")[0][0], 1)

    def test_demo_data_never_loaded_into_a_used_database(self):
        app = make_app(Path(self.tmp) / "y", seed=False)
        with mock.patch.dict(os.environ, {"HRMS_SEED_DEMO": "1"}):
            out = app.test_cli_runner().invoke(args=["render-init"])
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(sql(app, "SELECT COUNT(*) FROM users")[0][0], 1)
        self.assertFalse(sql(app, "SELECT 1 FROM users WHERE username = 'rahul'"))

    def test_demo_data_only_with_explicit_opt_in_on_empty_database(self):
        root = Path(self.tmp) / "z"
        root.mkdir()
        app = create_app({"INSTANCE_DIR": str(root), "DATABASE": str(root / "hrms.db"), "UPLOAD_DIR": str(root / "u"),
                          "BACKUP_DIR": str(root / "b"), "TESTING": True})
        with mock.patch.dict(os.environ, {"HRMS_SEED_DEMO": "1"}):
            out = app.test_cli_runner().invoke(args=["render-init"])
        self.assertEqual(out.exit_code, 0, out.output)
        self.assertTrue(sql(app, "SELECT 1 FROM users WHERE username = 'rahul'"))

    def test_admin_password_from_environment_must_meet_policy(self):
        root = Path(self.tmp) / "w"
        root.mkdir()
        app = create_app({"INSTANCE_DIR": str(root), "DATABASE": str(root / "hrms.db"), "UPLOAD_DIR": str(root / "u"),
                          "BACKUP_DIR": str(root / "b"), "TESTING": True})
        with mock.patch.dict(os.environ, {"HRMS_ADMIN_PASSWORD": "weak"}):
            out = app.test_cli_runner().invoke(args=["render-init"])
        self.assertEqual(out.exit_code, 0)
        self.assertIn("not used", out.output)
        self.assertEqual(sql(app, "SELECT COUNT(*) FROM users")[0][0], 1)

    def test_health_endpoint_is_public_and_minimal(self):
        app = make_app(Path(self.tmp) / "h")
        r = app.test_client().get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"status": "ok"})

    def test_production_settings(self):
        app = make_app(Path(self.tmp) / "p")
        self.assertFalse(app.debug)
        self.assertFalse(app.config["DEBUG"])
        r = app.test_client().get("/login")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])

    def test_render_environment_defaults_are_secure(self):
        import importlib
        import hrms.config as cfg
        with mock.patch.dict(os.environ, {"RENDER": "true"}):
            importlib.reload(cfg)
            try:
                self.assertTrue(cfg.Config.SESSION_COOKIE_SECURE)
                self.assertTrue(cfg.Config.TRUST_PROXY)
            finally:
                pass
        importlib.reload(cfg)
        self.assertFalse(cfg.Config.SESSION_COOKIE_SECURE)

    def test_start_script_is_well_formed(self):
        script = (Path(__file__).resolve().parent.parent / "render_start.sh").read_bytes()
        self.assertNotIn(b"\r\n", script)
        text = script.decode()
        for needle in ("render-init", "gunicorn run:app", "0.0.0.0:${PORT", "--workers 1", "--threads 4"):
            self.assertIn(needle, text)

    def test_cli_backup_and_restore_round_trip(self):
        app = make_app(Path(self.tmp) / "c")
        r = app.test_cli_runner().invoke(args=["backup-full"])
        self.assertEqual(r.exit_code, 0, r.output)
        zp = next(Path(app.config["BACKUP_DIR"]).glob("hrms-full-*.zip"))
        sql_write(app, "INSERT INTO departments (name) VALUES ('Temp')")
        r = app.test_cli_runner().invoke(args=["restore-backup", str(zp)])
        self.assertIn("Validation passed", r.output)
        self.assertTrue(sql(app, "SELECT 1 FROM departments WHERE name = 'Temp'"))
        r = app.test_cli_runner().invoke(args=["restore-backup", str(zp), "--yes"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertFalse(sql(app, "SELECT 1 FROM departments WHERE name = 'Temp'"))


if __name__ == "__main__":
    unittest.main()
