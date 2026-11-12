"""Import of HR's employee datasheet (sheets MITRA, SDA, MURWEP).

Matches people by name (titles such as Shri./Kum./Dr. ignored), or by mobile number when the
name cell is empty. For people already in the system it records the date of joining, contract
renewal dates, date of birth and mobile number; people not yet in the system are created. Dates in
the sheet are written in many ways (16.04.2026, 10..01.2026, '03.02.2026 - RO', 14/09/1997,
'27th November'), so every value that is unclear is reported rather than guessed.
"""
import re
from datetime import date, datetime

from ..core import audit
from ..core.db import transaction
from ..core.security import hash_password, temporary_password
from ..core.utils import today, utcnow_iso
from . import staff_import

SHEETS = {"MITRA": None, "SDA": "State Data Authority (SDA)", "MURWEP": "MURWEP"}
SENIOR_ROLES = {"CEO", "JOINT_CEO", "JOINT_SECRETARY"}  # government officers: calendar leave year, no joining date
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def norm_name(s):
    s = re.sub(r"\s+", " ", str(s or "")).strip().lower()
    s = re.sub(r"^(shri|smt|kum|dr|mr|mrs|ms)\.?\s*", "", s)
    s = re.sub(r"\bias\b", "", s)
    return re.sub(r"[^a-z]", "", s)


def display_name(raw):
    """'DR.PRAMOD SHINDE' -> 'Dr. Pramod Shinde'; other spellings are kept as written."""
    name = re.sub(r"\s+", " ", raw).strip()
    if name.upper() == name:
        name = re.sub(r"[A-Za-z]+", lambda m: m.group(0) if m.group(0) == "IAS" else m.group(0).capitalize(), name)
    return re.sub(r"^(shri|smt|kum|dr)\.\s*", lambda m: m.group(1).capitalize() + ". ", name, flags=re.I)


def parse_dates(value):
    """All dates in a cell, plus any leftover note text. Returns ([date, ...], note, partial_dob)."""
    if value in (None, ""):
        return [], None, None
    if isinstance(value, datetime):
        return [value.date()], None, None
    if isinstance(value, date):
        return [value], None, None
    text = str(value).strip()
    found = []
    for d, m, y in re.findall(r"(\d{1,2})\s*[./-]+\s*(\d{1,2})\s*[./-]+\s*(\d{4})", text):
        try:
            found.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    rest = re.sub(r"\d{1,2}\s*[./-]+\s*\d{1,2}\s*[./-]+\s*\d{4}", " ", text)
    partial = None
    if not found:
        m = re.match(r"^\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,})\.?\s*(\d{4})?\s*$", text)
        if m and m.group(2)[:3].lower() in MONTHS:
            d, mon, y = int(m.group(1)), MONTHS[m.group(2)[:3].lower()], m.group(3)
            if y:
                found.append(date(int(y), mon, d))
            else:
                partial = (mon, d)
            rest = ""
        m = re.match(r"^\s*(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]?\s*$", text)
        if m:
            partial, rest = (int(m.group(2)), int(m.group(1))), ""
    note = re.sub(r"^[\s\-–,]+|[\s\-–,]+$", "", re.sub(r"\s+", " ", rest)) or None
    return found, note, partial


def _rows(ws):
    header_row = None
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=5, values_only=True), 1):
        if any(str(v or "").strip().lower() == "name" for v in row):
            header_row, headers = i, [str(v or "").strip().lower() for v in row]
            break
    if header_row is None:
        return
    idx = {"renewals": []}
    for c, h in enumerate(headers):
        if h == "name":
            idx["name"] = c
        elif h.startswith("designation"):
            idx["designation"] = c
        elif h.startswith("sector"):
            idx["sector"] = c
        elif h in ("dob", "date of birth"):
            idx["dob"] = c
        elif h.startswith("contact"):
            idx["phone"] = c
        elif h.startswith("email"):
            idx["email"] = c
        elif h.startswith("date of joining"):
            idx["doj"] = c
        elif h.startswith("date of renewal") or h.startswith("remarks"):
            idx["renewals"].append(c)
    if "renewals" in idx and idx["renewals"]:  # an unlabelled column right after the renewals holds more dates
        last = max(idx["renewals"]) + 1
        if last < len(headers) and not headers[last]:
            idx["renewals"].append(last)
    for line, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), header_row + 1):
        get = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None  # noqa: E731
        rec = {"line": line, "name": str(get("name") or "").strip(), "designation": str(get("designation") or "").strip(),
               "sector": str(get("sector") or "").strip(), "dob": get("dob"), "phone": get("phone"),
               "email": str(get("email") or "").strip(), "doj": get("doj"),
               "renewals": [row[c] for c in idx["renewals"] if c < len(row)]}
        if rec["name"] or rec["phone"] or rec["designation"]:
            yield rec


def analyse(conn, path):
    """Read the workbook and work out every change, without writing. Returns (plan, notes)."""
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True, read_only=True)
    people = conn.execute("""SELECT e.*, r.code AS role_code FROM employees e LEFT JOIN users u ON u.employee_id = e.id
                             LEFT JOIN roles r ON r.id = u.role_id""").fetchall()
    by_name = {norm_name(p["full_name"]): p for p in people}
    by_phone = {re.sub(r"\D", "", str(p["phone"] or "")): p for p in people if p["phone"]}
    plan, notes, seen = {}, [], set()
    t = today()
    for sheet, dept in SHEETS.items():
        if sheet not in wb.sheetnames:
            continue
        for rec in _rows(wb[sheet]):
            where = f"{sheet} row {rec['line']}"
            phone = re.sub(r"\D", "", str(rec["phone"] or ""))
            p = by_name.get(norm_name(rec["name"])) if rec["name"] else None
            if p is None and phone and phone in by_phone:
                p = by_phone[phone]
                notes.append(f"{where}: no name in the sheet; matched to {p['full_name']} by mobile number.")
            if p is None and not rec["name"]:
                notes.append(f"{where}: skipped, no name ({rec['designation'] or 'no designation'}"
                             f"{', ' + rec['sector'] if rec['sector'] else ''}).")
                continue
            key = p["emp_code"] if p else f"NEW:{norm_name(rec['name'])}"
            item = plan.setdefault(key, {"existing": p, "name": rec["name"], "sheet": sheet, "designation": rec["designation"],
                                         "department": dept or rec["sector"], "doj": None, "renewals": {}, "dob": None,
                                         "phone": None, "email": None})
            seen.add(key)
            who = p["full_name"] if p else rec["name"]

            dojs, _, _ = parse_dates(rec["doj"])  # notes such as "- YP" next to the date are not needed
            if dojs:
                doj = dojs[0]
                senior = p is not None and p["role_code"] in SENIOR_ROLES
                if senior:
                    notes.append(f"{where}: {who}: joining date {doj:%d.%m.%Y} ({sheet}) not used; senior officers "
                                 "follow the calendar leave year.")
                elif item["doj"] and item["doj"] != doj:
                    notes.append(f"{where}: {who}: joining date {doj:%d.%m.%Y} differs from {item['doj']:%d.%m.%Y} "
                                 f"on the {item['sheet']} sheet; kept the {item['sheet']} sheet's date.")
                elif not item["doj"]:
                    item["doj"] = doj
                    if sheet != "MITRA" and p is not None:
                        notes.append(f"{where}: {who}: joining date {doj:%d.%m.%Y} taken from the {sheet} sheet.")
                    if doj > t:
                        notes.append(f"{where}: {who}: joining date {doj:%d.%m.%Y} is in the future.")
            elif rec["doj"] not in (None, ""):
                notes.append(f"{where}: {who}: joining date '{rec['doj']}' not understood.")

            order = []
            for cell in rec["renewals"]:
                ds, note, _ = parse_dates(cell)
                if not ds and cell not in (None, "") and note:
                    notes.append(f"{where}: {who}: note '{note}' kept with the latest renewal.")
                    item["pending_note"] = note
                for d in ds:
                    order.append(d)
                    item["renewals"][d] = note
            if order and order != sorted(order):
                notes.append(f"{where}: {who}: renewal dates are not in date order in the sheet "
                             f"({', '.join(f'{d:%d.%m.%Y}' for d in order)}); please check.")

            dobs, _, partial = parse_dates(rec["dob"])
            if dobs:
                item["dob"] = dobs[0]
            elif partial:
                item["dob"] = date(1900, partial[0], partial[1])  # year unknown; only day and month are ever shown
                notes.append(f"{where}: {who}: date of birth '{str(rec['dob']).strip()}' has no year; "
                             "saved as day and month only.")
            elif rec["dob"] not in (None, ""):
                notes.append(f"{where}: {who}: date of birth '{rec['dob']}' not understood.")
            if phone:
                item["phone"] = phone
            if rec["email"]:
                item["email"] = rec["email"]

    for key, item in plan.items():  # sanity checks on contract dates
        who = item["existing"]["full_name"] if item["existing"] else item["name"]
        doj = item["doj"] or (date.fromisoformat(item["existing"]["joining_date"])
                              if item["existing"] and item["existing"]["joining_date"] else None)
        if item.get("pending_note") and item["renewals"]:
            last = max(item["renewals"])
            item["renewals"][last] = ", ".join(x for x in (item["renewals"][last], item["pending_note"]) if x)
        if doj:
            item["renewals"].pop(doj, None)
            for d in [d for d in item["renewals"] if d < doj]:
                notes.append(f"{who}: renewal {d:%d.%m.%Y} is before the joining date {doj:%d.%m.%Y}; ignored.")
                item["renewals"].pop(d)
        marks = sorted(([doj] if doj else []) + list(item["renewals"]))
        for a, b in zip(marks, marks[1:]):
            if (b - a).days < 31:
                notes.append(f"{who}: contract dates {a:%d.%m.%Y} and {b:%d.%m.%Y} are less than a month apart; please check.")
        if item["renewals"] and not doj:
            notes.append(f"{who}: has renewal dates but no joining date; renewals saved, leave stays on the calendar year.")
        for d in item["renewals"]:
            if d > t.replace(year=t.year + 2):
                notes.append(f"{who}: renewal date {d:%d.%m.%Y} is more than two years ahead; please check.")
    wb.close()
    return plan, notes


def run(path, dry_run=False, shared_password=None):
    with transaction() as conn:
        plan, notes = analyse(conn, path)
        changes = {"updated": [], "created": [], "renewals": 0}
        if dry_run:
            return plan, notes, changes
        roles = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM roles")}
        taken = {r["username"].lower() for r in conn.execute("SELECT username FROM users")}
        codes = [int(m.group(1)) for (c,) in conn.execute("SELECT emp_code FROM employees")
                 for m in [re.match(r"^MT(\d+)$", c)] if m]
        next_code = max(codes, default=0) + 1
        depts, desigs, now = {}, {}, utcnow_iso()
        for key, item in plan.items():
            emp = item["existing"]
            if emp is None:
                code = f"MT{next_code:03d}"
                next_code += 1
                emp_id = conn.execute(
                    """INSERT INTO employees (emp_code, full_name, email, phone, designation_id, department_id, joining_date,
                                              date_of_birth, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)""",
                    (code, display_name(item["name"]), item["email"], item["phone"],
                     staff_import._lookup_id(conn, "designations", item["designation"] or None, desigs),
                     staff_import._lookup_id(conn, "departments", item["department"] or None, depts),
                     item["doj"].isoformat() if item["doj"] else None,
                     item["dob"].isoformat() if item["dob"] else None, now, now)).lastrowid
                username = staff_import.make_username(item["name"], taken)
                password = shared_password or temporary_password()
                uid = conn.execute(
                    """INSERT INTO users (employee_id, username, password_hash, role_id, must_change_password,
                                          password_changed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (emp_id, username, hash_password(password), roles["EMPLOYEE"], 0 if shared_password else 1,
                     now, now)).lastrowid
                audit.log("USER_CREATED", "user", uid, new={"username": username, "emp_code": code, "role": "EMPLOYEE"},
                          remarks=f"Created from employee datasheet ({item['sheet']} sheet)")
                changes["created"].append({"emp_code": code, "full_name": display_name(item["name"]), "username": username,
                                           "designation": item["designation"], "password": password,
                                           "employee_id": emp_id, "sheet": item["sheet"]})
            else:
                emp_id = emp["id"]
                upd = {}
                if item["doj"] and item["doj"].isoformat() != emp["joining_date"]:
                    upd["joining_date"] = item["doj"].isoformat()
                if item["dob"] and item["dob"].isoformat() != emp["date_of_birth"]:
                    upd["date_of_birth"] = item["dob"].isoformat()
                if item["phone"] and item["phone"] != (emp["phone"] or ""):
                    upd["phone"] = item["phone"]
                if item["email"] and item["email"] != (emp["email"] or ""):
                    upd["email"] = item["email"]
                if upd:
                    conn.execute(f"UPDATE employees SET {', '.join(k + ' = ?' for k in upd)}, updated_at = ? WHERE id = ?",
                                 list(upd.values()) + [now, emp_id])
                    audit.log("USER_UPDATED", "employee", emp_id, prev={k: emp[k] for k in upd},
                              new=upd, remarks="Updated from employee datasheet")
                    changes["updated"].append(emp["full_name"])
            for d, note in sorted(item["renewals"].items()):
                cur = conn.execute("""INSERT INTO contract_renewals (employee_id, renewal_date, note) VALUES (?, ?, ?)
                                      ON CONFLICT(employee_id, renewal_date) DO UPDATE SET note = excluded.note""",
                                   (emp_id, d.isoformat(), note))
                changes["renewals"] += cur.rowcount
        audit.log("DATASHEET_IMPORTED", "system", None,
                  new={"updated": len(changes["updated"]), "created": len(changes["created"]),
                       "renewal_dates": changes["renewals"]}, remarks=f"Employee datasheet {getattr(path, 'name', path)}")
    return plan, notes, changes
