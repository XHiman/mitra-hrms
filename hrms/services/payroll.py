"""Salary slips.

HR / Accounts keep a pay structure per employee (earning and deduction lines with amounts entered by
them; the system does not invent pay rules). Each month slips are generated from the structures as
drafts. Loss-of-pay days, if entered, reduce every earning in proportion to the days paid; any
amount can be corrected before the slip is issued. Issued slips are final: a mistake is corrected by
cancelling the slip and issuing a new one. Employees see only their own issued slips.

Amounts are kept in paise. Salary figures are deliberately not written to the audit log, which
system administrators can read.
"""
import calendar
import json
import re
from datetime import date

from flask import url_for

from ..core import audit
from ..core.db import query, transaction
from ..core.utils import ValidationError, clean_text, utcnow_iso
from .notifications import notify

MAX_LINES = 8


# ---------------------------------------------------------------- money
def to_paise(text, field):
    raw = (text or "").strip().replace(",", "").replace("₹", "").strip()
    if raw == "":
        return None
    if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", raw):
        raise ValidationError(f"{field}: enter an amount in rupees, e.g. 45000 or 45000.50.")
    rupees, _, paise = raw.partition(".")
    return int(rupees) * 100 + int((paise + "00")[:2])


def fmt_money(paise):
    """Indian digit grouping: 12,34,567.00"""
    paise = int(paise or 0)
    rupees, p = divmod(paise, 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = f"{head},{tail}"
    return f"{s}.{p:02d}"


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n):
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _three(n):
    h, r = divmod(n, 100)
    return " ".join(x for x in ((_ONES[h] + " Hundred") if h else "", _two(r) if r else "") if x)


def amount_in_words(paise):
    """'Rupees Forty-five Thousand Five Hundred and Fifty Paise Only' (Indian system: lakh, crore)."""
    rupees, p = divmod(int(paise or 0), 100)
    parts = []
    crore, rupees = divmod(rupees, 10_000_000)
    lakh, rupees = divmod(rupees, 100_000)
    thousand, rupees = divmod(rupees, 1000)
    for value, label in ((crore, "Crore"), (lakh, "Lakh"), (thousand, "Thousand")):
        if value:
            parts.append(f"{_three(value) if value >= 100 else _two(value)} {label}")
    if rupees:
        parts.append(_three(rupees))
    words = " ".join(parts) or "Zero"
    text = f"Rupees {words}"
    if p:
        text += f" and {_two(p)} Paise"
    return text + " Only"


# ---------------------------------------------------------------- helpers
def month_label(month):
    y, m = (int(x) for x in month.split("-"))
    return date(y, m, 1).strftime("%B %Y")


def valid_month(month):
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise ValidationError("Select a valid month.")
    y, m = (int(x) for x in month.split("-"))
    if not (1 <= m <= 12 and 2000 <= y <= 2100):
        raise ValidationError("Select a valid month.")
    return month


def _employee(conn, employee_id):
    return conn.execute(
        """SELECT e.*, d.name AS department, g.name AS designation, u.id AS user_id
             FROM employees e LEFT JOIN departments d ON d.id = e.department_id
             LEFT JOIN designations g ON g.id = e.designation_id LEFT JOIN users u ON u.employee_id = e.id
            WHERE e.id = ?""", (employee_id,)).fetchone()


def _guard_not_self(user, employee_id, action):
    if employee_id == user["employee_id"]:
        raise PermissionError(f"You cannot {action} your own salary. Another payroll officer must do it.")


def _read_lines(form, kind):
    lines = []
    prefix = "earning" if kind == "EARNING" else "deduction"
    for i in range(MAX_LINES):
        name = clean_text(form.get(f"{prefix}_name_{i}"), "Line name", max_len=80)
        amount = to_paise(form.get(f"{prefix}_amount_{i}"), name or f"{prefix.title()} {i + 1}")
        if not name and amount is None:
            continue
        if not name:
            raise ValidationError(f"Give a name for the {prefix} amount in row {i + 1}.")
        lines.append((kind, name, amount or 0))
    return lines


# ---------------------------------------------------------------- pay structures
def structure(employee_id):
    s = query("SELECT * FROM salary_structures WHERE employee_id = ?", (employee_id,), one=True)
    lines = query("SELECT * FROM salary_structure_lines WHERE structure_id = ? ORDER BY kind, seq", (s["id"],)) if s else []
    return s, lines


def save_structure(user, employee_id, form):
    _guard_not_self(user, employee_id, "change")
    lines = _read_lines(form, "EARNING") + _read_lines(form, "DEDUCTION")
    if not any(k == "EARNING" for k, _, _ in lines):
        raise ValidationError("Add at least one earning, e.g. Consolidated remuneration.")
    pan = clean_text(form.get("pan"), "PAN", max_len=10).upper() or None
    if pan and not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", pan):
        raise ValidationError("PAN should look like ABCDE1234F.")
    ifsc = clean_text(form.get("ifsc"), "IFSC", max_len=11).upper() or None
    if ifsc and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        raise ValidationError("IFSC should look like SBIN0001234.")
    account = clean_text(form.get("bank_account"), "Bank account number", max_len=20) or None
    if account and not re.fullmatch(r"\d{6,20}", account):
        raise ValidationError("Bank account number should contain digits only.")
    bank = clean_text(form.get("bank_name"), "Bank name", max_len=80) or None
    with transaction() as conn:
        if _employee(conn, employee_id) is None:
            raise ValidationError("Employee not found.")
        old = conn.execute("SELECT id FROM salary_structures WHERE employee_id = ?", (employee_id,)).fetchone()
        now = utcnow_iso()
        if old:
            sid = old["id"]
            conn.execute("""UPDATE salary_structures SET pan = ?, bank_name = ?, bank_account = ?, ifsc = ?,
                            updated_by = ?, updated_at = ? WHERE id = ?""", (pan, bank, account, ifsc, user["id"], now, sid))
            conn.execute("DELETE FROM salary_structure_lines WHERE structure_id = ?", (sid,))
        else:
            sid = conn.execute("""INSERT INTO salary_structures (employee_id, pan, bank_name, bank_account, ifsc,
                                  updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                               (employee_id, pan, bank, account, ifsc, user["id"], now)).lastrowid
        for seq, (kind, name, amount) in enumerate(lines):
            conn.execute("INSERT INTO salary_structure_lines (structure_id, kind, name, amount, seq) VALUES (?, ?, ?, ?, ?)",
                         (sid, kind, name, amount, seq))
        audit.log("PAY_STRUCTURE_SAVED", "employee", employee_id,
                  new={"earnings": [n for k, n, _ in lines if k == "EARNING"],
                       "deductions": [n for k, n, _ in lines if k == "DEDUCTION"]},
                  remarks="Amounts not logged (confidential)")


# ---------------------------------------------------------------- slips
def _totals(conn, slip_id):
    rows = conn.execute("SELECT kind, SUM(amount) AS s FROM salary_slip_lines WHERE slip_id = ? GROUP BY kind",
                        (slip_id,)).fetchall()
    t = {r["kind"]: r["s"] or 0 for r in rows}
    gross, ded = t.get("EARNING", 0), t.get("DEDUCTION", 0)
    if ded > gross:
        raise ValidationError("Deductions cannot be more than the gross earnings.")
    conn.execute("UPDATE salary_slips SET gross = ?, total_deductions = ?, net = ? WHERE id = ?",
                 (gross, ded, gross - ded, slip_id))


def _prorate(amount, paid_days, days):
    """Earning for the days paid, rounded to the nearest rupee."""
    return int(round(amount * paid_days / days / 100.0)) * 100


def _fill_from_structure(conn, slip_id, employee_id, lop_days, days):
    s = conn.execute("SELECT id FROM salary_structures WHERE employee_id = ?", (employee_id,)).fetchone()
    conn.execute("DELETE FROM salary_slip_lines WHERE slip_id = ?", (slip_id,))
    for line in conn.execute("SELECT * FROM salary_structure_lines WHERE structure_id = ? ORDER BY kind, seq",
                             (s["id"],)).fetchall():
        amount = line["amount"]
        if line["kind"] == "EARNING" and lop_days:
            amount = _prorate(amount, days - lop_days, days)
        conn.execute("INSERT INTO salary_slip_lines (slip_id, kind, name, amount, seq) VALUES (?, ?, ?, ?, ?)",
                     (slip_id, line["kind"], line["name"], amount, line["seq"]))
    _totals(conn, slip_id)


def generate(user, month, employee_ids=None):
    """Create draft slips for the month from pay structures. Skips anyone who already has a slip."""
    valid_month(month)
    y, m = (int(x) for x in month.split("-"))
    days = calendar.monthrange(y, m)[1]
    created, skipped = [], []
    with transaction() as conn:
        rows = conn.execute("""SELECT e.id, e.full_name FROM employees e JOIN salary_structures s ON s.employee_id = e.id
                               WHERE e.status = 'ACTIVE' ORDER BY e.full_name""").fetchall()
        for e in rows:
            if employee_ids is not None and e["id"] not in employee_ids:
                continue
            if e["id"] == user["employee_id"]:
                skipped.append(f"{e['full_name']} (your own slip: another payroll officer must prepare it)")
                continue
            if conn.execute("SELECT 1 FROM salary_slips WHERE employee_id = ? AND month = ? AND status != 'CANCELLED'",
                            (e["id"], month)).fetchone():
                skipped.append(f"{e['full_name']} (already has a slip)")
                continue
            sid = conn.execute("""INSERT INTO salary_slips (employee_id, month, status, days_in_month, details,
                                  created_by, created_at) VALUES (?, ?, 'DRAFT', ?, '{}', ?, ?)""",
                               (e["id"], month, days, user["id"], utcnow_iso())).lastrowid
            _fill_from_structure(conn, sid, e["id"], 0, days)
            created.append(sid)
        if created:
            audit.log("SALARY_SLIPS_GENERATED", "payroll", month, new={"drafts": len(created)})
    return created, skipped


def slip(slip_id):
    s = query("""SELECT s.*, e.full_name, e.emp_code, d.name AS department, g.name AS designation, e.joining_date,
                        cu.username AS created_by_name, iu.username AS issued_by_name
                   FROM salary_slips s JOIN employees e ON e.id = s.employee_id
                   LEFT JOIN departments d ON d.id = e.department_id LEFT JOIN designations g ON g.id = e.designation_id
                   LEFT JOIN users cu ON cu.id = s.created_by LEFT JOIN users iu ON iu.id = s.issued_by
                  WHERE s.id = ?""", (slip_id,), one=True)
    if s is None:
        return None, [], {}
    lines = query("SELECT * FROM salary_slip_lines WHERE slip_id = ? ORDER BY seq", (slip_id,))
    return s, lines, json.loads(s["details"] or "{}")


def update_draft(user, slip_id, form, recalculate=False):
    with transaction() as conn:
        s = conn.execute("SELECT * FROM salary_slips WHERE id = ?", (slip_id,)).fetchone()
        if s is None or s["status"] != "DRAFT":
            raise ValidationError("Only draft slips can be changed.")
        _guard_not_self(user, s["employee_id"], "prepare")
        try:
            lop = float(form.get("lop_days") or 0)
        except ValueError:
            raise ValidationError("Loss-of-pay days must be a number.")
        if lop < 0 or lop > s["days_in_month"] or (lop * 2) != int(lop * 2):
            raise ValidationError(f"Loss-of-pay days must be between 0 and {s['days_in_month']}, in half days.")
        remarks = clean_text(form.get("remarks"), "Remarks", max_len=300) or None
        conn.execute("UPDATE salary_slips SET lop_days = ?, remarks = ? WHERE id = ?", (lop, remarks, slip_id))
        if recalculate:
            if not conn.execute("SELECT 1 FROM salary_structures WHERE employee_id = ?", (s["employee_id"],)).fetchone():
                raise ValidationError("This employee has no pay structure.")
            _fill_from_structure(conn, slip_id, s["employee_id"], lop, s["days_in_month"])
        else:
            lines = _read_lines(form, "EARNING") + _read_lines(form, "DEDUCTION")
            if not any(k == "EARNING" for k, _, _ in lines):
                raise ValidationError("A slip needs at least one earning.")
            conn.execute("DELETE FROM salary_slip_lines WHERE slip_id = ?", (slip_id,))
            for seq, (kind, name, amount) in enumerate(lines):
                conn.execute("INSERT INTO salary_slip_lines (slip_id, kind, name, amount, seq) VALUES (?, ?, ?, ?, ?)",
                             (slip_id, kind, name, amount, seq))
            _totals(conn, slip_id)
        audit.log("SALARY_SLIP_EDITED", "salary_slip", slip_id, new={"lop_days": lop}, remarks="Amounts not logged")


def issue(user, slip_ids):
    issued = []
    with transaction() as conn:
        for slip_id in slip_ids:
            s = conn.execute("SELECT * FROM salary_slips WHERE id = ?", (slip_id,)).fetchone()
            if s is None or s["status"] != "DRAFT":
                continue
            _guard_not_self(user, s["employee_id"], "issue")
            emp = _employee(conn, s["employee_id"])
            st = conn.execute("SELECT * FROM salary_structures WHERE employee_id = ?", (s["employee_id"],)).fetchone()
            details = {"full_name": emp["full_name"], "emp_code": emp["emp_code"], "designation": emp["designation"],
                       "department": emp["department"], "joining_date": emp["joining_date"],
                       "pan": st["pan"] if st else None, "bank_name": st["bank_name"] if st else None,
                       "bank_account": st["bank_account"] if st else None, "ifsc": st["ifsc"] if st else None}
            conn.execute("""UPDATE salary_slips SET status = 'ISSUED', issued_by = ?, issued_at = ?, details = ?
                            WHERE id = ?""", (user["id"], utcnow_iso(), json.dumps(details), slip_id))
            audit.log("SALARY_SLIP_ISSUED", "salary_slip", slip_id, new={"employee": emp["emp_code"], "month": s["month"]})
            notify([emp["user_id"]], "Salary slip available",
                   f"Your salary slip for {month_label(s['month'])} has been issued.", url_for("payroll.my_slip", slip_id=slip_id))
            issued.append(slip_id)
    return issued


def cancel(user, slip_id, reason):
    reason = clean_text(reason, "Reason", required=True, max_len=300)
    with transaction() as conn:
        s = conn.execute("SELECT * FROM salary_slips WHERE id = ?", (slip_id,)).fetchone()
        if s is None or s["status"] == "CANCELLED":
            raise ValidationError("This slip cannot be cancelled.")
        _guard_not_self(user, s["employee_id"], "cancel")
        conn.execute("""UPDATE salary_slips SET status = 'CANCELLED', cancelled_by = ?, cancelled_at = ?, cancel_reason = ?
                        WHERE id = ?""", (user["id"], utcnow_iso(), reason, slip_id))
        audit.log("SALARY_SLIP_CANCELLED", "salary_slip", slip_id, prev={"status": s["status"]}, remarks=reason)
        if s["status"] == "ISSUED":
            emp = _employee(conn, s["employee_id"])
            notify([emp["user_id"]], "Salary slip withdrawn",
                   f"Your salary slip for {month_label(s['month'])} has been withdrawn by payroll. Reason: {reason}. "
                   "A corrected slip will be issued.", url_for("payroll.my_slips"))


def month_overview(month):
    """Every active employee with their pay-structure and slip status for the month."""
    return query("""SELECT e.id, e.emp_code, e.full_name, g.name AS designation, d.name AS department,
                           (SELECT COUNT(*) FROM salary_structures s WHERE s.employee_id = e.id) AS has_structure,
                           sl.id AS slip_id, sl.status AS slip_status, sl.net
                      FROM employees e LEFT JOIN designations g ON g.id = e.designation_id
                      LEFT JOIN departments d ON d.id = e.department_id
                      LEFT JOIN salary_slips sl ON sl.employee_id = e.id AND sl.month = ? AND sl.status != 'CANCELLED'
                     WHERE e.status = 'ACTIVE' ORDER BY e.full_name""", (month,))


def slips_for_employee(employee_id):
    return query("SELECT * FROM salary_slips WHERE employee_id = ? AND status = 'ISSUED' ORDER BY month DESC",
                 (employee_id,))

