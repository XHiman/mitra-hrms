"""Bulk staff onboarding from the MITRA Staff Onboarding Excel template.

The whole file is validated before anything is written, and the import runs in one
transaction. Staff already in the system (matched on Employee ID) are updated in place;
their passwords are not touched. New staff get a one-time password that must be changed
at first login.
"""
import re
from datetime import date, datetime

from ..core import audit
from ..core.db import transaction
from ..core.security import hash_password, temporary_password
from ..core.utils import employment_types, utcnow_iso

ROLE_NAMES = {
    "employee": "EMPLOYEE",
    "reporting manager": "REPORTING_MANAGER",
    "hr": "HR",
    "joint secretary": "JOINT_SECRETARY",
    "joint ceo": "JOINT_CEO",
    "ceo": "CEO",
    "admin": "SYSADMIN",
}
COLUMNS = ["emp_code", "full_name", "designation", "department", "role", "reporting_officer", "email", "phone",
           "joining_date", "employment_type", "username", "date_of_birth"]
HONORIFICS = {"shri", "smt", "mr", "mrs", "ms", "dr", "kum"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")


class ImportProblems(Exception):
    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = problems


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def _date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(_text(value), fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(_text(value))


def read_sheet(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb["Staff List"] if "Staff List" in wb.sheetnames else wb.worksheets[0]
    rows = []
    for line, values in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        values = list(values[:len(COLUMNS)]) + [None] * (len(COLUMNS) - len(values[:len(COLUMNS)]))
        row = dict(zip(COLUMNS, values))
        code = _text(row["emp_code"])
        if not any(_text(v) for v in values) or code.upper().startswith("EXAMPLE"):
            continue
        row["line"] = line
        rows.append(row)
    wb.close()
    return rows


def make_username(full_name, taken):
    parts = [re.sub(r"[^a-z]", "", p) for p in re.split(r"[\s.]+", full_name.lower())]
    parts = [p for p in parts if p and p not in HONORIFICS]
    if not parts:
        base = "user"
    elif len(parts) == 1:
        base = parts[0]
    else:
        base = f"{parts[0]}.{parts[-1]}"
    base = base[:36] if len(base) >= 3 else (base + "user")[:36]
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base}{n}", n + 1
    taken.add(name.lower())
    return name


def validate(conn, rows):
    """Returns (records, warnings). Raises ImportProblems listing every error found."""
    problems, warnings = [], []
    existing = {r["emp_code"]: r for r in conn.execute(
        "SELECT e.id, e.emp_code, u.id AS user_id, u.username FROM employees e LEFT JOIN users u ON u.employee_id = e.id")}
    taken = {r["username"].lower() for r in conn.execute("SELECT username FROM users")}
    allowed_types = set(employment_types())
    seen, records = {}, []

    for row in rows:
        where = f"Row {row['line']}"
        rec = {k: _text(row[k]) for k in COLUMNS}
        rec["line"] = row["line"]
        if not rec["emp_code"]:
            problems.append(f"{where}: Employee ID is missing.")
            continue
        where += f" ({rec['emp_code']})"
        if rec["emp_code"] in seen:
            problems.append(f"{where}: Employee ID is repeated (also row {seen[rec['emp_code']]}).")
        seen[rec["emp_code"]] = row["line"]
        if not rec["full_name"]:
            problems.append(f"{where}: Full Name is missing.")
        if not rec["designation"]:
            warnings.append(f"{where}: no designation.")
        if not rec["department"]:
            warnings.append(f"{where}: no department / team.")
        if not rec["role"]:
            warnings.append(f"{where}: no role given, set to Employee.")
            rec["role"] = "Employee"
        rec["role_code"] = ROLE_NAMES.get(rec["role"].lower())
        if rec["role_code"] is None:
            problems.append(f"{where}: unknown role '{rec['role']}'.")
        if rec["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", rec["email"]):
            problems.append(f"{where}: email '{rec['email']}' is not valid.")
        try:
            rec["joining_date"] = _date(row["joining_date"])
        except ValueError as exc:
            problems.append(f"{where}: joining date '{exc}' is not a date (use DD-MM-YYYY).")
        try:
            rec["date_of_birth"] = _date(row["date_of_birth"])
        except ValueError as exc:
            problems.append(f"{where}: date of birth '{exc}' is not a date (use DD-MM-YYYY).")
        if rec["employment_type"] and rec["employment_type"] not in allowed_types:
            problems.append(f"{where}: employment type '{rec['employment_type']}' is not one of "
                            f"{', '.join(sorted(allowed_types))}.")
        rec["employment_type"] = rec["employment_type"] or None
        rec["existing"] = existing.get(rec["emp_code"])
        records.append(rec)

    codes = {r["emp_code"] for r in records} | set(existing)
    by_code = {r["emp_code"]: r for r in records}
    for rec in records:
        where = f"Row {rec['line']} ({rec['emp_code']})"
        ro = rec["reporting_officer"]
        if not ro:
            if rec["role_code"] != "CEO":
                warnings.append(f"{where}: no reporting officer; leave requests will go to HR.")
        elif ro == rec["emp_code"]:
            problems.append(f"{where}: cannot report to themselves.")
        elif ro not in codes:
            problems.append(f"{where}: reporting officer '{ro}' is not in the sheet or the system.")
    for rec in records:  # circular reporting lines within the sheet
        chain, cur = [], rec["emp_code"]
        while cur in by_code and by_code[cur]["reporting_officer"]:
            if cur in chain:
                problems.append(f"Circular reporting line: {' -> '.join(chain[chain.index(cur):] + [cur])}.")
                break
            chain.append(cur)
            cur = by_code[cur]["reporting_officer"]

    for rec in records:  # usernames last, so preferred names are reserved before generated ones
        if rec["existing"] and rec["existing"]["username"]:
            rec["username"] = rec["existing"]["username"]
        elif rec["username"]:
            if not USERNAME_RE.match(rec["username"]):
                problems.append(f"Row {rec['line']} ({rec['emp_code']}): username '{rec['username']}' may only use "
                                "letters, digits, '.', '_' or '-' (3-40 characters).")
            elif rec["username"].lower() in taken:
                problems.append(f"Row {rec['line']} ({rec['emp_code']}): username '{rec['username']}' is already taken.")
            taken.add(rec["username"].lower())
    for rec in records:
        if not rec["username"]:
            rec["username"] = make_username(rec["full_name"] or rec["emp_code"], taken)

    if problems:
        raise ImportProblems(problems)
    return records, warnings


def credential_slips(created, login_url, org_name):
    """Printable A4 sheet of cut-out slips (one per new user). Contains passwords: confidential."""
    from html import escape
    slips = "\n".join(f"""
  <div class="slip">
    <div class="org">{escape(org_name)} HRMS: login details</div>
    <table>
      <tr><th>Name</th><td>{escape(c['full_name'])}</td></tr>
      <tr><th>Employee ID</th><td>{escape(c['emp_code'])}</td></tr>
      <tr><th>Designation</th><td>{escape(c['designation'] or '')}</td></tr>
      <tr><th>Username</th><td class="mono">{escape(c['username'])}</td></tr>
      <tr><th>Temporary password</th><td class="mono">{escape(c['password'])}</td></tr>
      <tr><th>Login at</th><td>{escape(login_url)}</td></tr>
    </table>
    <p>You will be asked to set your own password the first time you log in. Do not share this slip.</p>
  </div>""" for c in created)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(org_name)} HRMS login details (confidential)</title>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 12px; margin: 12mm; color: #000; }}
  h1 {{ font-size: 15px; margin: 0 0 4px; }}
  .note {{ margin: 0 0 10px; color: #444; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0; }}
  .slip {{ border: 1px dashed #777; padding: 8px 10px; break-inside: avoid; page-break-inside: avoid; }}
  .org {{ font-weight: bold; margin-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ text-align: left; font-weight: normal; color: #444; width: 42%; padding: 1px 0; vertical-align: top; }}
  td {{ padding: 1px 0; }}
  .mono {{ font-family: Consolas, "Courier New", monospace; font-size: 13px; font-weight: bold; }}
  p {{ margin: 5px 0 0; font-size: 11px; }}
  @media print {{ .note {{ display: none; }} body {{ margin: 8mm; }} }}
</style></head><body>
<h1>{escape(org_name)} HRMS: login details for {len(created)} staff (confidential)</h1>
<p class="note">Print, cut along the dashed lines and hand each slip to the person named on it.
Delete this file after printing.</p>
<div class="grid">{slips}
</div></body></html>"""


def _lookup_id(conn, table, name, cache):
    if not name:
        return None
    key = name.lower()
    if key not in cache:
        row = conn.execute(f"SELECT id FROM {table} WHERE lower(name) = ?", (key,)).fetchone()
        cache[key] = row["id"] if row else conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid
    return cache[key]


def run(path, dry_run=False):
    """Validate and import. Returns dict(created=[...credentials], updated=n, warnings=[...])."""
    rows = read_sheet(path)
    if not rows:
        raise ImportProblems(["The sheet has no staff rows."])
    with transaction() as conn:
        records, warnings = validate(conn, rows)
        if dry_run:
            return {"created": [], "updated": 0, "warnings": warnings, "records": records}
        roles = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM roles")}
        depts, desigs, now = {}, {}, utcnow_iso()
        created, updated, emp_ids = [], 0, {}
        for rec in records:
            fields = (rec["full_name"], rec["email"] or None, rec["phone"] or None,
                      _lookup_id(conn, "designations", rec["designation"], desigs),
                      _lookup_id(conn, "departments", rec["department"], depts),
                      rec["joining_date"], rec["employment_type"])
            if rec["existing"]:
                emp_id = rec["existing"]["id"]
                conn.execute("""UPDATE employees SET full_name = ?, email = ?, phone = ?, designation_id = ?,
                                department_id = ?, joining_date = ?, employment_type = ?, updated_at = ? WHERE id = ?""",
                             fields + (now, emp_id))
                if rec["existing"]["user_id"]:
                    conn.execute("""UPDATE users SET role_id = ?, session_version = session_version +
                                    CASE WHEN role_id != ? THEN 1 ELSE 0 END WHERE id = ?""",
                                 (roles[rec["role_code"]], roles[rec["role_code"]], rec["existing"]["user_id"]))
                if rec["date_of_birth"]:  # a blank cell never erases a date the employee entered themselves
                    conn.execute("UPDATE employees SET date_of_birth = ? WHERE id = ?", (rec["date_of_birth"], emp_id))
                audit.log("USER_UPDATED", "employee", emp_id, new={"emp_code": rec["emp_code"], "role": rec["role_code"]},
                          remarks="Updated by staff import")
                updated += 1
            else:
                emp_id = conn.execute(
                    """INSERT INTO employees (emp_code, full_name, email, phone, designation_id, department_id,
                                              joining_date, employment_type, date_of_birth, status, created_at,
                                              updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)""",
                    (rec["emp_code"],) + fields + (rec["date_of_birth"], now, now)).lastrowid
                password = temporary_password()
                uid = conn.execute(
                    """INSERT INTO users (employee_id, username, password_hash, role_id, must_change_password,
                                          password_changed_at, created_at) VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (emp_id, rec["username"], hash_password(password), roles[rec["role_code"]], now, now)).lastrowid
                audit.log("USER_CREATED", "user", uid, new={"username": rec["username"], "emp_code": rec["emp_code"],
                                                            "role": rec["role_code"]}, remarks="Created by staff import")
                created.append({**rec, "password": password})
            emp_ids[rec["emp_code"]] = emp_id
        all_ids = {r["emp_code"]: r["id"] for r in conn.execute("SELECT id, emp_code FROM employees")}
        for rec in records:  # second pass: reporting officers may appear later in the sheet
            conn.execute("UPDATE employees SET reporting_officer_id = ? WHERE id = ?",
                         (all_ids.get(rec["reporting_officer"]) if rec["reporting_officer"] else None,
                          emp_ids[rec["emp_code"]]))
        if not conn.execute("""SELECT 1 FROM users u JOIN role_permissions rp ON rp.role_id = u.role_id
                               WHERE u.is_active = 1 AND rp.permission_code = 'roles.manage'""").fetchone():
            raise ImportProblems(["After this import nobody would hold the Admin role. Give at least one person 'Admin'."])
        audit.log("STAFF_IMPORTED", "system", None, new={"created": len(created), "updated": updated},
                  remarks=f"Staff import from {path.name if hasattr(path, 'name') else path}")
    return {"created": created, "updated": updated, "warnings": warnings, "records": records}
