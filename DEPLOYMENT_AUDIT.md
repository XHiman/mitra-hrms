# MITRA HRMS — Deployment Audit

Audit of the repository as it was **before** the Render/backup work, based on reading the code (not assumptions).
Each finding states what was found, why it matters, and what was done ("Resolved") or what remains ("Open").

Baseline: Flask 3.1 app factory (`hrms.create_app`), SQLite in `instance/`, uploads in `instance/uploads/`,
hand-rolled migrations in `hrms/core/db.py`, waitress as the only production server, one commit on `main`.

---

## 1. Critical issues

| # | Finding | Why it matters | Status |
|---|---------|----------------|--------|
| C1 | **Render Free has an ephemeral filesystem.** SQLite DB, uploads, session secret file and server-side backups all live under `instance/`. Render Free loses the disk on redeploy, restart *and* idle spin-down (15 min). | Every deploy or sleep wipes all HR data. Verified against Render's documentation. | **Mitigated** by downloadable full-system backup + upload-and-restore; documented. Cannot be removed on Free (see Open risks in `IMPLEMENTATION_REPORT.md`). |
| C2 | **No `.gitignore` in the repo** beyond two user-added lines. `instance/`, `*.db`, `.venv`, `__pycache__`, `.env` could be committed and pushed to a public GitHub repo. | Leak of employee data, password hashes, secret key. | **Resolved** – `.gitignore` extended (`instance/`, `*.db*`, `.env*`, venvs, caches, `hrms-*.zip`). |
| C3 | **No Render/Gunicorn files**: no `render_start.sh`, no `render.yaml`, no Gunicorn dependency, no health-check endpoint. | Cannot be deployed as specified. | **Resolved** – added. |
| C4 | **Secret key regenerated on every fresh disk.** Without `HRMS_SECRET_KEY` the app creates `instance/secret_key`, which is lost on Render, invalidating all sessions after each deploy. | Forced logouts; weak operational posture. | **Resolved** – `render.yaml` generates `HRMS_SECRET_KEY` once; config warns if missing on Render. The secret is never included in backups and never overwritten by restore. |
| C5 | **Empty database after every redeploy has no admin.** Only `seed-demo` (hard-coded demo password) or manual `create-admin` existed. | Nobody can log in after a deploy; tempting to run demo seeding in production. | **Resolved** – `render-init` creates the first admin from `HRMS_ADMIN_*` (or a one-time generated password printed once to logs); never seeds demo data unless `HRMS_SEED_DEMO=1` **and** the DB is empty. |
| C6 | **Global 6 MB request cap** would reject any realistic restore ZIP. | Restore impossible. | **Resolved** – cap raised *only* for the restore endpoint (`HRMS_RESTORE_MAX_UPLOAD_MB`, default 200); the rest of the app keeps 6 MB. |

## 2. Deployment requirements

| # | Requirement | Status |
|---|-------------|--------|
| D1 | `requirements.txt`: Flask pinned to 3.1+ (needed for per-request `max_content_length`), `gunicorn` (non-Windows only), `waitress` kept for Windows/local, `openpyxl`. | Done |
| D2 | `render_start.sh` (LF endings enforced by `.gitattributes`): runs `render-init` then `exec gunicorn run:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4`. | Done |
| D3 | `render.yaml`: Free plan, branch `main`, `pip install -r requirements.txt`, `bash render_start.sh`, health check `/healthz`, env vars. | Done |
| D4 | One Gunicorn worker is mandatory: SQLite is single-writer and the maintenance/restore gate is in-process. | Done (documented) |
| D5 | Proxy handling (`X-Forwarded-*`) and secure cookies default **on** when `RENDER=true`; configurable otherwise. | Done |
| D6 | `DEBUG` forced off (`app.debug = False`); no debug server in any start path. | Done |
| D7 | Version/commit/deployment id from `RENDER_GIT_COMMIT`, `RENDER_INSTANCE_ID`/`RENDER_SERVICE_ID`, falling back to `git rev-parse`. | Done |
| D8 | `deploy/PYTHONANYWHERE.md` refers to a `deploy/mitra-hrms.zip` that is not in the repository. | Open (PythonAnywhere guide left untouched; not part of the Render path). |

## 3. Security improvements

| # | Finding | Status |
|---|---------|--------|
| S1 | Cookies were `Secure` only when `HRMS_SECURE_COOKIES=1` was set manually. | Defaults on for Render. |
| S2 | Proxy headers were not trusted by default → wrong scheme/IP behind Render's TLS proxy (affects secure cookies and audit IPs). | Defaults on for Render. |
| S3 | Hard-coded demo password in `cli.py` (`DEMO_PASSWORD`) used by `seed-demo`. | Kept for tests/local demo only; production path (`render-init`) never uses it. |
| S4 | Existing "database backup" produced a raw SQLite copy stored on the server and downloadable; no integrity manifest, no restore. | Kept (not removed); new full-system flow supersedes it and is linked from the old page. |
| S5 | A restore endpoint is a high-value target (arbitrary ZIP → overwrites data). | Designed defensively: admin-only (`settings.manage`), CSRF, safe-name whitelist, no symlinks, no absolute/`..` names, no executables extracted, zip-bomb limits, SHA-256 of every entry, `PRAGMA integrity_check`, schema ≤ current, two explicit confirmations (checkbox + typing `RESTORE`), staged upload bound to the user and expiring in 30 min, never writes outside `instance/uploads/` + DB. |
| S6 | Restored backup could lock everyone out (no active admin). | Rejected at validation: backup must contain an active user holding `roles.manage`. |
| S7 | Backups contain personal data & password hashes. | Admin-only download, audit-logged, UI warning; never contain `.env`, secret key, tokens, or source. |
| S8 | Concurrent writes during restore. | Maintenance gate: other requests get 503 while the restore holds an exclusive lock (in-process flag + lock file); `/healthz` stays up. |
| S9 | Old sessions after restore may reference non-existent users. | All `session_version` values bumped → everyone, including the admin, is signed out. |

## 4. Backup and recovery requirements

| # | Requirement | Implementation |
|---|-------------|----------------|
| B1 | Consistent SQLite snapshot incl. WAL | `sqlite3.Connection.backup()` (online backup API), converted to `journal_mode=DELETE`, `integrity_check` before packaging. |
| B2 | Full-system ZIP | `database/hrms.db` + `uploads/*` + `manifest.json` (app, format version, timestamp, app version, git SHA, db filename, schema version, per-file size & SHA-256, row counts). |
| B3 | Exclusions | No `.env`, secret key, tokens, venv, source, temp files. |
| B4 | Restore validation | Name rules, duplicates, symlinks, size caps, manifest ↔ archive agreement, SHA-256, integrity check, required tables, schema compatibility, audit-chain check (warning). |
| B5 | Atomic restore with rollback | Safety backup first; DB replaced via backup API (atomic); uploads via staged directory swap; any failure → DB and uploads rolled back, `BACKUP_RESTORE_FAILED` audited. |
| B6 | Post-restore | `bootstrap()` runs migrations on older schemas; live checks; `BACKUP_RESTORED` audited. |
| B7 | History | `BACKUP_DIR/history.json` (ephemeral by nature) + "last backup/restore" read from the audit log (travels with the DB). UI states plainly that server copies are temporary. |
| B8 | Newer schema must not be silently downgraded | `PRAGMA user_version` introduced (`SCHEMA_VERSION = 1`); backups from a newer schema are rejected. |
| B9 | CLI parity | `flask --app run backup-full`, `restore-backup <zip> [--yes]`. |

## 5. Functional improvements

| # | Finding | Status |
|---|---------|--------|
| F1 | "No recent backup" dashboard alert looked at DB-only backup files on disk (wrong on an ephemeral disk). | Now uses the audit log. |
| F2 | No system-status page. | Added to Backup & Restore: DB health, size, uploads usage, free disk, audit-chain integrity, schema version, app version/commit, deployment id, last backup/restore. All values are real. |
| F3 | No busy feedback on long operations. | `data-busy` handler in `app.js`. |
| F4 | Navigation entry. | "Settings and backups" highlights for the new pages. |

## Known, unresolved by design

* **SQLite on Render Free** remains single-node and ephemeral. For durable data use a paid plan with a persistent disk or move to PostgreSQL (a larger change, intentionally not done silently).
* The in-process daily job hook only runs while the service is awake; a sleeping Free service runs nothing.
* A restore replaces *all* data; there is no partial/merge restore.
* A pre-existing flaky test (`test_robustness.RandomWalk…`) fails independently of this work (see `IMPLEMENTATION_REPORT.md`).
