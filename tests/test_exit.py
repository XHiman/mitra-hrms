"""Resignation and exit: submit, reporting officer forwards, HR accepts, clearance, relieving (login closed),
withdrawal, revocation, HR's own resignation, and who may see or act.

Run:  python -m unittest tests.test_exit -v
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


class ExitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "t.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "TESTING": True})
        assert cls.app.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 0

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

    def resign(self, c, lwd, **extra):
        data = {"_csrf": c.tok, "reason_category": "Higher studies", "reason": "Joining a master's programme.",
                "proposed_lwd": lwd.isoformat(), "confirm": "1"}
        data.update(extra)
        return c.post("/exit/", data=data)

    def act(self, c, rid, action, **extra):
        return c.post(f"/exit/{rid}/act", data={"_csrf": c.tok, "action": action, **extra}, follow_redirects=True)

    def status(self, rid):
        return self.sql("SELECT status FROM resignations WHERE id = ?", (rid,))[0][0]

    def test_1_full_exit_to_relieving(self):
        priya = self.login("priya")
        self.assertIn("Submit resignation", priya.get("/exit/").get_data(as_text=True))
        page = self.resign(priya, date.today() + timedelta(days=10), confirm="").get_data(as_text=True)
        self.assertIn("Tick the box", page)
        page = self.resign(priya, date.today() - timedelta(days=1)).get_data(as_text=True)
        self.assertIn("cannot be before", page)
        r = self.resign(priya, date.today() + timedelta(days=10))
        self.assertEqual(r.status_code, 302)
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.assertEqual(self.status(rid), "WITH_OFFICER")
        self.assertIn("already have a resignation", self.resign(priya, date.today() + timedelta(days=12))
                      .get_data(as_text=True))

        # a colleague cannot see it; HR can see it but must wait for the reporting officer
        self.assertEqual(self.login("rahul").get(f"/exit/{rid}").status_code, 403)
        hr = self.login("hr")
        self.assertEqual(hr.get(f"/exit/{rid}").status_code, 200)
        self.act(hr, rid, "accept", accepted_lwd=date.today().isoformat())
        self.assertEqual(self.status(rid), "WITH_OFFICER")
        self.assertEqual(hr.post(f"/exit/{rid}/act", data={"_csrf": hr.tok, "action": "forward",
                                                           "remarks": "x"}).status_code, 403)

        jtceo = self.login("jtceo")
        self.assertIn("Priya Nair", jtceo.get("/pending").get_data(as_text=True))
        self.assertIn("Remarks is required", self.act(jtceo, rid, "forward", remarks="").get_data(as_text=True))
        self.act(jtceo, rid, "forward", remarks="Handover to Rahul agreed.")
        self.assertEqual(self.status(rid), "WITH_HR")
        self.assertEqual(self.act(priya, rid, "withdraw").status_code, 200)  # still allowed before acceptance...
        self.assertEqual(self.status(rid), "WITHDRAWN")

        # ...so resign again and go all the way
        r = self.resign(priya, date.today() + timedelta(days=10))
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.act(jtceo, rid, "forward", remarks="OK.")
        self.assertIn("Priya Nair", hr.get("/pending").get_data(as_text=True))
        self.act(hr, rid, "accept", accepted_lwd=date.today().isoformat(), remarks="Notice waived.")
        self.assertEqual(self.status(rid), "ACCEPTED")
        self.assertIn("speak to HR", self.act(priya, rid, "withdraw").get_data(as_text=True))
        items = self.sql("SELECT seq FROM exit_clearance WHERE resignation_id = ?", (rid,))
        self.assertEqual(len(items), 5)
        page = self.act(hr, rid, "relieve").get_data(as_text=True)
        self.assertIn("exit clearance items are not complete", page)
        self.assertEqual(self.status(rid), "ACCEPTED")
        for (seq,) in items:
            hr.post(f"/exit/{rid}/clearance/{seq}", data={"_csrf": hr.tok, "done": "1", "note": "ok"})
        page = self.act(hr, rid, "relieve", remarks="All clear.").get_data(as_text=True)
        self.assertIn("login is now closed", page)
        self.assertEqual(self.status(rid), "RELIEVED")
        self.assertEqual(tuple(self.sql("SELECT u.is_active, e.status FROM users u JOIN employees e "
                                        "ON e.id = u.employee_id WHERE u.username = 'priya'")[0]), (0, "INACTIVE"))
        self.assertEqual(priya.get("/exit/").status_code, 302)       # existing session ends
        actions = {r[0] for r in self.sql("SELECT action FROM audit_logs WHERE entity = 'resignation'")}
        self.assertTrue({"RESIGNATION_SUBMITTED", "RESIGNATION_FORWARDED", "RESIGNATION_WITHDRAWN",
                         "RESIGNATION_ACCEPTED", "EXIT_CLEARANCE_UPDATED", "EMPLOYEE_RELIEVED"} <= actions)

    def test_2_revoke_and_date_rule(self):
        rahul, jtceo, hr = self.login("rahul"), self.login("jtceo"), self.login("hr")
        r = self.resign(rahul, date.today() + timedelta(days=30))
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.act(jtceo, rid, "forward", remarks="Fine.")
        self.act(hr, rid, "accept", accepted_lwd=(date.today() + timedelta(days=30)).isoformat())
        page = hr.get(f"/exit/{rid}").get_data(as_text=True)
        self.assertIn("has not been reached", page)
        self.assertNotIn('value="relieve"', page)
        self.assertIn("Remarks is required", self.act(hr, rid, "revoke", remarks="").get_data(as_text=True))
        self.act(hr, rid, "revoke", remarks="Employee is staying on.")
        self.assertEqual(self.status(rid), "REVOKED")
        self.assertEqual(self.sql("SELECT is_active FROM users WHERE username = 'rahul'")[0][0], 1)

    def test_3_hr_own_resignation_goes_to_ceo(self):
        hr, js, ceo = self.login("hr"), self.login("js"), self.login("ceo")
        r = self.resign(hr, date.today() + timedelta(days=20))
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.act(js, rid, "forward", remarks="Agreed.")
        self.assertEqual(hr.post(f"/exit/{rid}/act", data={"_csrf": hr.tok, "action": "accept",
                                                           "accepted_lwd": date.today().isoformat()}).status_code, 403)
        self.assertIn("Sunita Patil", ceo.get("/pending").get_data(as_text=True))
        self.act(ceo, rid, "accept", accepted_lwd=(date.today() + timedelta(days=20)).isoformat())
        self.assertEqual(self.status(rid), "ACCEPTED")

    def test_4_admin_has_no_exit_rights(self):
        admin = self.login("admin")
        self.assertEqual(admin.get("/exit/manage").status_code, 403)
        self.assertEqual(admin.get("/exit/").status_code, 200)      # but may resign like anyone else


if __name__ == "__main__":
    unittest.main()
