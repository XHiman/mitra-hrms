# Backup & Restore Guide

Available to **System Administrators only** (permission `settings.manage`) at
*Settings and backups → Backup & Restore*. All actions are audit-logged.

## What a backup contains

A single ZIP, `hrms-backup-<UTC timestamp>.zip`:

```
manifest.json            app name, format version, created_at, created_by, app version, git commit,
                         database filename, schema version, row counts, every file with size + SHA-256
database/hrms.db         consistent snapshot made with SQLite's online backup API (WAL included)
uploads/<files>          uploaded documents
```

It does **not** contain: `.env`, `HRMS_SECRET_KEY`/`instance/secret_key`, API keys, GitHub/Render credentials,
temp files, virtualenvs or source code. It **does** contain personal data and password hashes — store it
privately, ideally encrypted, and never commit it to Git.

## Download a backup

1. Backup & Restore → **Download Full System Backup**.
2. The snapshot is integrity-checked before packaging; the browser downloads the ZIP.
3. Check the size is plausible and keep a copy outside the project.

Backup history lists date/time, type, status, size and who made it. "Download (on this server)" appears only while
the server still holds the temporary copy; on Render Free this disappears at the next restart/deploy, and the
history list goes with it. **The file in your Downloads folder is the backup.**

CLI: `flask --app run backup-full` (writes to the server's backup folder).

## Restore a backup

1. Backup & Restore → **Upload & restore a backup** → choose the ZIP → *Upload and check backup*.
2. The ZIP is validated completely **before anything changes**:
   safe entry names only (no absolute paths, `..`, drive letters, backslashes, symlinks, duplicates, executables);
   size and compression-ratio limits; manifest present and well-formed; every file listed and present with the
   right size and SHA-256; `PRAGMA integrity_check` and required tables in the database; schema version not newer
   than this server; the backup contains an active administrator who can manage roles; audit-chain check (warning only).
3. A summary shows what was checked, what the backup contains (users, employees, leave, timesheets, salary slips,
   audit entries, uploads) and what the server currently holds.
4. Tick *I understand…* and type `RESTORE`, then confirm the browser dialog (second explicit confirmation).
5. The system: blocks other requests (503, health check stays up) → makes a **safety backup** of the current data →
   replaces the database and uploads → runs migrations if the backup is older → verifies the result → writes an audit
   entry → signs everyone out.
6. **If anything fails**, the database and uploads are rolled back from the safety copy and a
   `BACKUP_RESTORE_FAILED` audit entry is written. There is no partial restore.

An uploaded ZIP waits at most 30 minutes for confirmation and can only be confirmed by the administrator who uploaded it.

CLI: `flask --app run restore-backup <zip>` (asks for confirmation; `--yes` to skip).

## Compatibility rules

| Backup | Result |
|---|---|
| Same schema version | Restored as is. |
| Older schema version | Restored, then upgraded by the normal migrations. |
| Newer schema version than the server | **Rejected** (never silently downgraded). |
| Newer backup format version | Rejected. |
| Modified/corrupt content, wrong checksum, unsafe names | Rejected with a message naming the problem. |

## What a restore never changes

Source code, `HRMS_SECRET_KEY` and other environment configuration, Render/GitHub credentials.

## Recommended routine

* Before every push/deploy: download a backup.
* After every deploy on Render Free: restore it (the new instance is empty).
* While in use: download a backup after significant changes, and at least weekly. Keep several generations.
* Test a restore into a scratch deployment occasionally.

## Troubleshooting

| Message | Meaning / action |
|---|---|
| "…is larger than the manifest says" / "…mismatch" | File was altered or truncated after download; re-download or use an earlier backup. |
| "newer schema" | Backup was made by a newer version; deploy that version first. |
| "no active administrator" | The backup would lock everyone out; use another backup. |
| Upload rejected immediately | Exceeds `HRMS_RESTORE_MAX_UPLOAD_MB`. |
| 503 "maintenance" | A restore is in progress; retry in a minute. A stale lock clears after 60 minutes. |
