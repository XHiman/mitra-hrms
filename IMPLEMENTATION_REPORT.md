# Implementation Report — Render deployment + Backup & Restore

## Summary
MITRA HRMS now deploys to Render Free (Gunicorn, 1 worker × 4 threads, SQLite kept) and has an admin-only
**Backup & Restore** system whose workflow does not rely on Render's temporary disk:
download a full ZIP before pushing → Render redeploys (empty instance + fresh admin) → upload the ZIP → review → confirm twice → verify.
Architecture, schema, auth, RBAC, audit logging and business logic are preserved; the schema gained only a `PRAGMA user_version`.

## Files created
`DEPLOYMENT_AUDIT.md`, `RENDER_DEPLOYMENT.md`, `BACKUP_RESTORE_GUIDE.md`, `IMPLEMENTATION_REPORT.md`, `render_start.sh`, `render.yaml`,
`.gitattributes`, `hrms/version.py`, `hrms/core/maintenance.py`, `hrms/services/backup.py`, `hrms/modules/admin/backup.py`,
`hrms/templates/admin/backup_restore.html`, `hrms/templates/admin/restore_confirm.html`, `tests/test_backup_restore.py`.

## Files modified
`requirements.txt`, `.gitignore`, `README.md`, `hrms/config.py`, `hrms/__init__.py` (`/healthz`, debug off), `hrms/cli.py`
(`render-init`, `backup-full`, `restore-backup`, `create_admin_user`), `hrms/core/db.py` (schema version), `hrms/core/audit.py`
(`verify_rows`), `hrms/core/security.py` (maintenance gate, per-endpoint upload limit, public `/healthz`), `hrms/core/utils.py`,
`hrms/modules/admin/__init__.py`, templates `base.html` / `backups.html` / `settings.html`, `hrms/static/js/app.js`,
`tests/test_robustness.py` (sample value for the new `token` route parameter).
Existing features were not removed (database-only backup/download remain).

## Features
* Full System Backup ZIP (consistent SQLite online-backup snapshot, uploads, `manifest.json` with SHA-256/sizes/schema/commit); no secrets or source.
* Upload & Restore: full validation (names, symlinks, duplicates, size/zip-bomb, manifest, SHA-256, `integrity_check`, required tables, schema/format compatibility, active admin present), summary page, checkbox + typed `RESTORE` + browser confirm, safety backup, DB + uploads replaced, migrations, live checks, audit entries, automatic rollback, all users signed out, maintenance 503 during restore.
* Admin-only (`settings.manage`), CSRF, audited. Real system status panel and backup history (with the ephemeral-storage explanation).
* Render: `/healthz`, `render-init` (creates first admin only on an empty DB; never seeds demo into a used DB), secure cookies and proxy trust default on, `SECRET_KEY` preserved by restore.
* CLI: `backup-full`, `restore-backup`, `render-init`.

## Tests and results
* New: `tests/test_backup_restore.py` — 43 tests, all pass. They cover: backup creation, SQLite consistency, ZIP integrity, manifest and SHA-256 validation, download/restore permissions, invalid archives, path traversal/symlinks/zip-bombs, DB integrity after restore, upload restoration, rollback on failure (including inside DB replacement), older-schema migration, newer-schema rejection, secret-key preservation, empty DB, repeated deployments, maintenance mode, >6 MB upload, CLI round trip, `render-init`, health check, production settings. Fictional data only.
* Full suite: 118 tests, 9 skipped, **1 failure**: `test_robustness.RandomWalk.test_hundreds_of_random_actions_keep_everything_consistent` (`4 not greater than 5`). It fails identically on the original code (baseline run before my changes) and is unrelated to this work.
* Two bugs found by the tests and fixed: rollback not triggered when the DB replacement itself failed; `PageSweep` needed a sample for the new route parameter.

## Render configuration
Runtime Python 3.12.8 · branch `main` · plan Free · build `pip install -r requirements.txt` · start `bash render_start.sh` · health `/healthz`.
Gunicorn: `--bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`.

## Environment variables
`HRMS_SECRET_KEY` (required), `HRMS_ADMIN_PASSWORD` (recommended; empty-DB bootstrap only), `HRMS_ADMIN_USERNAME/NAME/EMP_CODE`, `HRMS_SECURE_COOKIES`, `HRMS_TRUST_PROXY`, `HRMS_SEED_DEMO` (opt-in), `HRMS_INSTANCE_DIR`, `HRMS_DATABASE`, `HRMS_SESSION_IDLE_MINUTES`, `HRMS_RESTORE_MAX_UPLOAD_MB`, `HRMS_RESTORE_MAX_UNCOMPRESSED_MB`; provided by Render: `PORT`, `RENDER`, `RENDER_GIT_COMMIT`, `RENDER_INSTANCE_ID`.

## Not verified / remaining limitations
* **Gunicorn itself was not run** (development machine is Windows). `render_start.sh`'s `render-init` step and the app factory were exercised; the Gunicorn command line was reviewed but not executed. First real Render deploy is the proof — check its log and `/healthz`.
* Render Free wipes data on deploy, restart and idle spin-down; only downloaded ZIPs persist. Server-side backup history is erased with the disk.
* Restore replaces everything (no merge). Very large backups are bounded by Render Free memory and request time (default cap 200 MB).
* SQLite single writer; one worker. No scheduled off-site backups on Free.
* Backups hold personal data and password hashes; the ZIP is not encrypted — store it securely.
* `deploy/PYTHONANYWHERE.md` refers to a missing `deploy/mitra-hrms.zip` (pre-existing, untouched).
* Git: the implementation is in commit `3ed7108` ("Render_sh"); the docs and the two test edits from this session are uncommitted. Nothing was pushed by me.

## Exact deploy instructions
1. Local: `python -m unittest discover -s tests`; confirm `git status` shows no `instance/`, `*.db`, `.env` or backup ZIP.
2. Existing deployment: log in as admin → Backup & Restore → **Download Full System Backup**; keep the ZIP.
3. `git add -A && git commit && git push origin main`.
4. First time only: Render → New → Blueprint → pick the repo; set `HRMS_ADMIN_PASSWORD` when prompted.
5. Wait for the deploy; open `/healthz` (expect `{"status":"ok"}`), then log in as `admin`.
6. Backup & Restore → **Upload & restore a backup** → choose the ZIP → review the summary → tick the box, type `RESTORE`, confirm.
7. Sign in with an account from the backup; verify counts, an uploaded document, and Audit log → Verify chain.
