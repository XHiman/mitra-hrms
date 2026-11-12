"""End-to-end tests for the pilot leave lifecycle, RBAC and audit.

Run:  python -m unittest discover -s tests -v
"""
import io
import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrms import create_app  # noqa: E402
from hrms.cli import DEMO_PASSWORD  # noqa: E402


def next_weekday(start, offset_days):
    d = start + timedelta(days=offset_days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class _DB:
    """Each statement runs on its own short-lived connection, so the test never holds a read
    lock while the app writes (matters with SQLite's rollback-journal modes)."""

    def __init__(self, path):
        self.path = path

    def execute(self, sql, params=()):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            return _Rows(conn.execute(sql, params).fetchall())
        finally:
            conn.close()


class PilotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.app = create_app({
            "DATABASE": str(Path(cls.tmp) / "test.db"),
            "UPLOAD_DIR": str(Path(cls.tmp) / "uploads"),
            "BACKUP_DIR": str(Path(cls.tmp) / "backups"),
            "TESTING": True,
        })
        result = cls.app.test_cli_runner().invoke(args=["seed-demo"])
        assert result.exit_code == 0, result.output

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def client_for(self, username):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"username": username, "password": DEMO_PASSWORD, "_csrf": token})
        self.assertEqual(r.status_code, 302, f"login failed for {username}")
        c.csrf = token
        return c

    def token(self, c, url="/notifications/"):
        page = c.get(url).get_data(as_text=True)
        return re.search(r'name="_csrf" value="([^"]+)"', page).group(1)

    def db(self):
        return _DB(self.app.config["DATABASE"])

    def configure_entitlements(self, admin):
        t = self.token(admin, "/admin/leave-types")
        for lt in self.db().execute("SELECT * FROM leave_types WHERE credit_based = 0"):
            data = {"_csrf": t, "code": lt["code"], "name": lt["name"], "description": lt["description"] or "",
                    "balance_tracked": "1", "annual_entitlement": "10", "is_active": "1"}
            if lt["code"] == "SL":
                data["document_after_days"] = "2"
            r = admin.post(f"/admin/leave-types/{lt['id']}", data=data)
            self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])

    def apply(self, c, type_code, start, end, reason="Personal work", document=None):
        lt = self.db().execute("SELECT id FROM leave_types WHERE code = ?", (type_code,)).fetchone()["id"]
        data = {"_csrf": self.token(c, "/leave/apply"), "leave_type_id": str(lt), "from_date": start.isoformat(),
                "to_date": end.isoformat(), "reason": reason}
        if document:
            data["document"] = document
        return c.post("/leave/apply", data=data, content_type="multipart/form-data")

    # ---------------------------------------------------------------- tests
    def test_01_cannot_apply_before_entitlement_configured(self):
        rahul = self.client_for("rahul")
        d = next_weekday(date.today(), 20)
        r = self.apply(rahul, "CL", d, d)
        self.assertEqual(r.status_code, 200)
        self.assertIn("has not been configured", r.get_data(as_text=True))

    def test_02_full_lifecycle(self):
        admin = self.client_for("admin")
        self.configure_entitlements(admin)

        rahul = self.client_for("rahul")
        start = next_weekday(date.today(), 14)
        end = start + timedelta(days=(4 - start.weekday()) if start.weekday() < 4 else 0)  # to Friday
        r = self.apply(rahul, "CL", start, end)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:800])
        req_id = int(r.headers["Location"].rstrip("/").split("/")[-1])

        db = self.db()
        req = db.execute("SELECT * FROM leave_requests WHERE id = ?", (req_id,)).fetchone()
        self.assertEqual(req["status"], "PENDING")
        expected_days = sum(1 for i in range((end - start).days + 1) if (start + timedelta(i)).weekday() < 5)
        self.assertEqual(req["days"], expected_days)
        bal = db.execute("SELECT * FROM leave_balances WHERE employee_id = ? AND leave_type_id = ?",
                         (req["employee_id"], req["leave_type_id"])).fetchone()
        self.assertEqual(bal["pending"], expected_days)

        # Reporting officer (Joint CEO) is notified; JS (not in chain) cannot act.
        jt = db.execute("SELECT id FROM users WHERE username = 'jtceo'").fetchone()["id"]
        self.assertTrue(db.execute("SELECT 1 FROM notifications WHERE user_id = ? AND title LIKE 'New leave request%'",
                                   (jt,)).fetchone())
        js = self.client_for("js")
        r = js.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(js), "decision": "approve",
                                                         "stage": "1", "remarks": ""})
        self.assertEqual(r.status_code, 403)

        # Admin has no approval rights.
        r = admin.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(admin), "decision": "approve",
                                                            "stage": "1"})
        self.assertEqual(r.status_code, 403)

        # Rahul cannot approve his own request.
        r = rahul.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(rahul), "decision": "approve",
                                                            "stage": "1"})
        self.assertEqual(r.status_code, 403)

        # Reporting officer sees it in the supervisory dashboard and approves.
        jtc = self.client_for("jtceo")
        self.assertIn("Rahul Joshi", jtc.get("/supervise").get_data(as_text=True))
        r = jtc.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(jtc), "decision": "approve",
                                                          "stage": "1", "remarks": "Approved, ensure handover"})
        self.assertEqual(r.status_code, 302)

        req = db.execute("SELECT * FROM leave_requests WHERE id = ?", (req_id,)).fetchone()
        self.assertEqual(req["status"], "APPROVED")
        bal = db.execute("SELECT * FROM leave_balances WHERE id = ?", (bal["id"],)).fetchone()
        self.assertEqual((bal["pending"], bal["used"]), (0, expected_days))

        # Double action is refused.
        r = jtc.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(jtc), "decision": "approve", "stage": "1"})
        self.assertIn(r.status_code, (302, 403))
        self.assertEqual(db.execute("SELECT status FROM leave_requests WHERE id = ?", (req_id,)).fetchone()[0], "APPROVED")

        # Employee notified; audit trail recorded.
        rahul_uid = db.execute("SELECT id FROM users WHERE username = 'rahul'").fetchone()["id"]
        self.assertTrue(db.execute("SELECT 1 FROM notifications WHERE user_id = ? AND title = 'Leave approved'",
                                   (rahul_uid,)).fetchone())
        actions = [r["action"] for r in db.execute(
            "SELECT action FROM audit_logs WHERE entity = 'leave_request' AND entity_id = ? ORDER BY id", (str(req_id),))]
        self.assertEqual(actions[:2], ["LEAVE_SUBMITTED", "LEAVE_APPROVED"])

        # Employee cancels approved future leave -> balance restored.
        r = rahul.post(f"/leave/{req_id}/cancel", data={"_csrf": self.token(rahul), "reason": "Plans changed"})
        self.assertEqual(r.status_code, 302)
        bal = db.execute("SELECT * FROM leave_balances WHERE id = ?", (bal["id"],)).fetchone()
        self.assertEqual(bal["used"], 0)

    def test_03_reject_requires_remarks_and_overlap_blocked(self):
        priya = self.client_for("priya")
        d = next_weekday(date.today(), 30)
        r = self.apply(priya, "CL", d, d)
        self.assertEqual(r.status_code, 302)
        req_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        r = self.apply(priya, "CL", d, d)
        self.assertIn("overlap", r.get_data(as_text=True))

        jtc = self.client_for("jtceo")
        jtc.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(jtc), "decision": "reject", "stage": "1",
                                                      "remarks": ""})
        self.assertEqual(self.db().execute("SELECT status FROM leave_requests WHERE id = ?", (req_id,)).fetchone()[0],
                         "PENDING")
        jtc.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(jtc), "decision": "reject", "stage": "1",
                                                      "remarks": "Project deadline"})
        row = self.db().execute("SELECT status, decision_remarks FROM leave_requests WHERE id = ?", (req_id,)).fetchone()
        self.assertEqual(tuple(row), ("REJECTED", "Project deadline"))
        self.assertIn("Project deadline", priya.get(f"/leave/{req_id}").get_data(as_text=True))

    def test_04_document_rules_and_upload_validation(self):
        kavita = self.client_for("kavita")
        start = next_weekday(date.today(), 40)
        days = [start]
        while len(days) < 3:
            days.append(next_weekday(days[-1], 1))
        r = self.apply(kavita, "SL", start, days[-1])
        self.assertIn("supporting document is mandatory", r.get_data(as_text=True).lower())
        fake = (io.BytesIO(b"MZ not really a pdf"), "cert.pdf")
        r = self.apply(kavita, "SL", start, days[-1], document=fake)
        self.assertIn("does not match", r.get_data(as_text=True))
        good = (io.BytesIO(b"%PDF-1.4\n%test\n"), "cert.pdf")
        r = self.apply(kavita, "SL", start, days[-1], document=good)
        self.assertEqual(r.status_code, 302)
        req_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        doc = kavita.get(f"/leave/{req_id}/document")
        self.assertEqual(doc.status_code, 200)
        doc.close()
        # Another employee cannot read it.
        self.assertEqual(self.client_for("amit").get(f"/leave/{req_id}/document").status_code, 403)

    def test_05_rbac_pages(self):
        emp = self.client_for("amit")
        for url in ("/supervise", "/approvals/", "/admin/", "/admin/users", "/admin/roles", "/admin/leave-types",
                    "/admin/workflows", "/leave/records", "/admin/backups", "/compoff/approvals"):
            self.assertEqual(emp.get(url).status_code, 403, url)
        hr = self.client_for("hr")
        self.assertEqual(hr.get("/supervise").status_code, 200)
        for url in ("/admin/", "/admin/users", "/admin/leave-types", "/admin/workflows"):
            self.assertEqual(hr.get(url).status_code, 403, url)
        admin = self.client_for("admin")
        for url in ("/admin/", "/admin/users", "/admin/roles", "/admin/org", "/admin/leave-types", "/admin/workflows",
                    "/admin/settings", "/admin/backups", "/audit/?verify=1"):
            self.assertEqual(admin.get(url).status_code, 200, url)
        self.assertEqual(admin.get("/approvals/").status_code, 403)
        self.assertEqual(admin.get("/compoff/approvals").status_code, 403)
        # Employee sees only own requests
        other = self.db().execute("SELECT r.id FROM leave_requests r JOIN users u ON u.employee_id = r.employee_id "
                                  "WHERE u.username != 'amit' LIMIT 1").fetchone()["id"]
        self.assertEqual(emp.get(f"/leave/{other}").status_code, 403)

    def test_06_csrf_and_auth_required(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/dashboard").status_code, 302)
        c = self.client_for("rahul")
        self.assertEqual(c.post("/notifications/read-all", data={}).status_code, 400)

    def test_07_sod_role_guard(self):
        admin = self.client_for("admin")
        role_id = self.db().execute("SELECT id FROM roles WHERE code = 'HR'").fetchone()["id"]
        r = admin.post(f"/admin/roles/{role_id}", data={"_csrf": self.token(admin, "/admin/roles"), "name": "HR",
                                                        "perm": ["leave.approve", "users.manage"]})
        self.assertIn("Segregation of duties", r.get_data(as_text=True))

    def test_08_audit_is_immutable_and_chained(self):
        conn = self.db()
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE audit_logs SET action = 'X' WHERE id = 1")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM audit_logs")
        admin = self.client_for("admin")
        self.assertIn("hash chain intact", admin.get("/audit/?verify=1").get_data(as_text=True))

    def test_09_lockout(self):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        for _ in range(5):
            c.post("/login", data={"username": "kavita", "password": "wrong", "_csrf": token})
        r = c.post("/login", data={"username": "kavita", "password": DEMO_PASSWORD, "_csrf": token})
        self.assertIn("temporarily locked", r.get_data(as_text=True))

    def test_11_multistage_workflow_and_admin_pages(self):
        admin = self.client_for("admin")
        db = self.db()
        hr_role = db.execute("SELECT id FROM roles WHERE code = 'HR'").fetchone()["id"]
        wf = db.execute("SELECT id FROM approval_workflows WHERE is_default = 1").fetchone()["id"]
        r = admin.post(f"/admin/workflows/{wf}", data={
            "_csrf": self.token(admin), "name": "Standard", "description": "", "is_default": "1", "is_active": "1",
            "stage_order_0": "1", "stage_name_0": "Reporting Officer", "stage_type_0": "REPORTING_OFFICER",
            "stage_fallback_0": str(hr_role),
            "stage_order_1": "2", "stage_name_1": "HR verification", "stage_type_1": "ROLE",
            "stage_role_1": str(hr_role), "stage_min_days_1": "2"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:600])

        amit = self.client_for("amit")  # reports to JS
        start = next_weekday(date.today(), 60)
        while start.weekday() != 0:
            start += timedelta(days=1)
        r = self.apply(amit, "EL", start, start + timedelta(days=2))  # Mon-Wed = 3 days > 2
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:600])
        req_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        js, hr = self.client_for("js"), self.client_for("hr")
        self.assertEqual(hr.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(hr), "decision": "approve",
                                                                      "stage": "1"}).status_code, 403)
        js.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(js), "decision": "approve", "stage": "1"})
        self.assertEqual(db.execute("SELECT status, current_stage FROM leave_requests WHERE id = ?",
                                    (req_id,)).fetchone()[:], ("PENDING", 2))
        hr.post(f"/approvals/{req_id}/decide", data={"_csrf": self.token(hr), "decision": "approve", "stage": "2",
                                                     "remarks": "Verified"})
        self.assertEqual(db.execute("SELECT status FROM leave_requests WHERE id = ?", (req_id,)).fetchone()[0], "APPROVED")
        self.assertEqual(hr.get(f"/leave/{req_id}").status_code, 200)

        # 1-day request skips the HR stage.
        r = self.apply(amit, "EL", start + timedelta(days=7), start + timedelta(days=7))
        rid2 = int(r.headers["Location"].rstrip("/").split("/")[-1])
        self.assertEqual(db.execute("SELECT status FROM request_approvals WHERE request_id = ? AND seq = 2",
                                    (rid2,)).fetchone()[0], "SKIPPED")

        # Create a user -> one-time password -> forced change.
        lookup = {k: db.execute(q).fetchone()[0] for k, q in {
            "role": "SELECT id FROM roles WHERE code = 'EMPLOYEE'", "dept": "SELECT id FROM departments LIMIT 1",
            "ro": "SELECT id FROM employees WHERE emp_code = 'M002'"}.items()}
        r = admin.post("/admin/users/new", data={
            "_csrf": self.token(admin), "emp_code": "M200", "full_name": "Test Person", "username": "tperson",
            "role_id": lookup["role"], "department_id": lookup["dept"], "reporting_officer_id": lookup["ro"],
            "employment_type": "Permanent", "joining_date": "2026-01-01"})
        body = r.get_data(as_text=True)
        pw = re.search(r'class="secret">([^<]+)<', body).group(1)
        c = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"username": "tperson", "password": pw, "_csrf": tok})
        self.assertIn("/account/password", c.get("/dashboard").headers["Location"])

        uid = db.execute("SELECT id FROM users WHERE username = 'rahul'").fetchone()["id"]
        emp = db.execute("SELECT employee_id FROM users WHERE id = ?", (uid,)).fetchone()[0]
        for url in (f"/admin/users/{uid}", "/admin/roles/1", "/admin/roles/new", "/admin/leave-types/1",
                    "/admin/leave-types/new", f"/admin/workflows/{wf}", "/admin/workflows/new", "/admin/org",
                    f"/employees/{emp}", "/notifications/"):
            self.assertEqual(admin.get(url).status_code, 200, url)
        self.assertEqual(self.client_for("ceo").get(f"/employees/{emp}").status_code, 200)
        self.assertEqual(self.client_for("priya").get(f"/employees/{emp}").status_code, 403)

    def test_12_compoff_claim_approved_by_assigning_officer_then_availed(self):
        db = self.db()
        t = date.today()
        saturday = t - timedelta(days=(t.weekday() - 5) % 7)  # most recent Saturday (today if Saturday)
        weekday = next_weekday(t - timedelta(days=14), 0)
        rahul = self.client_for("rahul")  # reports to jtceo
        js_uid = db.execute("SELECT id FROM users WHERE username = 'js'").fetchone()["id"]
        admin_uid = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]

        page = rahul.get("/compoff/").get_data(as_text=True)
        self.assertIn("(reporting officer)", page)
        self.assertNotIn("Dev Admin", page)  # admin holds no comp-off approval rights

        def claim(c, worked, approver, days="1", details="Assembly session briefing"):
            return c.post("/compoff/", data={"_csrf": self.token(c, "/compoff/"), "worked_date": worked.isoformat(),
                                             "days": days, "approver_user_id": str(approver), "work_details": details})

        self.assertIn("weekly off or a holiday", claim(rahul, weekday, js_uid).get_data(as_text=True))
        self.assertIn("already been done", claim(rahul, t + timedelta(days=7), js_uid).get_data(as_text=True))
        self.assertIn("officer who assigned", claim(rahul, saturday, admin_uid).get_data(as_text=True))
        self.assertEqual(claim(rahul, saturday, js_uid).status_code, 302)
        self.assertIn("already have a comp-off claim", claim(rahul, saturday, js_uid).get_data(as_text=True))
        cid = db.execute("SELECT id FROM compoff_claims WHERE status = 'PENDING' ORDER BY id DESC").fetchone()["id"]
        self.assertTrue(db.execute("SELECT 1 FROM notifications WHERE user_id = ? AND title LIKE 'New comp-off%'",
                                   (js_uid,)).fetchone())

        # Only the assigning officer (JS) may decide - not rahul's reporting officer, nor admin, nor rahul.
        for who in ("jtceo", "rahul"):
            c = self.client_for(who)
            r = c.post(f"/compoff/{cid}/decide", data={"_csrf": self.token(c), "decision": "approve"})
            self.assertEqual(r.status_code, 403, who)
        admin = self.client_for("admin")
        self.assertEqual(admin.post(f"/compoff/{cid}/decide", data={"_csrf": self.token(admin),
                                                                   "decision": "approve"}).status_code, 403)
        js = self.client_for("js")
        self.assertIn("Rahul Joshi", js.get("/supervise").get_data(as_text=True))
        js.post(f"/compoff/{cid}/decide", data={"_csrf": self.token(js), "decision": "approve", "remarks": "Confirmed"})
        self.assertEqual(db.execute("SELECT status FROM compoff_claims WHERE id = ?", (cid,)).fetchone()[0], "APPROVED")

        co = db.execute("SELECT id FROM leave_types WHERE credit_based = 1").fetchone()["id"]
        emp = db.execute("SELECT employee_id FROM users WHERE username = 'rahul'").fetchone()[0]
        bal = db.execute("SELECT entitlement, used FROM leave_balances WHERE employee_id = ? AND leave_type_id = ?",
                         (emp, co)).fetchone()
        self.assertEqual(bal["entitlement"], 1)
        actions = [r[0] for r in db.execute("SELECT action FROM audit_logs WHERE entity = 'compoff_claim' "
                                            "AND entity_id = ? ORDER BY id", (str(cid),))]
        self.assertEqual(actions, ["COMPOFF_CLAIMED", "COMPOFF_APPROVED"])
        self.assertIn("COMPOFF_APPROVED", rahul.get("/audit/").get_data(as_text=True))

        # Rejection needs remarks; withdrawn claims notify nobody further.
        sunday = saturday + timedelta(days=1) if saturday + timedelta(days=1) <= t else saturday - timedelta(days=6)
        claim(rahul, sunday, js_uid, days="0.5")
        cid2 = db.execute("SELECT id FROM compoff_claims WHERE status = 'PENDING' ORDER BY id DESC").fetchone()["id"]
        js.post(f"/compoff/{cid2}/decide", data={"_csrf": self.token(js), "decision": "reject", "remarks": ""})
        self.assertEqual(db.execute("SELECT status FROM compoff_claims WHERE id = ?", (cid2,)).fetchone()[0], "PENDING")
        rahul.post(f"/compoff/{cid2}/withdraw", data={"_csrf": self.token(rahul)})
        self.assertEqual(db.execute("SELECT status FROM compoff_claims WHERE id = ?", (cid2,)).fetchone()[0], "CANCELLED")

        # Avail the credit as leave: normal workflow (reporting officer), balance consumed.
        d1 = next_weekday(t, 3)
        d2 = next_weekday(d1, 1)  # a different working day, so the overlap check doesn't trigger first
        r = self.apply(rahul, "CO", d1, d1)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:600])
        r = self.apply(rahul, "CO", d2, d2)
        self.assertIn("Insufficient", r.get_data(as_text=True))

        # Balance cannot be overridden by the admin; supervisors see the claim in history.
        r = admin.post(f"/admin/users/{db.execute('SELECT id FROM users WHERE username = ?', ('rahul',)).fetchone()[0]}"
                       "/balance", data={"_csrf": self.token(admin), "leave_type_id": str(co), "entitlement": "5",
                                         "remarks": "x"}, follow_redirects=True)
        self.assertIn("only through approved comp-off claims", r.get_data(as_text=True))
        self.assertIn("Assembly session briefing", self.client_for("ceo").get(f"/employees/{emp}").get_data(as_text=True))

    def test_13_monthly_accrual_and_cycles(self):
        from hrms.services.cycles import Cycle, cycle_for
        from hrms.services.leave import credited
        lt = {"accrual_per_month": 1.5, "credit_based": 0}
        cal = Cycle(date(2026, 1, 1), date(2026, 12, 31), "CALENDAR")
        con = Cycle(date(2026, 8, 20), date(2027, 8, 19), "CONTRACT")
        with self.app.app_context():
            # calendar cycle: credit on the 1st of each month, from the joining month
            self.assertEqual(credited(lt, 18, False, cal, None, date(2026, 10, 7)), 15)
            self.assertEqual(credited(lt, 18, False, cal, "2026-08-20", date(2026, 10, 7)), 4.5)
            self.assertEqual(credited(lt, 18, False, cal, None, date(2026, 12, 31)), 18)
            # contract cycle: credit for each completed month
            self.assertEqual(credited(lt, 18, False, con, None, date(2026, 9, 18)), 0)
            self.assertEqual(credited(lt, 18, False, con, None, date(2026, 9, 19)), 1.5)
            self.assertEqual(credited(lt, 18, False, con, None, date(2026, 10, 7)), 1.5)
            self.assertEqual(credited(lt, 18, False, con, None, date(2027, 8, 19)), 18)
            self.assertEqual(credited(lt, 18, True, con, None, date(2026, 10, 7)), 18)  # override taken as-is

            db = sqlite3.connect(self.app.config["DATABASE"])
            emp = db.execute("SELECT id FROM employees WHERE emp_code = 'M104'").fetchone()[0]  # Kavita, joined 2024-04-01
            db.execute("INSERT OR IGNORE INTO contract_renewals (employee_id, renewal_date) VALUES (?, '2026-02-10')", (emp,))
            db.commit()
            db.close()
            c = cycle_for(emp, date(2025, 6, 1))   # first term, split at the yearly anniversary
            self.assertEqual((c.start, c.end, c.kind), (date(2025, 4, 1), date(2026, 2, 9), "CONTRACT"))
            c = cycle_for(emp, date(2026, 10, 7))  # renewed on 10 Feb 2026
            self.assertEqual((c.start, c.end), (date(2026, 2, 10), date(2027, 2, 9)))
            c = cycle_for(emp, date(2027, 3, 1))   # no further renewal recorded: repeats yearly
            self.assertEqual((c.start, c.end), (date(2027, 2, 10), date(2028, 2, 9)))

    def test_14_failed_password_change_is_explained_and_audited(self):
        c = self.client_for("priya")
        r = c.post("/account/password", data={"_csrf": self.token(c, "/account/password"), "current_password": "wrong",
                                              "new_password": "Priya2026xx", "confirm_password": "Priya2026xy"})
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        for msg in ("is not correct", "do not match", "a special character"):
            self.assertIn(msg, body)
        row = self.db().execute("SELECT remarks FROM audit_logs WHERE action = 'PASSWORD_CHANGE_FAILED' "
                                "ORDER BY id DESC").fetchone()
        self.assertIn("special character", row["remarks"])
        self.assertNotIn("Priya2026", row["remarks"])  # passwords are never written to the log

    def test_10_dashboards_render(self):
        for user, url in (("rahul", "/dashboard"), ("rahul", "/profile"), ("ceo", "/supervise"), ("hr", "/supervise"),
                          ("ceo", "/leave/records"), ("admin", "/admin/"), ("rahul", "/audit/"), ("js", "/audit/"),
                          ("rahul", "/leave/apply"), ("rahul", "/leave/my"), ("admin", "/admin/users/new"),
                          ("rahul", "/compoff/"), ("hr", "/compoff/approvals"), ("admin", "/admin/leave-types")):
            self.assertEqual(self.client_for(user).get(url).status_code, 200, (user, url))


if __name__ == "__main__":
    unittest.main()
