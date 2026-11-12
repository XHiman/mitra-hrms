"""Employee datasheet import: joining / renewal dates, birthdays, new staff, contract leave cycles.

Run:  python -m unittest discover -s tests -v
"""
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrms import create_app  # noqa: E402
from hrms.cli import DEMO_PASSWORD  # noqa: E402

HEAD = ["Sr. No ", "Name", "Designation ", "Sector", "DOB", "Contact Numbers ", "Email Id ", "Date of Joining",
        "Date of Renewal ", "Date of Renewal", "Date of Renewal", "Remarks", None]


class DatasheetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "d.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "INSTANCE_DIR": str(cls.tmp), "TESTING": True})
        cls.cli = cls.app.test_cli_runner()
        assert cls.cli.invoke(args=["seed-demo"]).exit_code == 0
        conn = sqlite3.connect(cls.app.config["DATABASE"])
        conn.execute("UPDATE employees SET phone = '9000000001' WHERE emp_code = 'M102'")  # Priya
        conn.execute("UPDATE leave_types SET annual_entitlement = 18, accrual_per_month = 1.5, allow_backdated = 1 "
                     "WHERE code = 'EL'")
        conn.commit()
        conn.close()
        wb = Workbook()
        ws = wb.active
        ws.title = "MITRA"
        ws.append(HEAD)
        ws.append([1, "Ananya Rao", "C.E.O", None, None, 9800000000, None, None, None, None, None, None, None])
        ws.append([2, "Rahul Joshi ", "Senior Consultant", "Finance", datetime(1990, 11, 13), 8149879269, None,
                   "19.03.2024", "03.02.2026", "03.01.2027", None, None, None])
        ws.append([3, None, "Associate", "Infrastructure", "28.11.", 9000000001, None, "10..01.2025 ",
                   "10.01.2026 - RO", None, None, "putup", None])           # Priya, found by phone
        ws.append([4, "Kavita Pawar", "Associate", None, "27th  November ", None, None, "01.10.2025- YP",
                   "01.09.2027", "01.08.2026", None, None, None])           # dates out of order
        ws.append([5, None, "Director", "Technical", None, None, None, None, None, None, None, None, None])
        ws.append([6, "Swarali Lalge", "Clerk", None, "14/09/1997", None, None, "04.03.2025", None, None, None, None, None])
        mur = wb.create_sheet("MURWEP")
        mur.append(["Maharashtra Urban Water Supply Sanitation and Reuse Program"])
        mur.append(["Sr. No ", "Name", "Designation ", "Sector", "Contact Numbers ", "Email Id ", "Date of Joining",
                    "Date of Renewal ", "Date of Renewal"])
        mur.append([1, "SHRI.ANANYA RAO IAS", "PROJECT DIRECTOR", None, None, None, "06.04.2026", None, None])
        mur.append([2, "DR.PRAMOD SHINDE", "ADDNL PROJECT DIRECTOR", None, None, None, "24.02.2026", None, None])
        cls.sheet = cls.tmp / "datasheet.xlsx"
        wb.save(cls.sheet)
        cls.dry = cls.cli.invoke(args=["import-datasheet", str(cls.sheet), "--dry-run"])
        cls.real = cls.cli.invoke(args=["import-datasheet", str(cls.sheet), "--shared-password", DEMO_PASSWORD])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rows(self, q, params=()):
        conn = sqlite3.connect(self.app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(q, params).fetchall()
        finally:
            conn.close()

    def emp(self, code):
        return self.rows("SELECT * FROM employees WHERE emp_code = ?", (code,))[0]

    def renewals(self, code):
        return [(r["renewal_date"], r["note"]) for r in self.rows(
            "SELECT renewal_date, note FROM contract_renewals r JOIN employees e ON e.id = r.employee_id "
            "WHERE e.emp_code = ? ORDER BY renewal_date", (code,))]

    def test_dry_run_reports_without_changing(self):
        out = self.dry.output
        self.assertEqual(self.dry.exit_code, 0, out)
        self.assertIn("2 new: Swarali Lalge, DR.PRAMOD SHINDE", out)
        self.assertIn("matched to Priya Nair by mobile number", out)
        self.assertIn("skipped, no name (Director, Technical)", out)
        self.assertIn("not in date order", out)
        self.assertIn("senior officers follow the calendar leave year", out)

    def test_dates_recorded(self):
        self.assertEqual(self.real.exit_code, 0, self.real.output)
        self.assertEqual(self.emp("M101")["joining_date"], "2024-03-19")
        self.assertEqual(self.emp("M101")["date_of_birth"], "1990-11-13")
        self.assertEqual(self.renewals("M101"), [("2026-02-03", None), ("2027-01-03", None)])
        priya = self.emp("M102")
        self.assertEqual((priya["joining_date"], priya["date_of_birth"]), ("2025-01-10", "1900-11-28"))
        self.assertEqual(self.renewals("M102"), [("2026-01-10", "RO, putup")])
        self.assertEqual(self.emp("M001")["joining_date"], "2024-04-01")  # CEO: project (MURWEP) date not applied
        self.assertEqual(self.emp("M104")["date_of_birth"], "1900-11-27")

    def test_new_staff_created(self):
        new = {r["full_name"]: r for r in self.rows(
            """SELECT e.full_name, e.joining_date, d.name AS dept, u.username, u.must_change_password
                 FROM employees e JOIN users u ON u.employee_id = e.id LEFT JOIN departments d ON d.id = e.department_id
                WHERE e.emp_code LIKE 'MT%'""")}
        self.assertEqual(set(new), {"Swarali Lalge", "Dr. Pramod Shinde"})
        self.assertEqual((new["Dr. Pramod Shinde"]["username"], new["Dr. Pramod Shinde"]["dept"]),
                         ("pramod.shinde", "MURWEP"))
        self.assertEqual(new["Swarali Lalge"]["joining_date"], "2025-03-04")
        self.assertEqual(new["Swarali Lalge"]["must_change_password"], 0)

    def test_contract_cycle_on_dashboard_and_leave(self):
        c = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"username": "rahul", "password": DEMO_PASSWORD, "_csrf": tok})
        page = c.get("/dashboard").get_data(as_text=True)
        self.assertIn("19 Mar 2024", page)          # date of joining
        self.assertIn("03 Jan 2027", page)          # renewal due
        self.assertIn("03 Feb 2026 to 02 Jan 2027", page)  # leave cycle = current contract term
        from hrms.services.cycles import cycle_for
        with self.app.app_context():
            emp_id = self.emp("M101")["id"]
            self.assertEqual(cycle_for(emp_id, date(2025, 6, 1)).start, date(2025, 3, 19))  # yearly split of 1st term
            self.assertEqual(cycle_for(emp_id, date(2026, 6, 1)).start, date(2026, 2, 3))
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/leave/apply").get_data(as_text=True)).group(1)
        el = self.rows("SELECT id FROM leave_types WHERE code = 'EL'")[0]["id"]
        r = c.post("/leave/apply", data={"_csrf": tok, "leave_type_id": str(el), "from_date": "2026-01-29",
                                         "to_date": "2026-02-05", "reason": "x"}, content_type="multipart/form-data")
        self.assertIn("crosses into your next leave cycle", r.get_data(as_text=True))
        r = c.post("/leave/apply", data={"_csrf": tok, "leave_type_id": str(el), "from_date": "2024-03-01",
                                         "to_date": "2024-03-01", "reason": "x"}, content_type="multipart/form-data")
        self.assertIn("before your date of joining", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
