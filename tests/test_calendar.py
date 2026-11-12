"""Holiday list (Government of Maharashtra) and staff birthdays on the calendar.

Run:  python -m unittest discover -s tests -v
"""
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrms import create_app  # noqa: E402
from hrms.cli import DEMO_PASSWORD  # noqa: E402


class CalendarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "c.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "TESTING": True})
        cli = cls.app.test_cli_runner()
        assert cli.invoke(args=["seed-demo"]).exit_code == 0
        cls.first_load = cli.invoke(args=["load-holidays", "2026"]).output
        cls.second_load = cli.invoke(args=["load-holidays", "2026"]).output

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def login(self, user):
        c = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"username": user, "password": DEMO_PASSWORD, "_csrf": tok})
        c.tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/profile").get_data(as_text=True)).group(1)
        return c

    def sql(self, q, params=()):
        conn = sqlite3.connect(self.app.config["DATABASE"])
        try:
            return conn.execute(q, params).fetchall()
        finally:
            conn.close()

    def test_official_holidays_loaded_once_and_shown(self):
        self.assertIn("23 added", self.first_load)
        self.assertIn("0 added, 0 updated", self.second_load)
        page = self.login("rahul").get("/holidays?year=2026").get_data(as_text=True)
        self.assertIn("Gudhi Padwa", page)
        self.assertIn("Marathi New Year", page)
        self.assertIn("(weekly off)", page)  # e.g. Mahashivratri falls on a Sunday in 2026
        cal = self.login("rahul").get("/dashboard?y=2026&m=11").get_data(as_text=True)
        self.assertIn("Bhaubeej", cal)

    def test_holidays_not_counted_as_leave(self):
        from hrms.services.leave import count_days
        with self.app.app_context():
            lt = {"count_non_working_days": 0}
            # Mon 9 Nov - Fri 13 Nov 2026: 10 and 11 Nov are holidays
            self.assertEqual(count_days(lt, date(2026, 11, 9), date(2026, 11, 13)), 3)

    def test_birthdays_day_and_month_only_with_opt_out(self):
        from hrms.services.calendar_events import birthdays_between
        rahul = self.login("rahul")
        r = rahul.post("/profile/birthday", data={"_csrf": rahul.tok, "date_of_birth": "1991-03-14", "show_birthday": "1"})
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            names = [b["full_name"] for b in birthdays_between(date(2026, 3, 1), date(2026, 3, 31))]
        self.assertIn("Rahul Joshi", names)
        cal = self.login("priya").get("/dashboard?y=2026&m=3").get_data(as_text=True)
        self.assertIn("Birthday: Rahul Joshi", cal)
        self.assertNotIn("1991", cal)

        r = rahul.post("/profile/birthday", data={"_csrf": rahul.tok, "date_of_birth": "1991-03-14"})  # unticked
        cal = self.login("priya").get("/dashboard?y=2026&m=3").get_data(as_text=True)
        self.assertNotIn("Birthday: Rahul Joshi", cal)
        self.assertTrue(self.sql("SELECT 1 FROM audit_logs WHERE action = 'PROFILE_UPDATED'"))

        r = rahul.post("/profile/birthday", data={"_csrf": rahul.tok, "date_of_birth": "2025-01-01", "show_birthday": "1"},
                       follow_redirects=True)
        self.assertIn("valid date of birth", r.get_data(as_text=True))

    def test_leap_day_birthday(self):
        from hrms.services.calendar_events import birthdays_between
        conn = sqlite3.connect(self.app.config["DATABASE"])
        conn.execute("UPDATE employees SET date_of_birth = '1996-02-29', show_birthday = 1 WHERE emp_code = 'M103'")
        conn.commit()
        conn.close()
        with self.app.app_context():
            days = [b["date"] for b in birthdays_between(date(2027, 2, 1), date(2027, 3, 1)) if b["full_name"] == "Amit Shinde"]
        self.assertEqual(days, [date(2027, 2, 28)])


if __name__ == "__main__":
    unittest.main()
