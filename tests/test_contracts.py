"""Contract renewal reminders to the employee and HR, and HR recording renewals.

Run:  python -m unittest tests.test_contracts -v
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

T = date(2026, 10, 9)  # a fixed "today" so the tests do not depend on the calendar


def months_back(d, n):
    y, m = divmod(d.month - 1 - n, 12)
    return date(d.year + y, m + 1, d.day)


class ContractReminderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "c.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "TESTING": True})
        assert cls.app.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 0
        cls.sql("UPDATE employees SET joining_date = NULL")
        cls.ids = {r["username"]: r["employee_id"] for r in cls.sql("SELECT username, employee_id FROM users")}
        # Rahul: renewal recorded 30 days ahead. Priya and Kavita: no next renewal date recorded.
        # Meera (js): renewal recorded 20 days ahead. Everyone else: no joining date, so no contract tracked.
        cls.set_joining("rahul", "2024-04-01")
        cls.sql("INSERT INTO contract_renewals (employee_id, renewal_date) VALUES (?, ?)",
                (cls.ids["rahul"], (T + timedelta(days=30)).isoformat()))
        cls.set_joining("priya", (months_back(T, 12) + timedelta(days=8)).isoformat())
        cls.set_joining("kavita", (months_back(T, 12) - timedelta(days=9)).isoformat())
        cls.set_joining("js", "2025-01-15")
        cls.sql("INSERT INTO contract_renewals (employee_id, renewal_date) VALUES (?, ?)",
                (cls.ids["js"], (T + timedelta(days=20)).isoformat()))

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

    @classmethod
    def set_joining(cls, user, d):
        cls.sql("UPDATE employees SET joining_date = ? WHERE id = ?", (d, cls.ids[user]))

    def run_on(self, d):
        from hrms.services.contracts import send_reminders
        with self.app.test_request_context():
            return send_reminders(d)

    def notes(self, user):
        return [(r["title"], r["message"]) for r in self.sql(
            "SELECT n.title, n.message FROM notifications n JOIN users u ON u.id = n.user_id WHERE u.username = ? "
            "ORDER BY n.id", (user,))]

    def test_reminder_sequence(self):
        self.sql("DELETE FROM notifications")
        self.sql("DELETE FROM reminder_log")

        self.run_on(T)  # T is a Friday
        rahul, hr = self.notes("rahul"), self.notes("hr")
        self.assertEqual(rahul, [("Contract renewal due in 30 days",
                                  f"Your contract renewal is due on {(T + timedelta(days=30)).strftime('%d %b %Y')} "
                                  "(in 30 days). Please contact HR or your reporting officer about the renewal.")])
        self.assertIn(("Contract renewal due in 30 days",
                       f"Contract renewal for Rahul Joshi (Senior Consultant, Economy & Finance) is due on "
                       f"{(T + timedelta(days=30)).strftime('%d %b %Y')} (in 30 days)."), hr)
        # no next renewal date: only HR is told, in one combined request, and no end date is guessed
        self.assertEqual(self.notes("priya"), [])
        self.assertEqual(self.notes("kavita"), [])
        asks = lambda: [m for t, m in self.notes("hr") if t == "Contract renewal dates needed"]  # noqa: E731
        self.assertEqual(asks(), ["No next renewal date is recorded for 2 employees: Kavita Pawar, Priya Nair. "
                                  "Please record each person's renewal date so that renewal reminders can be sent."])
        self.assertEqual(self.notes("amit"), [])  # no joining date: no contract tracked

        before = len(self.sql("SELECT 1 FROM notifications"))
        self.assertEqual(self.run_on(T), 0)                       # running again the same day sends nothing
        self.assertEqual(self.run_on(T + timedelta(days=1)), 0)   # nor later the same week
        self.assertEqual(len(self.sql("SELECT 1 FROM notifications")), before)
        self.run_on(T + timedelta(days=3))                        # the following week: HR asked again
        self.assertEqual(len(asks()), 2)

        self.run_on(T + timedelta(days=16))  # Rahul now 14 days out: the 15-day reminder, once
        self.assertEqual([t for t, _ in self.notes("rahul")][-1], "Contract renewal due in 14 days")
        self.run_on(T + timedelta(days=17))
        self.assertEqual(len(self.notes("rahul")), 2)

        # Rahul's renewal day passes with no further date recorded: HR is asked straight away,
        # even though this week's request was already sent
        self.run_on(T + timedelta(days=30))
        self.assertIn("Rahul Joshi", asks()[-1])
        self.assertIn("4 employees", asks()[-1])  # Meera's renewal (day 20) has also passed by now

    def test_missed_days_do_not_cause_a_burst(self):
        self.sql("DELETE FROM notifications WHERE user_id = (SELECT id FROM users WHERE username = 'js')")
        self.run_on(T + timedelta(days=19))  # first check happens only 1 day before Meera's renewal
        js = [t for t, _ in self.notes("js")]
        self.assertEqual(js, ["Contract renewal due tomorrow"])

    def test_hr_records_renewal_and_reminders_stop(self):
        c = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"username": "hr", "password": DEMO_PASSWORD, "_csrf": tok})
        page = c.get(f"/employees/{self.ids['priya']}").get_data(as_text=True)
        self.assertIn("Record renewal", page)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        new_date = (T + timedelta(days=7)).isoformat()
        r = c.post(f"/employees/{self.ids['priya']}/renewals", data={"_csrf": tok, "renewal_date": new_date,
                                                                    "note": "Renewed for 11 months"})
        self.assertEqual(r.status_code, 302)
        self.addCleanup(self.sql, "DELETE FROM contract_renewals WHERE employee_id = ?", (self.ids["priya"],))
        from hrms.services.contracts import status
        with self.app.app_context():
            s = status(self.ids["priya"], T)
        self.assertEqual((s["state"], s["renewal_due"].isoformat()), ("scheduled", new_date))
        self.assertTrue(self.sql("SELECT 1 FROM audit_logs WHERE action = 'CONTRACT_RENEWAL_ADDED'"))

        hr_emp = self.ids["hr"]  # HR cannot record her own renewal
        r = c.post(f"/employees/{hr_emp}/renewals", data={"_csrf": tok, "renewal_date": "2027-01-01"}, follow_redirects=True)
        self.assertIn("cannot record your own contract renewal", r.get_data(as_text=True))

        emp = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', emp.get("/login").get_data(as_text=True)).group(1)
        emp.post("/login", data={"username": "rahul", "password": DEMO_PASSWORD, "_csrf": tok})
        tok = re.search(r'name="_csrf" value="([^"]+)"', emp.get("/profile").get_data(as_text=True)).group(1)
        self.assertEqual(emp.post(f"/employees/{self.ids['rahul']}/renewals",
                                  data={"_csrf": tok, "renewal_date": "2027-01-01"}).status_code, 403)

    def test_daily_check_runs_once_from_normal_use_and_from_the_command(self):
        app = create_app({"DATABASE": self.app.config["DATABASE"], "UPLOAD_DIR": str(self.tmp / "u"),
                          "BACKUP_DIR": str(self.tmp / "b"), "DAILY_JOBS": True})
        self.sql("DELETE FROM reminder_log")
        self.sql("DELETE FROM notifications")
        c = app.test_client()
        c.get("/login")                                    # not signed in: nothing runs
        self.assertEqual(self.sql("SELECT COUNT(*) FROM notifications")[0][0], 0)
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"username": "ceo", "password": DEMO_PASSWORD, "_csrf": tok})
        c.get("/dashboard")
        first = self.sql("SELECT COUNT(*) FROM notifications")[0][0]
        self.assertGreater(first, 0)
        c.get("/dashboard")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM notifications")[0][0], first)
        r = app.test_cli_runner().invoke(args=["send-reminders"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Renewal reminders sent: 0", r.output)


if __name__ == "__main__":
    unittest.main()
