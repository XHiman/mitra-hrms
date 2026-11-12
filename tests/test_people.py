"""Reporting line and projects on a person's page: HR sets the reporting officer, team members and
projects; staff see their own read-only; no circular lines, no editing your own record.

Run:  python -m unittest tests.test_people -v
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


class PeopleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "t.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "TESTING": True})
        assert cls.app.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 0
        cls.emp = {r[0]: r[1] for r in cls.sql("SELECT username, employee_id FROM users")}
        cls.proj = {r[0]: r[1] for r in cls.sql("SELECT name, id FROM timesheet_projects")}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def sql(cls, q, params=()):
        conn = sqlite3.connect(cls.app.config["DATABASE"])
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

    def post(self, c, url, **data):
        return c.post(url, data={"_csrf": c.tok, **data}, follow_redirects=True).get_data(as_text=True)

    def ro_of(self, user):
        return self.sql("SELECT reporting_officer_id FROM employees WHERE id = ?", (self.emp[user],))[0][0]

    def test_1_hr_sets_reporting_officer_team_and_projects(self):
        hr = self.login("hr")
        rahul, amit, js = self.emp["rahul"], self.emp["amit"], self.emp["js"]
        page = hr.get(f"/employees/{rahul}").get_data(as_text=True)
        self.assertIn("Reporting line &amp; projects", page)
        self.assertIn('name="reporting_officer_id"', page)

        self.assertIn("Reporting officer updated", self.post(hr, f"/employees/{rahul}/reporting-officer",
                                                            reporting_officer_id=str(js)))
        self.assertEqual(self.ro_of("rahul"), js)
        self.assertIn("circular", self.post(hr, f"/employees/{js}/reporting-officer", reporting_officer_id=str(rahul)))
        self.assertIn("themselves", self.post(hr, f"/employees/{rahul}/reporting-officer", reporting_officer_id=str(rahul)))

        self.assertIn("now reports here", self.post(hr, f"/employees/{rahul}/team", member_id=str(amit)))
        self.assertEqual(self.ro_of("amit"), rahul)
        self.assertIn("Amit Shinde", hr.get(f"/employees/{rahul}").get_data(as_text=True))
        self.assertIn("no longer reports here", self.post(hr, f"/employees/{rahul}/team/{amit}/remove"))
        self.assertIsNone(self.ro_of("amit"))

        self.assertIn("Added to MURWEP", self.post(hr, f"/employees/{rahul}/projects", project_id=str(self.proj["MURWEP"])))
        self.assertIn("already on MURWEP", self.post(hr, f"/employees/{rahul}/projects", project_id=str(self.proj["MURWEP"])))
        r = self.login("rahul")
        prof = r.get("/profile").get_data(as_text=True)
        self.assertIn("MURWEP", prof)
        self.assertNotIn('name="reporting_officer_id"', prof)                   # read only for the employee
        cal = r.get("/timesheet/?day=" + self.sql("SELECT date('now', '-1 day')")[0][0]).get_data(as_text=True)
        self.assertIn('optgroup label="My projects"', cal)
        self.assertTrue(self.sql("SELECT 1 FROM notifications n JOIN users u ON u.id = n.user_id "
                                 "WHERE u.username = 'rahul' AND n.title = 'You have been added to a project'"))
        self.assertIn("Removed from MURWEP", self.post(hr, f"/employees/{rahul}/projects/{self.proj['MURWEP']}/remove"))
        actions = {a[0] for a in self.sql("SELECT action FROM audit_logs WHERE entity = 'employee'")}
        self.assertTrue({"REPORTING_OFFICER_CHANGED", "PROJECT_MEMBER_ADDED", "PROJECT_MEMBER_REMOVED"} <= actions)

    def test_2_who_may_change_it(self):
        rahul = self.login("rahul")
        self.assertEqual(rahul.post(f"/employees/{self.emp['priya']}/reporting-officer",
                                    data={"_csrf": rahul.tok, "reporting_officer_id": ""}).status_code, 403)
        hr = self.login("hr")
        self.assertIn("cannot change your own", self.post(hr, f"/employees/{self.emp['hr']}/reporting-officer",
                                                          reporting_officer_id=""))
        ceo = self.login("ceo")   # can view people, but not change reporting lines
        self.assertNotIn('name="reporting_officer_id"', ceo.get(f"/employees/{self.emp['priya']}").get_data(as_text=True))
        self.assertEqual(ceo.post(f"/employees/{self.emp['priya']}/team",
                                  data={"_csrf": ceo.tok, "member_id": str(self.emp['rahul'])}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
