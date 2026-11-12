"""Command-line administration: schema setup, first admin, demo data, backups.

  flask --app run init-db
  flask --app run create-admin --username admin --name "System Administrator" --emp-code SYS001
  flask --app run seed-demo
  flask --app run backup-db
"""
from datetime import datetime
from pathlib import Path

import click
from flask import current_app

from .core import audit
from .core.db import backup_to, init_schema, query, transaction
from .core.rbac import DEFAULT_ROLES, GRANTED_ON_UPGRADE, INHERITED_ON_UPGRADE, PERMISSIONS, sod_violations
from .core.security import hash_password, temporary_password
from .core.utils import DEFAULT_SETTINGS, get_int_setting, utcnow_iso

DEMO_PASSWORD = "Mitra@2026"


def bootstrap():
    """Idempotent: schema, permission catalogue, default roles/settings, default workflow."""
    init_schema(current_app.config["DATABASE"])
    with transaction() as conn:
        known = {r["code"] for r in conn.execute("SELECT code FROM permissions")}
        for code, module, desc in PERMISSIONS:
            if known and code not in known and code in INHERITED_ON_UPGRADE:
                conn.execute("""INSERT INTO permissions (code, module, description) VALUES (?, ?, ?)""",
                             (code, module, desc))
                conn.execute("""INSERT OR IGNORE INTO role_permissions (role_id, permission_code)
                                SELECT role_id, ? FROM role_permissions WHERE permission_code = ?""",
                             (code, INHERITED_ON_UPGRADE[code]))
                continue
            if known and code not in known and code in GRANTED_ON_UPGRADE:
                conn.execute("""INSERT INTO permissions (code, module, description) VALUES (?, ?, ?)""",
                             (code, module, desc))
                for role_code in GRANTED_ON_UPGRADE[code]:
                    conn.execute("""INSERT OR IGNORE INTO role_permissions (role_id, permission_code)
                                    SELECT id, ? FROM roles WHERE code = ?""", (code, role_code))
                continue
            conn.execute("INSERT INTO permissions (code, module, description) VALUES (?, ?, ?) "
                         "ON CONFLICT(code) DO UPDATE SET module = excluded.module, description = excluded.description",
                         (code, module, desc))
        for code, name, desc, perms in DEFAULT_ROLES:
            assert not sod_violations(perms), code
            if conn.execute("SELECT 1 FROM roles WHERE code = ?", (code,)).fetchone():
                continue
            role_id = conn.execute("INSERT INTO roles (code, name, description, is_system) VALUES (?, ?, ?, 1)",
                                   (code, name, desc)).lastrowid
            for p in perms:
                conn.execute("INSERT INTO role_permissions (role_id, permission_code) VALUES (?, ?)", (role_id, p))
        for key, (value, desc) in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)", (key, value, desc))
        if not conn.execute("SELECT 1 FROM approval_workflows").fetchone():
            hr = conn.execute("SELECT id FROM roles WHERE code = 'HR'").fetchone()["id"]
            wf = conn.execute("INSERT INTO approval_workflows (name, description, is_default) VALUES (?, ?, 1)",
                              ("Standard", "Reporting officer approves; HR acts if no reporting officer can approve.")
                              ).lastrowid
            conn.execute("""INSERT INTO workflow_stages (workflow_id, seq, name, approver_type, fallback_role_id)
                            VALUES (?, 1, 'Reporting Officer', 'REPORTING_OFFICER', ?)""", (wf, hr))
        # Compensatory off: its balance comes only from approved claims, so no entitlement is assumed.
        if not conn.execute("SELECT 1 FROM leave_types WHERE credit_based = 1").fetchone() and \
                not conn.execute("SELECT 1 FROM leave_types WHERE code = 'CO' OR name = 'Compensatory Off'").fetchone():
            now = utcnow_iso()
            conn.execute("""INSERT INTO leave_types (code, name, description, balance_tracked, credit_based,
                                                     created_at, updated_at) VALUES ('CO', 'Compensatory Off', ?, 1, 1, ?, ?)""",
                         ("Earned by working on a weekly off or holiday at an officer's direction; "
                          "credited when that officer approves the comp-off claim.", now, now))


def _insert_user(conn, emp_id, username, role_id, password, must_change, now):
    return conn.execute(
        """INSERT INTO users (employee_id, username, password_hash, role_id, must_change_password,
                              password_changed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (emp_id, username, hash_password(password), role_id, must_change, now, now)).lastrowid


def register_cli(app):
    @app.cli.command("init-db")
    def init_db_cmd():
        """Create tables, permissions, default roles, settings and default workflow."""
        bootstrap()
        click.echo("Database initialised.")

    @app.cli.command("create-admin")
    @click.option("--username", required=True)
    @click.option("--name", required=True)
    @click.option("--emp-code", required=True)
    def create_admin_cmd(username, name, emp_code):
        """Create a System Administrator account with a one-time password."""
        bootstrap()
        if query("SELECT 1 FROM users WHERE username = ?", (username,), one=True):
            raise click.ClickException("Username already exists.")
        password = temporary_password()
        with transaction() as conn:
            now = utcnow_iso()
            emp_id = conn.execute(
                """INSERT INTO employees (emp_code, full_name, status, created_at, updated_at)
                   VALUES (?, ?, 'ACTIVE', ?, ?)""", (emp_code, name, now, now)).lastrowid
            role_id = conn.execute("SELECT id FROM roles WHERE code = 'SYSADMIN'").fetchone()["id"]
            uid = _insert_user(conn, emp_id, username, role_id, password, 1, now)
            audit.log("USER_CREATED", "user", uid, new={"username": username, "role": "SYSADMIN"},
                      remarks="Created via CLI")
        click.echo(f"Administrator '{username}' created. One-time password: {password}")
        click.echo("The password must be changed at first login.")

    @app.cli.command("seed-demo")
    def seed_demo_cmd():
        """Load a small fictional organisation for the pilot walkthrough.

        Leave types are created WITHOUT entitlements – those must be configured by the
        administrator from MITRA's approved HR policy.
        """
        bootstrap()
        if query("SELECT 1 FROM users", one=True):
            raise click.ClickException("Users already exist; demo data is only loaded into an empty database.")
        with transaction() as conn:
            for name, code in [("Office of the CEO", "CEO"), ("Administration & HR", "ADM"),
                               ("Economy & Finance", "ECO"), ("Infrastructure", "INF"), ("IT Cell", "IT")]:
                conn.execute("INSERT INTO departments (name, code) VALUES (?, ?)", (name, code))
            for lvl, name in enumerate(["Chief Executive Officer", "Joint CEO", "Joint Secretary", "HR Manager",
                                        "Senior Consultant", "Associate", "System Administrator"], start=1):
                conn.execute("INSERT INTO designations (name, level) VALUES (?, ?)", (name, lvl))

            people = {}

            def add(key, code, name, role, dept, desig, ro=None, etype="Permanent"):
                people[key] = _person(conn, code, key, name, role, dept, desig,
                                      people[ro][0] if ro else None, etype)

            add("ceo", "M001", "Ananya Rao", "CEO", "Office of the CEO", "Chief Executive Officer")
            add("jtceo", "M002", "Vikram Deshmukh", "JOINT_CEO", "Office of the CEO", "Joint CEO", "ceo")
            add("js", "M003", "Meera Kulkarni", "JOINT_SECRETARY", "Administration & HR", "Joint Secretary", "ceo")
            add("hr", "M004", "Sunita Patil", "HR", "Administration & HR", "HR Manager", "js")
            add("rahul", "M101", "Rahul Joshi", "EMPLOYEE", "Economy & Finance", "Senior Consultant", "jtceo")
            add("priya", "M102", "Priya Nair", "EMPLOYEE", "Infrastructure", "Associate", "jtceo", "Contractual")
            add("amit", "M103", "Amit Shinde", "EMPLOYEE", "Administration & HR", "Associate", "js")
            add("kavita", "M104", "Kavita Pawar", "EMPLOYEE", "Economy & Finance", "Associate", "jtceo", "Consultant")
            add("admin", "M900", "Dev Admin", "SYSADMIN", "IT Cell", "System Administrator", "js")

            now = utcnow_iso()
            for code, name, desc in [
                ("CL", "Casual Leave", "Entitlement and rules to be configured as per MITRA HR policy."),
                ("SL", "Sick Leave", "Entitlement and rules to be configured as per MITRA HR policy."),
                ("EL", "Earned / Privilege Leave", "Entitlement and rules to be configured as per MITRA HR policy."),
            ]:
                conn.execute("""INSERT INTO leave_types (code, name, description, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?)""", (code, name, desc, now, now))
            for name in ("Sector research", "MURWEP", "MRDP", "Administration"):
                conn.execute("INSERT INTO timesheet_projects (name) VALUES (?)", (name,))
            audit.log("DEMO_DATA_LOADED", "system", None, remarks="Fictional demo organisation loaded via CLI")
        click.echo("Demo organisation loaded. All demo accounts use the password: " + DEMO_PASSWORD)
        click.echo("Usernames: ceo, jtceo, js, hr, rahul, priya, amit, kavita, admin")
        click.echo("NOTE: leave entitlements are intentionally blank. Log in as 'admin' and configure them.")

    @app.cli.command("import-staff")
    @click.argument("xlsx", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option("--dry-run", is_flag=True, help="Only check the sheet; change nothing.")
    @click.option("--login-url", default="http://127.0.0.1:5000", show_default=True,
                  help="Address printed on the credential slips.")
    def import_staff_cmd(xlsx, dry_run, login_url):
        """Create or update staff accounts from the Staff Onboarding Excel sheet."""
        from .services import staff_import
        bootstrap()
        try:
            result = staff_import.run(xlsx, dry_run=dry_run)
        except staff_import.ImportProblems as exc:
            click.echo(f"Nothing was imported. Fix these {len(exc.problems)} problem(s) in the sheet:")
            for p in exc.problems:
                click.echo(f"  - {p}")
            raise SystemExit(1)
        for w in result["warnings"]:
            click.echo(f"  note: {w}")
        if dry_run:
            new = sum(1 for r in result["records"] if not r["existing"])
            click.echo(f"Sheet OK: {new} new, {len(result['records']) - new} existing. Run again without --dry-run to import.")
            return
        click.echo(f"Imported: {len(result['created'])} created, {result['updated']} updated.")
        if result["created"]:
            out = Path(current_app.config["INSTANCE_DIR"]) / f"login-slips-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
            out.write_text(staff_import.credential_slips(result["created"], login_url, current_app.config["ORG_NAME"]),
                           encoding="utf-8")
            click.echo(f"Login slips (confidential, print then delete): {out}")

    @app.cli.command("import-datasheet")
    @click.argument("xlsx", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option("--dry-run", is_flag=True, help="Only report what would change.")
    @click.option("--shared-password", default=None, help="Give new staff this password instead of one-time passwords.")
    def import_datasheet_cmd(xlsx, dry_run, shared_password):
        """Joining dates, contract renewals, birthdays and new staff from HR's employee datasheet."""
        from .services import datasheet_import, staff_import
        from .services.leave import rebuild_balances
        bootstrap()
        plan, notes, changes = datasheet_import.run(xlsx, dry_run=dry_run, shared_password=shared_password)
        for n in notes:
            click.echo(f"  check: {n}")
        new = [i for i in plan.values() if i["existing"] is None]
        if dry_run:
            click.echo(f"Dry run: {len(plan) - len(new)} existing staff matched, {len(new)} new: "
                       + ", ".join(i["name"] for i in new))
            return
        rebuild_balances(remarks="Recalculated after the employee datasheet import")
        click.echo(f"Updated {len(changes['updated'])} staff, recorded {changes['renewals']} renewal dates, "
                   f"created {len(changes['created'])} new staff.")
        for c in changes["created"]:
            click.echo(f"  new: {c['emp_code']} {c['full_name']} -> {c['username']}")
        if changes["created"] and not shared_password:
            out = Path(current_app.config["INSTANCE_DIR"]) / f"login-slips-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
            out.write_text(staff_import.credential_slips(changes["created"], "http://127.0.0.1:5000",
                                                         current_app.config["ORG_NAME"]), encoding="utf-8")
            click.echo(f"Login slips (confidential): {out}")

    @app.cli.command("send-reminders")
    def send_reminders_cmd():
        """Send due contract-renewal reminders (schedule daily, e.g. with Windows Task Scheduler)."""
        from .services.contracts import send_reminders
        bootstrap()
        with current_app.test_request_context():  # links in notifications need a request context
            n = send_reminders()
        click.echo(f"Renewal reminders sent: {n}.")

    @app.cli.command("rebuild-balances")
    def rebuild_balances_cmd():
        """Re-assign leave to the correct leave cycles and recalculate every balance."""
        from .services.leave import rebuild_balances
        bootstrap()
        n = rebuild_balances(remarks="Recalculated from the command line")
        click.echo(f"Balances recalculated ({n} balance rows).")

    @app.cli.command("load-holidays")
    @click.argument("source", required=False)
    def load_holidays_cmd(source):
        """Load public holidays: a year with a bundled official list (e.g. 2026) or a CSV file (date,name,description)."""
        from .core.utils import ValidationError
        from .services import calendar_events as events
        bootstrap()
        files = events.bundled_holiday_files()
        if not source:
            click.echo("Bundled Government of Maharashtra lists: " + (", ".join(map(str, files)) or "none"))
            return
        path = files.get(int(source)) if source.isdigit() and len(source) == 4 else Path(source)
        if path is None or not Path(path).is_file():
            raise click.ClickException(f"No holiday list found for '{source}'.")
        try:
            added, updated = events.load_holidays(path, f"Holiday list loaded from {Path(path).name}")
        except ValidationError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Holidays: {added} added, {updated} updated.")

    @app.cli.command("backup-db")
    def backup_db_cmd():
        """Write a timestamped online backup and apply the retention policy."""
        path = run_backup()
        click.echo(f"Backup written to {path}")


def _person(conn, emp_code, username, name, role_code, dept, desig, ro, etype):
    now = utcnow_iso()
    dept_id = conn.execute("SELECT id FROM departments WHERE name = ?", (dept,)).fetchone()["id"]
    desig_id = conn.execute("SELECT id FROM designations WHERE name = ?", (desig,)).fetchone()["id"]
    emp_id = conn.execute(
        """INSERT INTO employees (emp_code, full_name, designation_id, department_id, reporting_officer_id,
                                  joining_date, employment_type, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, '2024-04-01', ?, 'ACTIVE', ?, ?)""",
        (emp_code, name, desig_id, dept_id, ro, etype, now, now)).lastrowid
    role_id = conn.execute("SELECT id FROM roles WHERE code = ?", (role_code,)).fetchone()["id"]
    uid = _insert_user(conn, emp_id, username, role_id, DEMO_PASSWORD, 0, now)
    return emp_id, uid


def run_backup():
    backup_dir = Path(current_app.config["BACKUP_DIR"])
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"hrms-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    backup_to(current_app.config["DATABASE"], str(dest))
    keep = max(get_int_setting("backup_retention"), 1)
    for old in sorted(backup_dir.glob("hrms-*.db"), reverse=True)[keep:]:
        old.unlink()
    audit.log("DATABASE_BACKUP", "system", None, new={"file": dest.name})
    return dest
