"""Half-day leave applications and the contract end date: applying, balances, overlaps, display, and
no leave or timesheet after the contract ends.

Run:  python -m unittest tests.test_leave_halfday_contract -v
"""
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrms import create_app  # noqa: E402
from hrms.cli import DEMO_PASSWORD  # noqa: E402


def next_weekday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


class HalfDayContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "t.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "TESTING": True})
        assert cls.app.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 0
        cls.sql("UPDATE leave_types SET annual_entitlement = 12, accrual_per_month = NULL, min_notice_days = NULL "
                "WHERE code = 'CL'")
        cls.cl = cls.sql("SELECT id FROM leave_types WHERE code = 'CL'")[0][0]
        cls.day = next_weekday(date.today() + timedelta(days=10))

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

    def apply(self, c, start, end=None, half=""):
        return c.post("/leave/apply", data={"_csrf": c.tok, "leave_type_id": str(self.cl), "from_date": start.isoformat(),
                                            "to_date": (end or start).isoformat(), "half_day": half,
                                            "reason": "Family function"})

    def test_1_half_day_application(self):
        rahul = self.login("rahul")
        page = rahul.get("/leave/apply").get_data(as_text=True)
        self.assertIn("Half day: first half", page)
        prev = rahul.get(f"/leave/preview?leave_type_id={self.cl}&from_date={self.day}&to_date={self.day}&half_day=FIRST")
        self.assertEqual(prev.get_json(), {"ok": True, "days": 0.5})

        r = self.apply(rahul, self.day, self.day + timedelta(days=1), half="FIRST")
        self.assertIn("A half day must be a single date", r.get_data(as_text=True))
        saturday = self.day + timedelta(days=(5 - self.day.weekday()))
        self.assertIn("only be taken on a working day", self.apply(rahul, saturday, half="SECOND").get_data(as_text=True))
        self.assertIn("Choose the first half", self.apply(rahul, self.day, half="MORNING").get_data(as_text=True))

        self.assertEqual(self.apply(rahul, self.day, half="FIRST").status_code, 302)
        req = self.sql("SELECT * FROM leave_requests ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((req["days"], req["half_day"]), (0.5, "FIRST"))
        bal = self.sql("SELECT pending FROM leave_balances WHERE leave_type_id = ?", (self.cl,))[0][0]
        self.assertEqual(bal, 0.5)
        self.assertIn("first half", rahul.get(f"/leave/{req['id']}").get_data(as_text=True))

        self.assertIn("overlap", self.apply(rahul, self.day, half="FIRST").get_data(as_text=True))   # same half
        self.assertIn("overlap", self.apply(rahul, self.day).get_data(as_text=True))                 # full day
        self.assertEqual(self.apply(rahul, self.day, half="SECOND").status_code, 302)                # other half
        self.assertEqual(self.sql("SELECT pending FROM leave_balances WHERE leave_type_id = ?", (self.cl,))[0][0], 1)

    def test_2_no_leave_after_contract_end(self):
        priya = self.login("priya")
        end = self.day + timedelta(days=3)
        self.sql("UPDATE employees SET contract_end_date = ? WHERE emp_code = 'M102'", (end.isoformat(),))
        self.assertIn("Your contract ends on", priya.get("/leave/apply").get_data(as_text=True))
        r = self.apply(priya, end, end + timedelta(days=4))
        self.assertIn("Leave cannot be taken after that date", r.get_data(as_text=True))
        self.assertEqual(self.apply(priya, next_weekday(self.day)).status_code, 302)      # before the end: fine
        self.sql("UPDATE employees SET contract_end_date = NULL WHERE emp_code = 'M102'")

    def test_3_hr_records_contract_end(self):
        hr, priya = self.login("hr"), self.login("priya")
        priya_emp = self.sql("SELECT employee_id FROM users WHERE username = 'priya'")[0][0]
        hr_emp = self.sql("SELECT employee_id FROM users WHERE username = 'hr'")[0][0]
        end = (date.today() + timedelta(days=200)).isoformat()
        r = hr.post(f"/employees/{priya_emp}/contract-end", data={"_csrf": hr.tok, "contract_end_date": end},
                    follow_redirects=True)
        self.assertIn("Contract end date saved", r.get_data(as_text=True))
        self.assertEqual(self.sql("SELECT contract_end_date FROM employees WHERE id = ?", (priya_emp,))[0][0], end)
        self.assertTrue(self.sql("SELECT 1 FROM audit_logs WHERE action = 'CONTRACT_END_SET'"))
        self.assertIn("Contract ends", priya.get("/profile").get_data(as_text=True))
        r = hr.post(f"/employees/{hr_emp}/contract-end", data={"_csrf": hr.tok, "contract_end_date": end},
                    follow_redirects=True)
        self.assertIn("cannot set your own contract end date", r.get_data(as_text=True))
        self.assertEqual(priya.post(f"/employees/{priya_emp}/contract-end",
                                    data={"_csrf": priya.tok, "contract_end_date": end}).status_code, 403)
        self.assertIn("without a contract end date", hr.get("/pending").get_data(as_text=True))
        hr.post(f"/employees/{priya_emp}/contract-end", data={"_csrf": hr.tok, "contract_end_date": ""})
        self.assertIsNone(self.sql("SELECT contract_end_date FROM employees WHERE id = ?", (priya_emp,))[0][0])

    def test_4_unpaid_flag_on_leave_types(self):
        admin = self.login("admin")
        page = admin.get(f"/admin/leave-types/{self.cl}").get_data(as_text=True)
        self.assertIn('name="is_paid"', page)
        self.assertEqual(self.sql("SELECT is_paid FROM leave_types WHERE id = ?", (self.cl,))[0][0], 1)


if __name__ == "__main__":
    unittest.main()
