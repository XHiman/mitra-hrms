"""Robustness and security from MITRA's (the client's) point of view.

- Every page, for every role (and logged out), never crashes and never shows what that role may not see.
- Every form refuses a request without its security token (CSRF).
- Hostile or malformed input (script tags, SQL fragments, huge text, bad dates, odd numbers, Marathi
  text) is handled without errors and shown safely.
- Nobody can read or act on another person's leave, document, comp-off claim or salary slip.
- Sessions end on logout, deactivation and role change; tampered cookies are ignored.
- Simultaneous clicks (double approval, parallel applications) cannot corrupt balances.
- After hundreds of random actions, every balance still matches the requests behind it and the
  audit log's hash chain is intact.

Run:  python -m unittest tests.test_robustness -v
"""
import io
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrms import create_app  # noqa: E402
from hrms.cli import DEMO_PASSWORD  # noqa: E402

ROLES = ("rahul", "jtceo", "ceo", "hr", "amit", "admin")  # employee, supervisor, CEO, HR, accounts, sysadmin
XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>'
SQLI = "' OR '1'='1'; DROP TABLE users; --"


class Base(unittest.TestCase):
    def assert_balances_consistent(self):
        """Every balance equals the requests behind it, and nobody has used or reserved more than credited."""
        from hrms.services import cycles
        from hrms.services.leave import credited, leave_type
        with self.app.app_context():
            for b in self.sql("SELECT * FROM leave_balances"):
                sums = {r["status"]: r["s"] for r in self.sql(
                    """SELECT status, SUM(days) AS s FROM leave_requests WHERE employee_id = ? AND leave_type_id = ?
                       AND leave_year = ? GROUP BY status""", (b["employee_id"], b["leave_type_id"], b["leave_year"]))}
                self.assertAlmostEqual(b["used"], sums.get("APPROVED") or 0, msg=dict(b))
                self.assertAlmostEqual(b["pending"], sums.get("PENDING") or 0, msg=dict(b))
                cyc = cycles.cycle_by_key(b["employee_id"], b["leave_year"])
                cred = credited(leave_type(b["leave_type_id"]), b["entitlement"], b["is_override"], cyc)
                if cred is not None:  # credits only grow, so this holds at every moment
                    self.assertGreaterEqual(cred - b["used"] - b["pending"], -1e-9, msg=dict(b))

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "r.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "INSTANCE_DIR": str(cls.tmp), "TESTING": True})
        cli = cls.app.test_cli_runner()
        assert cli.invoke(args=["seed-demo"]).exit_code == 0
        assert cli.invoke(args=["load-holidays", "2026"]).exit_code == 0
        cls.sql("UPDATE leave_types SET annual_entitlement = 18, accrual_per_month = 1.5, allow_backdated = 1 "
                "WHERE code = 'EL'")
        cls.sql("UPDATE leave_types SET annual_entitlement = 8 WHERE code IN ('CL', 'SL')")
        cls.sql("UPDATE users SET role_id = (SELECT id FROM roles WHERE code = 'ACCOUNTS') WHERE username = 'amit'")
        cls.holidays = {r[0] for r in cls.sql("SELECT date FROM holidays")}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def sql(cls, q, params=()):
        conn = sqlite3.connect(cls.app.config["DATABASE"], timeout=30)
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
        r = c.post("/login", data={"username": user, "password": DEMO_PASSWORD, "_csrf": tok})
        assert r.status_code == 302, user
        c.tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/profile").get_data(as_text=True)).group(1)
        return c

    def workday(self, offset):
        d = date.today() + timedelta(days=offset)
        while d.weekday() >= 5 or d.isoformat() in self.holidays:
            d += timedelta(days=1)
        return d

    def type_id(self, code):
        return self.sql("SELECT id FROM leave_types WHERE code = ?", (code,))[0][0]

    def apply(self, c, start, end=None, code="EL", reason="Personal work", **extra):
        data = {"_csrf": c.tok, "leave_type_id": str(self.type_id(code)), "from_date": str(start),
                "to_date": str(end or start), "reason": reason, **extra}
        return c.post("/leave/apply", data=data, content_type="multipart/form-data")


class PageSweep(Base):
    """Every route, every role: no crashes, and restricted areas stay restricted."""

    def sample_args(self):
        one = lambda q: (self.sql(q) or [[1]])[0][0]  # noqa: E731
        c = self.login("rahul")
        self.apply(c, self.workday(10))
        return {"request_id": one("SELECT id FROM leave_requests"), "employee_id": one("SELECT id FROM employees"),
                "user_id": one("SELECT id FROM users WHERE username = 'priya'"), "role_id": 1,
                "type_id": self.type_id("EL"), "workflow_id": one("SELECT id FROM approval_workflows"),
                "slip_id": 1, "claim_id": 1, "notification_id": 1, "holiday_id": 1, "renewal_id": 1,
                "name": "hrms-20260101-000000.db", "day": self.workday(-3).isoformat(), "rid": 1, "project_id": 1}

    def test_no_page_crashes_for_any_role(self):
        args = self.sample_args()
        routes = []
        for rule in self.app.url_map.iter_rules():
            if "GET" not in rule.methods or rule.endpoint == "static":
                continue
            try:
                routes.append((rule.endpoint, rule.build({k: args[k] for k in rule.arguments}, append_unknown=False)[1]))
            except KeyError as exc:  # a new route parameter this sweep does not know yet
                self.fail(f"add a sample value for '{exc.args[0]}' used by {rule.rule}")
        self.assertGreater(len(routes), 40)
        crashes = []
        for user in ROLES + (None,):
            c = self.login(user) if user else self.app.test_client()
            for endpoint, url in routes:
                for u in (url, re.sub(r"/\d+(?=/|$)", "/999999", url)):  # also a record that does not exist
                    status = c.get(u).status_code
                    if status >= 500 or status not in (200, 302, 400, 403, 404):
                        crashes.append(f"{user or 'anonymous'} GET {u} -> {status}")
        self.assertEqual(crashes, [])

    def test_restricted_areas(self):
        args = self.sample_args()
        rules = {r.endpoint: r for r in self.app.url_map.iter_rules() if "GET" in r.methods}

        def url(ep):
            return rules[ep].build({k: args[k] for k in rules[ep].arguments}, append_unknown=False)[1]

        admin_pages = [ep for ep in rules if ep.startswith("admin.") and ep != "admin.backup_download"]
        payroll_pages = [ep for ep in rules if ep.startswith("payroll.") and not ep.startswith("payroll.my_")]
        allowed = {"admin": set(admin_pages), "hr": set(payroll_pages), "amit": set(payroll_pages)}
        leaks = []
        for user in ROLES:
            c = self.login(user)
            for ep in admin_pages + payroll_pages:
                status = c.get(url(ep)).status_code
                if ep not in allowed.get(user, set()) and status not in (403, 404):
                    leaks.append(f"{user} can open {ep} ({status})")
        anon = self.app.test_client()
        for ep in admin_pages + payroll_pages + ["dashboards.employee", "leave.records", "audit.index"]:
            if anon.get(url(ep)).status_code != 302:
                leaks.append(f"anonymous can open {ep}")
        self.assertEqual(leaks, [])

    def test_every_form_requires_its_security_token(self):
        c = self.login("admin")
        missing = []
        for rule in self.app.url_map.iter_rules():
            if "POST" not in rule.methods:
                continue
            url = rule.build({k: 1 for k in rule.arguments} | ({"name": "x.db"} if "name" in rule.arguments else {}),
                             append_unknown=False)[1]
            for client in (c, self.app.test_client()):
                if client.post(url, data={"x": "1"}).status_code != 400:
                    missing.append(url)
                if client.post(url, data={"_csrf": "forged-token"}).status_code != 400:
                    missing.append(url + " (forged token)")
        self.assertEqual(sorted(set(missing)), [])

    def test_security_headers_and_cookies(self):
        c = self.app.test_client()
        r = c.get("/login")
        for h in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
            self.assertIn(h, r.headers)
        tok = re.search(r'name="_csrf" value="([^"]+)"', r.get_data(as_text=True)).group(1)
        r = c.post("/login", data={"username": "rahul", "password": DEMO_PASSWORD, "_csrf": tok})
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertEqual(c.get("/dashboard").headers.get("Cache-Control"), "no-store")  # no caching of personal pages
        page = c.get("/leave/999999").get_data(as_text=True)
        self.assertNotIn("Traceback", page)

    def test_login_does_not_reveal_which_usernames_exist(self):
        c = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        a = c.post("/login", data={"username": "nobody.here", "password": "x", "_csrf": tok}).get_data(as_text=True)
        b = c.post("/login", data={"username": "priya", "password": "x", "_csrf": tok}).get_data(as_text=True)
        msg = lambda s: re.search(r'class="flash error"[^>]*>([^<]+)<', s).group(1)  # noqa: E731
        self.assertEqual(msg(a), msg(b))


class HostileInput(Base):
    def test_script_tags_are_shown_as_text_everywhere(self):
        rahul = self.login("rahul")
        self.assertEqual(self.apply(rahul, self.workday(12), reason=XSS).status_code, 302)
        t = date.today()
        sat = t - timedelta(days=(t.weekday() - 5) % 7)
        js_uid = self.sql("SELECT id FROM users WHERE username = 'js'")[0][0]
        rahul.post("/compoff/", data={"_csrf": rahul.tok, "worked_date": sat.isoformat(), "days": "1",
                                      "approver_user_id": str(js_uid), "work_details": XSS})
        admin = self.login("admin")
        admin.post("/admin/holidays", data={"_csrf": admin.tok, "date": "2026-12-31", "name": XSS[:80], "description": XSS})
        hr = self.login("hr")
        priya = self.sql("SELECT employee_id FROM users WHERE username = 'priya'")[0][0]
        hr.post(f"/payroll/structure/{priya}", data={"_csrf": hr.tok, "earning_name_0": XSS[:80], "earning_amount_0": "100"})
        pages = [(rahul, "/leave/my"), (rahul, "/dashboard"), (self.login("jtceo"), "/supervise"),
                 (self.login("js"), "/compoff/approvals"), (rahul, "/holidays?year=2026"), (rahul, "/notifications/"),
                 (hr, f"/payroll/structure/{priya}"), (admin, "/audit/"), (admin, "/admin/leave-types")]
        for c, url in pages:
            body = c.get(url).get_data(as_text=True)
            self.assertNotIn("<script>alert", body, url)
            self.assertNotIn("onerror=alert", body.replace("onerror=alert(1)&gt;", ""), url)

    def test_malformed_values_are_rejected_politely(self):
        rahul = self.login("rahul")
        cases = [
            {"from_date": "2026-02-30"}, {"from_date": "not-a-date"}, {"to_date": "1900-01-01"},
            {"leave_type_id": "abc"}, {"leave_type_id": "999999"}, {"leave_type_id": "-1"},
            {"reason": "x" * 20000}, {"reason": "   "}, {"from_date": "2099-01-05", "to_date": "2099-01-05"},
            {"from_date": str(self.workday(40)), "to_date": str(self.workday(30))},
        ]
        for case in cases:
            data = {"_csrf": rahul.tok, "leave_type_id": str(self.type_id("EL")), "from_date": str(self.workday(50)),
                    "to_date": str(self.workday(50)), "reason": "ok", **case}
            r = rahul.post("/leave/apply", data=data, content_type="multipart/form-data")
            self.assertEqual(r.status_code, 200, case)                  # form shown again with a message
            self.assertIn('class="flash error"', r.get_data(as_text=True), case)
        for q in (SQLI, "%", "_", "\\", "'" * 50):
            for c, url in ((self.login("jtceo"), "/leave/records?q="), (self.login("admin"), "/admin/users?q="),
                           (self.login("admin"), "/audit/?user=")):
                self.assertEqual(c.get(url + q).status_code, 200, (url, q))
        self.assertTrue(self.sql("SELECT COUNT(*) FROM users")[0][0] > 5)  # the users table is still there

        hr = self.login("hr")
        priya = self.sql("SELECT employee_id FROM users WHERE username = 'priya'")[0][0]
        for amount in ("-5", "1e9", "12.345", "abc", "9" * 15):
            r = hr.post(f"/payroll/structure/{priya}", data={"_csrf": hr.tok, "earning_name_0": "Pay",
                                                             "earning_amount_0": amount})
            self.assertEqual(r.status_code, 200, amount)
            self.assertIn('class="flash error"', r.get_data(as_text=True), amount)

    def test_marathi_names_work(self):
        admin = self.login("admin")
        role = self.sql("SELECT id FROM roles WHERE code = 'EMPLOYEE'")[0][0]
        r = admin.post("/admin/users/new", data={"_csrf": admin.tok, "emp_code": "M777", "full_name": "सुनीता पाटील",
                                                 "username": "sunita.patil", "role_id": str(role)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("सुनीता पाटील", admin.get("/admin/users?q=सुनीता").get_data(as_text=True))

    def test_upload_abuse(self):
        rahul = self.login("rahul")
        d = self.workday(60)
        cases = [((io.BytesIO(b"<html><script>alert(1)</script>"), "cert.pdf"), "does not match"),
                 ((io.BytesIO(b"%PDF-1.4"), "cert.exe"), "must be a PDF"),
                 ((io.BytesIO(b""), "cert.pdf"), "empty")]
        for doc, msg in cases:
            r = self.apply(rahul, d, document=doc)
            self.assertIn(msg, r.get_data(as_text=True))
        r = self.apply(rahul, d, document=(io.BytesIO(b"%PDF-1.4\n"), "../../../etc/passwd.pdf"))
        self.assertEqual(r.status_code, 302)
        stored = self.sql("SELECT attachment_path, attachment_name FROM leave_requests ORDER BY id DESC LIMIT 1")[0]
        self.assertNotIn("..", stored["attachment_path"])
        self.assertNotIn("/", stored["attachment_name"])
        big = io.BytesIO(b"%PDF-1.4\n" + b"0" * (7 * 1024 * 1024))
        self.assertEqual(self.apply(rahul, self.workday(70), document=(big, "big.pdf")).status_code, 413)


class OtherPeoplesData(Base):
    def test_cannot_see_or_touch_someone_elses_records(self):
        priya, rahul = self.login("priya"), self.login("rahul")
        self.apply(priya, self.workday(15), document=(io.BytesIO(b"%PDF-1.4\n"), "note.pdf"))
        rid = self.sql("SELECT r.id FROM leave_requests r JOIN users u ON u.employee_id = r.employee_id "
                       "WHERE u.username = 'priya' ORDER BY r.id DESC")[0][0]
        self.assertEqual(rahul.get(f"/leave/{rid}").status_code, 403)
        self.assertEqual(rahul.get(f"/leave/{rid}/document").status_code, 403)
        self.assertEqual(rahul.post(f"/leave/{rid}/cancel", data={"_csrf": rahul.tok}).status_code, 403)
        self.assertEqual(rahul.post(f"/approvals/{rid}/decide", data={"_csrf": rahul.tok, "decision": "approve",
                                                                      "stage": "1"}).status_code, 403)
        priya_emp = self.sql("SELECT employee_id FROM users WHERE username = 'priya'")[0][0]
        self.assertEqual(rahul.get(f"/employees/{priya_emp}").status_code, 403)
        # salary slip of someone else: indistinguishable from a missing slip
        hr = self.login("hr")
        hr.post(f"/payroll/structure/{priya_emp}", data={"_csrf": hr.tok, "earning_name_0": "Pay", "earning_amount_0": "100"})
        hr.post("/payroll/generate", data={"_csrf": hr.tok, "month": "2026-07"})
        sid = self.sql("SELECT id FROM salary_slips WHERE employee_id = ?", (priya_emp,))[0][0]
        hr.post("/payroll/issue", data={"_csrf": hr.tok, "slip_id": str(sid)})
        self.assertEqual(rahul.get(f"/salary/{sid}").status_code, 404)
        self.assertEqual(rahul.get(f"/payroll/slips/{sid}/print").status_code, 403)
        self.assertEqual(self.login("admin").get(f"/payroll/slips/{sid}").status_code, 403)
        self.assertEqual(priya.get(f"/salary/{sid}").status_code, 200)


class Sessions(Base):
    def test_logout_deactivation_role_change_and_tampering(self):
        c = self.login("kavita")
        c.post("/logout", data={"_csrf": c.tok})
        self.assertEqual(c.get("/dashboard").status_code, 302)

        c = self.login("kavita")
        admin = self.login("admin")
        uid = self.sql("SELECT id FROM users WHERE username = 'kavita'")[0][0]
        admin.post(f"/admin/users/{uid}/toggle", data={"_csrf": admin.tok})          # deactivate
        self.assertIn("/login", c.get("/dashboard").headers["Location"])
        admin.post(f"/admin/users/{uid}/toggle", data={"_csrf": admin.tok})          # reactivate

        c = self.login("kavita")
        role = self.sql("SELECT id FROM roles WHERE code = 'HR'")[0][0]
        self.sql("UPDATE users SET role_id = ?, session_version = session_version + 1 WHERE id = ?", (role, uid))
        self.assertIn("/login", c.get("/dashboard").headers["Location"])          # must log in again with new rights
        self.sql("UPDATE users SET role_id = (SELECT id FROM roles WHERE code = 'EMPLOYEE') WHERE id = ?", (uid,))

        c = self.app.test_client()
        c.set_cookie("mitra_hrms", "eyJ1aWQiOjF9.forged.signature", domain="localhost")
        self.assertEqual(c.get("/admin/").status_code, 302)                        # treated as logged out


class Concurrency(Base):
    def test_double_click_approve_counts_once(self):
        rahul = self.login("rahul")
        day = self.workday(20)
        self.apply(rahul, day)
        rid = self.sql("SELECT id FROM leave_requests WHERE from_date = ? ORDER BY id DESC", (str(day),))[0][0]
        boss = [self.login("jtceo") for _ in range(6)]
        results = []

        def click(c):
            results.append(c.post(f"/approvals/{rid}/decide", data={"_csrf": c.tok, "decision": "approve",
                                                                    "stage": "1"}).status_code)
        threads = [threading.Thread(target=click, args=(c,)) for c in boss]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertTrue(all(s in (302, 403) for s in results), results)
        self.assertEqual(len(self.sql("SELECT 1 FROM audit_logs WHERE action = 'LEAVE_APPROVED' AND entity_id = ?",
                                      (str(rid),))), 1)
        self.assert_balances_consistent()

    def test_parallel_applications_cannot_overdraw(self):
        users = [self.login("amit") for _ in range(8)]
        days = [self.workday(80 + 7 * i) for i in range(8)]
        lt = self.type_id("EL")

        def go(c, d):
            c.post("/leave/apply", data={"_csrf": c.tok, "leave_type_id": str(lt), "from_date": str(d),
                                         "to_date": str(d + timedelta(days=4)), "reason": "x"},
                   content_type="multipart/form-data")
        threads = [threading.Thread(target=go, args=(c, d)) for c, d in zip(users, days)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assert_balances_consistent()
        with self.app.app_context():
            from hrms.services.leave import balances_for
            emp = self.sql("SELECT employee_id FROM users WHERE username = 'amit'")[0][0]
            el = next(b for b in balances_for(emp) if b["leave_type"]["code"] == "EL")
            self.assertGreaterEqual(el["available"], 0)



class RandomWalk(Base):
    def test_hundreds_of_random_actions_keep_everything_consistent(self):
        rng = random.Random(2026)
        staff = ["rahul", "priya", "kavita", "amit", "hr", "js"]
        clients = {u: self.login(u) for u in staff + ["jtceo", "ceo"]}
        lt = self.type_id("EL")
        statuses = {}
        for _ in range(250):
            action = rng.choice(["apply", "apply", "decide", "decide", "cancel"])
            if action == "apply":
                u = rng.choice(staff)
                d = self.workday(rng.randint(-20, 150))
                r = clients[u].post("/leave/apply", data={"_csrf": clients[u].tok, "leave_type_id": str(lt),
                                                          "from_date": str(d),
                                                          "to_date": str(d + timedelta(days=rng.choice([0, 0, 1, 2, 4]))),
                                                          "reason": "random"}, content_type="multipart/form-data")
                statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
            elif action == "decide":
                pend = self.sql("SELECT id, current_stage FROM leave_requests WHERE status = 'PENDING'")
                if pend:
                    p = rng.choice(pend)
                    for boss in ("jtceo", "ceo", "js", "hr"):
                        c = clients[boss]
                        r = c.post(f"/approvals/{p['id']}/decide",
                                   data={"_csrf": c.tok, "decision": rng.choice(["approve", "reject"]),
                                         "stage": str(p["current_stage"]), "remarks": "ok"})
                        statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
                        if r.status_code == 302:
                            break
            else:
                mine = self.sql("""SELECT r.id, u.username FROM leave_requests r JOIN users u ON u.employee_id = r.employee_id
                                    WHERE r.status IN ('PENDING', 'APPROVED')""")
                if mine:
                    m = rng.choice(mine)
                    if m["username"] in clients:
                        r = clients[m["username"]].post(f"/leave/{m['id']}/cancel",
                                                        data={"_csrf": clients[m["username"]].tok, "reason": "changed"})
                        statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
        self.assertNotIn(500, statuses)
        self.assertGreater(len(self.sql("SELECT 1 FROM leave_requests WHERE status = 'APPROVED'")), 5)
        self.assert_balances_consistent()
        with self.app.app_context():
            from hrms.core.audit import verify_chain
            ok, n, bad = verify_chain()
            self.assertTrue(ok, f"audit chain broken at row {bad}")
            self.assertGreater(n, 100)


class Performance(Base):
    """MITRA has 80-130 staff: the heaviest pages must stay quick with a full year of leave history."""

    def test_heavy_pages_stay_fast_at_mitra_scale(self):
        import time
        conn = sqlite3.connect(self.app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        pw = conn.execute("SELECT password_hash FROM users WHERE username = 'rahul'").fetchone()[0]
        role = conn.execute("SELECT id FROM roles WHERE code = 'EMPLOYEE'").fetchone()[0]
        lt = self.type_id("EL")
        jt = conn.execute("SELECT employee_id FROM users WHERE username = 'jtceo'").fetchone()[0]
        rng = random.Random(7)
        conn.execute("BEGIN")
        for i in range(120):
            eid = conn.execute("""INSERT INTO employees (emp_code, full_name, reporting_officer_id, joining_date, status,
                                  created_at, updated_at) VALUES (?, ?, ?, '2024-06-01', 'ACTIVE', 'x', 'x')""",
                               (f"P{i:03}", f"Perf Person {i}", jt)).lastrowid
            conn.execute("INSERT INTO users (employee_id, username, password_hash, role_id, must_change_password, "
                         "created_at) VALUES (?, ?, ?, ?, 0, 'x')", (eid, f"perf{i}", pw, role))
            for _ in range(8):
                d = date(2026, 1, 5) + timedelta(days=rng.randint(0, 330))
                st = rng.choice(["APPROVED"] * 5 + ["REJECTED", "PENDING"])
                rid = conn.execute("""INSERT INTO leave_requests (employee_id, leave_type_id, from_date, to_date, days,
                                      leave_year, reason, status, workflow_id, current_stage, submitted_at)
                                      VALUES (?, ?, ?, ?, 1, 20260601, 'x', ?, 1, ?, 'x')""",
                                   (eid, lt, d.isoformat(), d.isoformat(), st, 1 if st == "PENDING" else None)).lastrowid
                conn.execute("""INSERT INTO request_approvals (request_id, seq, stage_name, approver_type, status)
                                VALUES (?, 1, 'Reporting Officer', 'REPORTING_OFFICER', ?)""",
                             (rid, "PENDING" if st == "PENDING" else "APPROVED"))
        conn.execute("COMMIT")
        conn.close()
        with self.app.app_context():
            from hrms.services.leave import rebuild_balances
            rebuild_balances()
        slow = []
        for user, url, limit in (("jtceo", "/supervise", 2.0), ("hr", "/supervise", 2.0), ("ceo", "/leave/records", 2.0),
                                 ("jtceo", "/approvals/", 2.0), ("rahul", "/dashboard", 0.5), ("hr", "/payroll/", 1.0),
                                 ("admin", "/admin/", 1.0), ("admin", "/audit/", 1.0)):
            c = self.login(user)
            c.get(url)
            t0 = time.perf_counter()
            self.assertEqual(c.get(url).status_code, 200)
            took = time.perf_counter() - t0
            if took > limit:
                slow.append(f"{url} for {user}: {took:.2f}s (limit {limit}s)")
        self.assertEqual(slow, [])
        clients = [self.login(f"perf{i}") for i in range(20)]
        t0 = time.perf_counter()
        threads = [threading.Thread(target=c.get, args=("/dashboard",)) for c in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertLess(time.perf_counter() - t0, 5.0, "20 people opening their dashboards at the same moment")


if __name__ == "__main__":
    unittest.main()
