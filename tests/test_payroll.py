"""Salary slips: pay structures, monthly drafts, loss of pay, issue / cancel, access control.

Run:  python -m unittest discover -s tests -v
"""
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrms import create_app  # noqa: E402
from hrms.cli import DEMO_PASSWORD  # noqa: E402


class PayrollTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "p.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "TESTING": True})
        assert cls.app.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 0
        cls.sql("UPDATE users SET role_id = (SELECT id FROM roles WHERE code = 'ACCOUNTS') WHERE username = 'amit'")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def sql(cls, q, params=()):
        conn = sqlite3.connect(cls.app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(q, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def login(self, user):
        c = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"username": user, "password": DEMO_PASSWORD, "_csrf": tok})
        c.tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/profile").get_data(as_text=True)).group(1)
        return c

    def emp_id(self, username):
        return self.sql("SELECT employee_id FROM users WHERE username = ?", (username,))[0][0]

    def structure(self, c, username, earnings, deductions):
        data = {"_csrf": c.tok, "pan": "ABCDE1234F", "bank_name": "State Bank of India", "bank_account": "12345678901",
                "ifsc": "SBIN0001234"}
        for i, (n, a) in enumerate(earnings):
            data[f"earning_name_{i}"], data[f"earning_amount_{i}"] = n, a
        for i, (n, a) in enumerate(deductions):
            data[f"deduction_name_{i}"], data[f"deduction_amount_{i}"] = n, a
        return c.post(f"/payroll/structure/{self.emp_id(username)}", data=data)

    def test_access_control(self):
        for user in ("admin", "rahul", "ceo"):
            self.assertEqual(self.login(user).get("/payroll/").status_code, 403, user)
        self.assertEqual(self.login("hr").get("/payroll/").status_code, 200)
        self.assertEqual(self.login("amit").get("/payroll/").status_code, 200)  # Accounts Officer role
        from hrms.core.rbac import sod_violations
        self.assertTrue(sod_violations({"payroll.manage", "users.manage"}))

    def test_slip_lifecycle(self):
        hr = self.login("hr")
        r = self.structure(hr, "rahul", [("Consolidated remuneration", "45,000"), ("Travel allowance", "2000")],
                           [("TDS", "4700"), ("Professional tax", "200")])
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])
        self.assertIn("own pay structure", self.structure(hr, "hr", [("Pay", "1")], []).get_data(as_text=True))
        audit = " ".join(r["new_state"] or "" for r in self.sql("SELECT new_state FROM audit_logs WHERE action LIKE 'PAY%'"))
        self.assertIn("Consolidated remuneration", audit)
        self.assertNotIn("4500000", audit)
        self.assertNotIn("45000", audit)  # amounts never reach the audit log

        hr.post("/payroll/generate", data={"_csrf": hr.tok, "month": "2026-09"})
        slip = self.sql("SELECT * FROM salary_slips WHERE employee_id = ? AND month = '2026-09'", (self.emp_id("rahul"),))[0]
        self.assertEqual((slip["status"], slip["gross"], slip["net"]), ("DRAFT", 4700000, 4210000))
        rahul = self.login("rahul")
        self.assertEqual(rahul.get(f"/salary/{slip['id']}").status_code, 404)  # drafts are not visible

        r = hr.post(f"/payroll/slips/{slip['id']}", data={"_csrf": hr.tok, "lop_days": "3", "action": "recalculate"})
        self.assertEqual(r.status_code, 302)
        s = self.sql("SELECT gross, total_deductions, net FROM salary_slips WHERE id = ?", (slip["id"],))[0]
        self.assertEqual(tuple(s), (4230000, 490000, 3740000))  # 45000*27/30 + 2000*27/30 - 4900

        hr.post("/payroll/issue", data={"_csrf": hr.tok, "slip_id": str(slip["id"])})
        page = rahul.get(f"/salary/{slip['id']}").get_data(as_text=True)
        self.assertIn("Rupees Thirty Seven Thousand Four Hundred Only", page)
        self.assertIn("37,400.00", page)
        self.assertIn("SBIN0001234", page)
        self.assertIn("September 2026", rahul.get("/salary/").get_data(as_text=True))
        self.assertTrue(self.sql("SELECT 1 FROM notifications n JOIN users u ON u.id = n.user_id "
                                 "WHERE u.username = 'rahul' AND n.title = 'Salary slip available'"))
        self.assertEqual(self.login("priya").get(f"/salary/{slip['id']}").status_code, 404)

        r = hr.post(f"/payroll/slips/{slip['id']}", data={"_csrf": hr.tok, "lop_days": "0", "action": "save",
                                                          "earning_name_0": "Pay", "earning_amount_0": "1"})
        self.assertIn("Only draft slips can be changed", r.get_data(as_text=True))

        hr.post(f"/payroll/slips/{slip['id']}/cancel", data={"_csrf": hr.tok, "reason": "Wrong TDS"})
        self.assertEqual(rahul.get(f"/salary/{slip['id']}").status_code, 404)
        hr.post("/payroll/generate", data={"_csrf": hr.tok, "month": "2026-09"})
        self.assertEqual(len(self.sql("SELECT 1 FROM salary_slips WHERE employee_id = ? AND month = '2026-09' "
                                      "AND status = 'DRAFT'", (self.emp_id("rahul"),))), 1)

    def test_own_slip_needs_another_officer(self):
        accounts = self.login("amit")
        self.assertEqual(self.structure(accounts, "hr", [("Consolidated remuneration", "60000")], []).status_code, 302)
        hr = self.login("hr")
        r = hr.post("/payroll/generate", data={"_csrf": hr.tok, "month": "2026-08"}, follow_redirects=True)
        self.assertIn("your own slip", r.get_data(as_text=True))
        accounts.post("/payroll/generate", data={"_csrf": accounts.tok, "month": "2026-08"})
        sid = self.sql("SELECT id FROM salary_slips WHERE employee_id = ? AND month = '2026-08'", (self.emp_id("hr"),))[0][0]
        r = hr.post("/payroll/issue", data={"_csrf": hr.tok, "slip_id": str(sid)}, follow_redirects=True)
        self.assertIn("cannot issue your own", r.get_data(as_text=True))
        accounts.post("/payroll/issue", data={"_csrf": accounts.tok, "slip_id": str(sid)})
        self.assertEqual(self.sql("SELECT status FROM salary_slips WHERE id = ?", (sid,))[0][0], "ISSUED")
        self.assertIn("Rupees Sixty Thousand Only", hr.get(f"/salary/{sid}").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
