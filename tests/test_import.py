"""Staff import from the onboarding Excel sheet.

Run:  python -m unittest discover -s tests -v
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrms import create_app  # noqa: E402

HEADER = ["Employee ID *", "Full Name *", "Designation *", "Department / Team *", "Role *",
          "Reporting Officer Employee ID *", "Official Email", "Mobile", "Joining Date", "Employment Type",
          "Preferred Username"]


def write_sheet(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Staff List"
    ws.append(HEADER)
    ws.append(["EXAMPLE-1", "Ignored Person", "x", "x", "CEO", "", "", "", "", "", ""])
    for r in rows:
        ws.append(r)
    wb.save(path)


class StaffImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "t.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "INSTANCE_DIR": str(cls.tmp), "TESTING": True})
        cls.cli = cls.app.test_cli_runner()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rows(self, sql, params=()):
        conn = sqlite3.connect(self.app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_import_validates_then_creates_then_updates(self):
        bad = self.tmp / "bad.xlsx"
        write_sheet(bad, [["MT1", "Chief Person", "C.E.O", "Office", "CEO", "", "", "", "", "", ""],
                          ["MT2", "Some One", "Associate", "", "Manager", "MT9", "bad-email", "", "31-31-2025", "", ""],
                          ["MT1", "Dup Person", "Associate", "", "", "MT1", "", "", "", "", ""]])
        r = self.cli.invoke(args=["import-staff", str(bad)])
        self.assertNotEqual(r.exit_code, 0)
        for msg in ("unknown role 'Manager'", "'MT9' is not in the sheet", "email 'bad-email'",
                    "joining date", "repeated"):
            self.assertIn(msg, r.output)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM employees")[0][0], 0)

        good = self.tmp / "good.xlsx"
        write_sheet(good, [
            ["MT1", "Shri. Anil Kumar", "C.E.O", "Office", "CEO", "", "", "9800000001", "01-04-2023", "", ""],
            ["MT3", "Rohan Mehta", "Associate", "Finance", "", "MT2", "", "", "15-06-2024", "Consultant", ""],
            ["MT2", "Sneha Rao", "Director", "Finance", "Reporting Manager", "MT1", "s@x.in", "", "", "", "sneha"],
            ["MT4", "Dev Admin", "Young Professional", "IT", "Admin", "MT1", "", "", "", "", ""],
            ["MT5", "Rohan Mehta", "Associate", "", "Employee", "MT2", "", "", "", "", ""],
        ])
        r = self.cli.invoke(args=["import-staff", str(good), "--dry-run"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Sheet OK: 5 new", r.output)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM employees")[0][0], 0)

        r = self.cli.invoke(args=["import-staff", str(good)])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("5 created", r.output)
        users = {u["emp_code"]: u for u in self.rows(
            """SELECT e.emp_code, u.username, r.code AS role, ro.emp_code AS ro, u.must_change_password, u.password_hash,
                      e.joining_date FROM users u JOIN employees e ON e.id = u.employee_id JOIN roles r ON r.id = u.role_id
                      LEFT JOIN employees ro ON ro.id = e.reporting_officer_id""")}
        self.assertEqual(users["MT1"]["username"], "anil.kumar")      # honorific dropped
        self.assertEqual(users["MT2"]["username"], "sneha")           # preferred username kept
        self.assertEqual({users["MT3"]["username"], users["MT5"]["username"]}, {"rohan.mehta", "rohan.mehta2"})
        self.assertEqual(users["MT3"]["role"], "EMPLOYEE")            # blank role -> Employee
        self.assertEqual(users["MT3"]["ro"], "MT2")                   # officer listed later in the sheet
        self.assertEqual(users["MT4"]["role"], "SYSADMIN")
        self.assertEqual(users["MT1"]["joining_date"], "2023-04-01")
        self.assertTrue(all(u["must_change_password"] for u in users.values()))
        slips = list(self.tmp.glob("login-slips-*.html"))
        self.assertEqual(len(slips), 1)
        self.assertIn("anil.kumar", slips[0].read_text(encoding="utf-8"))
        self.assertNotIn("EXAMPLE", "".join(users))

        # Re-import with a change: details update, passwords untouched, no new slips for existing staff.
        write_sheet(good, [
            ["MT1", "Shri. Anil Kumar", "C.E.O", "Office", "CEO", "", "", "", "", "", ""],
            ["MT2", "Sneha Rao", "Director", "Finance", "Reporting Manager", "MT1", "", "", "", "", ""],
            ["MT3", "Rohan Mehta", "Senior Associate", "Finance", "Employee", "MT1", "", "", "", "", ""],
            ["MT4", "Dev Admin", "Young Professional", "IT", "Admin", "MT1", "", "", "", "", ""],
            ["MT5", "Rohan Mehta", "Associate", "", "Employee", "MT2", "", "", "", "", ""],
        ])
        r = self.cli.invoke(args=["import-staff", str(good)])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("0 created, 5 updated", r.output)
        mt3 = self.rows("""SELECT u.password_hash, ro.emp_code AS ro, g.name AS desig FROM users u
                           JOIN employees e ON e.id = u.employee_id LEFT JOIN employees ro ON ro.id = e.reporting_officer_id
                           LEFT JOIN designations g ON g.id = e.designation_id WHERE e.emp_code = 'MT3'""")[0]
        self.assertEqual(mt3["password_hash"], users["MT3"]["password_hash"])
        self.assertEqual((mt3["ro"], mt3["desig"]), ("MT1", "Senior Associate"))
        self.assertEqual(len(list(self.tmp.glob("login-slips-*.html"))), 1)


if __name__ == "__main__":
    unittest.main()
