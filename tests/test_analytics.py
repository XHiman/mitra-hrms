"""Analytics: who can see which scope, the figures behind the charts, and every view rendering.

Run:  python -m unittest tests.test_analytics -v
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


class AnalyticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "t.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "TESTING": True})
        assert cls.app.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 0
        t = date.today()
        cls.monday = t - timedelta(days=t.weekday() + 7)          # last week, fully past
        cls.emp = {r["username"]: r["employee_id"] for r in cls.sql("SELECT username, employee_id FROM users")}
        proj = {r["name"]: r["id"] for r in cls.sql("SELECT id, name FROM timesheet_projects")}
        cls.murwep = proj["MURWEP"]
        cls.book("kavita", [(0, "MURWEP", 8), (1, "MURWEP", 4), (1, "MRDP", 4)], proj, submit=True)   # reports to jtceo
        cls.book("amit", [(0, "Administration", 6)], proj)                                            # reports to js
        el = cls.sql("SELECT id FROM leave_types WHERE code = 'CL'")[0][0]
        d = (cls.monday + timedelta(days=2)).isoformat()
        cls.sql("""INSERT INTO leave_requests (employee_id, leave_type_id, from_date, to_date, days, leave_year, reason,
                   status, submitted_at) VALUES (?, ?, ?, ?, 1, 2026, 'x', 'APPROVED', 'x')""", (cls.emp["kavita"], el, d, d))

    @classmethod
    def book(cls, user, rows, proj, submit=False):
        cls.sql("INSERT INTO timesheets (employee_id, week_start, status, total_hours, updated_at) VALUES (?, ?, ?, ?, 'x')",
                (cls.emp[user], cls.monday.isoformat(), "SUBMITTED" if submit else "DRAFT", sum(r[2] for r in rows)))
        sid = cls.sql("SELECT id FROM timesheets WHERE employee_id = ?", (cls.emp[user],))[0][0]
        for seq, (day, p, h) in enumerate(rows):
            cls.sql("""INSERT INTO timesheet_entries (timesheet_id, work_date, project_id, hours, description, seq)
                       VALUES (?, ?, ?, ?, 'x', ?)""", (sid, (cls.monday + timedelta(days=day)).isoformat(), proj[p], h, seq))

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
        return c

    def test_1_who_sees_what(self):
        for user in ("rahul", "admin"):
            self.assertEqual(self.login(user).get("/analytics/").status_code, 403, user)
        jt = self.login("jtceo")
        page = jt.get("/analytics/?scope=team&period=3").get_data(as_text=True)
        self.assertIn("Kavita Pawar", page)
        self.assertNotIn("Amit Shinde", page)                      # not in the Joint CEO's reporting chain
        self.assertIn("Amit Shinde", jt.get("/analytics/?scope=office&period=3").get_data(as_text=True))
        js = self.login("js")
        self.assertIn("Amit Shinde", js.get("/analytics/?scope=team&period=3").get_data(as_text=True))
        self.assertEqual(js.get("/analytics/?scope=office").status_code, 403)   # team only for a Joint Secretary
        self.assertNotIn('value="office"', js.get("/analytics/").get_data(as_text=True))
        hr = self.login("hr")
        dept = self.sql("SELECT department_id FROM employees WHERE id = ?", (self.emp["kavita"],))[0][0]
        self.assertIn("Kavita Pawar", hr.get(f"/analytics/?scope=dept:{dept}&period=3").get_data(as_text=True))
        self.assertIn("Analytics", jt.get("/dashboard").get_data(as_text=True))

    def test_2_figures(self):
        from hrms.services import analytics as an
        with self.app.app_context():
            emps = [e for e in an.employees_in({"permissions": {"analytics.view_office"}, "employee_id": 0}, "office")
                    if e["id"] in (self.emp["kavita"], self.emp["amit"])]
            d = an.compute(emps, self.monday, self.monday + timedelta(days=6))
        kav = next(p for p in d["people"] if p["emp"]["id"] == self.emp["kavita"])
        amit = next(p for p in d["people"] if p["emp"]["id"] == self.emp["amit"])
        ideal = 8 * 5  # holidays count as weekdays by default
        self.assertEqual(kav["ideal"], ideal)
        self.assertEqual((kav["project"], kav["leave_hours"], kav["booked"]), (16, 8, 24))
        self.assertEqual(kav["util"], round(100 * 24 / ideal))
        self.assertEqual((kav["weeks_due"], kav["weeks_submitted"]), (1, 1))
        self.assertEqual((amit["booked"], amit["weeks_submitted"]), (6, 0))
        self.assertEqual(d["people"][0]["emp"]["id"], self.emp["kavita"])    # highest utilisation first
        self.assertEqual(d["project_rows"][0]["name"], "MURWEP")
        self.assertEqual(d["project_rows"][0]["hours"], 12)
        self.assertEqual(d["total"]["booked"], 30)
        self.assertEqual(d["leave_rows"][0]["days"], 1)

    def test_3_every_view_renders(self):
        ceo = self.login("ceo")
        for scope in ("team", "office"):
            for period in ("this", "last", "3", "6", "12"):
                for view in ("people", "projects", "departments", "leave", "trend"):
                    r = ceo.get(f"/analytics/?scope={scope}&period={period}&view={view}")
                    self.assertEqual(r.status_code, 200, (scope, period, view))
        page = ceo.get(f"/analytics/?scope=office&period=3&view=projects&project={self.murwep}").get_data(as_text=True)
        self.assertIn("MURWEP: hours by person", page)
        self.assertIn("Kavita Pawar", page)
        trend = ceo.get("/analytics/?scope=office&period=6&view=trend").get_data(as_text=True)
        self.assertIn("<polyline", trend)
        self.assertEqual(ceo.get("/analytics/?scope=dept:99999").status_code, 403)

    def test_4_group_filter(self):
        """YP / RO / all others filter on Analytics and Team timesheets (runs last: changes designations)."""
        for name, user in (("Young Professional", "kavita"), ("Research Officer", "amit")):
            self.sql("INSERT OR IGNORE INTO designations (name) VALUES (?)", (name,))
            did = self.sql("SELECT id FROM designations WHERE name = ?", (name,))[0][0]
            self.sql("UPDATE employees SET designation_id = ? WHERE id = ?", (did, self.emp[user]))
        ceo = self.login("ceo")
        page = lambda url: ceo.get(url).get_data(as_text=True)  # noqa: E731
        yp = page("/analytics/?scope=office&period=3&group=yp")
        self.assertIn("Kavita Pawar", yp)
        self.assertNotIn("Amit Shinde", yp)
        self.assertIn("Young Professionals only", yp)
        ro = page("/analytics/?scope=office&period=3&group=ro&view=projects")
        self.assertIn("Administration", ro)          # Amit's project
        self.assertNotIn(">MURWEP<", ro)             # Kavita's project is filtered out
        others = page("/analytics/?scope=office&period=3&group=other")
        self.assertNotIn("Kavita Pawar", others)
        self.assertNotIn("Amit Shinde", others)
        self.assertIn("Rahul Joshi", others)
        self.assertIn("group=yp", page("/analytics/?scope=office&group=yp"))          # tabs keep the filter
        self.assertIn("Kavita Pawar", page("/analytics/?scope=office&group=bogus&period=3"))  # unknown = all
        team = page(f"/timesheet/team?week={self.monday}&group=ro")
        self.assertIn("Amit Shinde", team)
        self.assertNotIn("Kavita Pawar", team)
        self.assertIn("of 1 staff", team)
        self.assertIn("Kavita Pawar", page(f"/timesheet/team?week={self.monday}&group=yp"))


if __name__ == "__main__":
    unittest.main()
