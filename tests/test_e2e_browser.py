"""End-to-end journeys in a real browser (Chrome, driven by Playwright), from the users' point of view.

Each journey clicks through the pages the way a person would, including the JavaScript parts
(live day count, password checklist, confirmation prompts, print button). Every page is also
checked for JavaScript errors and content-security-policy violations, and the phone layout is
checked for sideways scrolling.

Run:  python -m unittest tests.test_e2e_browser -v      (skipped if Playwright / Chrome are missing)
"""
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

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


def next_monday(after_days=7):
    d = date.today() + timedelta(days=after_days)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class BrowserJourneys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "e2e.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "INSTANCE_DIR": str(cls.tmp)})
        cli = cls.app.test_cli_runner()
        assert cli.invoke(args=["seed-demo"]).exit_code == 0
        assert cli.invoke(args=["load-holidays", "2026"]).exit_code == 0
        conn = sqlite3.connect(cls.app.config["DATABASE"])
        conn.execute("UPDATE leave_types SET annual_entitlement = 18, accrual_per_month = 1.5, allow_backdated = 1 "
                     "WHERE code = 'EL'")
        conn.execute("UPDATE leave_types SET is_active = 0 WHERE code IN ('CL', 'SL')")
        conn.commit()
        conn.close()
        cls.server = make_server("127.0.0.1", 0, cls.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # pragma: no cover
            cls.pw.stop()
            cls.server.shutdown()
            raise unittest.SkipTest(f"Chrome not available: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def new_page(self, width=1366, height=900):
        ctx = self.browser.new_context(viewport={"width": width, "height": height})
        page = ctx.new_page()
        page.problems = []
        page.on("pageerror", lambda e: page.problems.append(f"JS error: {e}"))
        page.on("console", lambda m: page.problems.append(f"console: {m.text}")
                if m.type == "error" or "Content Security Policy" in m.text else None)
        page.on("dialog", lambda d: d.accept())  # confirm "Approve this leave request?" etc.
        page.add_init_script("window.__printed = 0; window.print = function () { window.__printed++; };")
        self.addCleanup(ctx.close)
        return page

    def login(self, page, username, password=DEMO_PASSWORD):
        page.goto(self.base + "/login")
        page.fill("#username", username)
        page.fill("#password", password)
        page.click("button[type=submit]")
        page.wait_for_load_state()

    def assert_clean(self, page):
        self.assertEqual(page.problems, [], page.url)

    def flash(self, page):
        return " ".join(page.locator(".flash").all_inner_texts())

    def sql(self, q, params=()):
        conn = sqlite3.connect(self.app.config["DATABASE"])
        try:
            return conn.execute(q, params).fetchall()
        finally:
            conn.close()

    # ---------------------------------------------------------------- journeys
    def test_1_new_user_first_login_sets_password(self):
        admin = self.new_page()
        self.login(admin, "admin")
        admin.goto(self.base + "/admin/users/new")
        admin.fill("#ec", "M500")
        admin.fill("#fn", "Neha Kale")
        admin.fill("#un", "neha.kale")
        admin.select_option("#rl", label="Employee")
        admin.select_option("#ro", label="Vikram Deshmukh (Joint CEO)")
        admin.fill("#jd", "2025-06-02")
        admin.click("button:has-text('Create user')")
        temp = admin.locator(".secret").inner_text().strip()
        self.assertTrue(len(temp) >= 10)
        self.assert_clean(admin)

        neha = self.new_page()
        self.login(neha, "neha.kale", temp)
        self.assertIn("/account/password", neha.url)
        self.assertIn("Set your password", neha.inner_text("h1"))
        neha.fill("#cp", temp)
        neha.fill("#np", "neha")
        self.assertEqual(neha.get_attribute("[data-rule=length]", "class"), "bad")
        neha.fill("#np", "Neha.Kale@2026")  # contains the username
        self.assertEqual(neha.get_attribute("[data-rule=username]", "class"), "bad")
        neha.fill("#np", "Kale@Leave2026")
        for rule in ("length", "upper", "lower", "digit", "special", "username"):
            self.assertEqual(neha.get_attribute(f"[data-rule={rule}]", "class"), "ok", rule)
        neha.click(".pw-toggle[data-reveal=np]")
        self.assertEqual(neha.get_attribute("#np", "type"), "text")
        neha.fill("#cf", "Kale@Leave2027")
        self.assertIn("do not match", neha.inner_text("#pw-match"))
        neha.fill("#cf", "Kale@Leave2026")
        neha.click("button:has-text('Save password')")
        self.assertIn("Password changed", self.flash(neha))
        self.assertIn("/dashboard", neha.url)
        self.assertIn("Service details", neha.content())
        self.assert_clean(neha)

    def test_2_apply_approve_and_see_result(self):
        start = next_monday(14)
        while self.sql("SELECT 1 FROM holidays WHERE date BETWEEN ? AND ?",       # a week with no public holiday
                       (start.isoformat(), (start + timedelta(days=2)).isoformat())):
            start += timedelta(days=7)
        end = start + timedelta(days=2)
        rahul = self.new_page()
        self.login(rahul, "rahul")
        rahul.click("text=Apply for leave >> nth=0")
        rahul.select_option("#lt", label="Earned / Privilege Leave")
        self.assertIn("Available", rahul.inner_text("#type-hint"))
        rahul.fill("#fd", start.isoformat())
        rahul.fill("#td", end.isoformat())
        rahul.dispatch_event("#td", "change")
        rahul.wait_for_function("document.getElementById('day-count').textContent.includes('day')")
        self.assertTrue(rahul.inner_text("#day-count").startswith("3 days."))
        self.assertIn("after this request", rahul.inner_text("#day-count"))
        rahul.fill("#rs", "Family function in Pune")
        rahul.click("button:has-text('Submit application')")
        self.assertIn("submitted for approval", self.flash(rahul))
        self.assertIn("Pending", rahul.content())
        self.assert_clean(rahul)

        boss = self.new_page()
        self.login(boss, "jtceo")
        self.assertIn("/supervise", boss.url)
        row = boss.locator("tr", has_text="Rahul Joshi").filter(has_text="Family function")
        row.locator("input[name=remarks]").fill("Approved. Please hand over the budget note.")
        row.locator("button[value=approve]").click()
        boss.wait_for_load_state()
        self.assertIn("Leave approved", self.flash(boss))
        self.assert_clean(boss)

        rahul.goto(self.base + "/notifications/")
        self.assertIn("Leave approved", rahul.content())
        rahul.goto(self.base + "/dashboard")
        el = rahul.locator("tr", has_text="Earned / Privilege Leave").first
        self.assertIn("3", el.locator("td").nth(2).inner_text())  # used
        rahul.goto(self.base + "/leave/my")
        self.assertIn("hand over the budget note", rahul.content())
        self.assert_clean(rahul)

    def test_3_reject_needs_remarks_and_employee_sees_reason(self):
        holidays = {r[0] for r in self.sql("SELECT date FROM holidays")}
        day = next_monday(28) + timedelta(days=1)
        while day.isoformat() in holidays or day.weekday() >= 5:
            day += timedelta(days=1)
        priya = self.new_page()
        self.login(priya, "priya")
        priya.goto(self.base + "/leave/apply")
        priya.select_option("#lt", label="Earned / Privilege Leave")
        priya.fill("#fd", day.isoformat())
        priya.fill("#td", day.isoformat())
        priya.fill("#rs", "Personal work")
        priya.click("button:has-text('Submit application')")
        boss = self.new_page()
        self.login(boss, "jtceo")
        row = boss.locator("tr", has_text="Priya Nair").filter(has_text="Personal work")
        row.locator("button[value=reject]").click()
        boss.wait_for_load_state()
        self.assertIn("Remarks is required", self.flash(boss))
        row = boss.locator("tr", has_text="Priya Nair").filter(has_text="Personal work")
        row.locator("input[name=remarks]").fill("Project review that week")
        row.locator("button[value=reject]").click()
        boss.wait_for_load_state()
        priya.goto(self.base + "/leave/my")
        self.assertIn("Project review that week", priya.content())
        self.assert_clean(boss)
        self.assert_clean(priya)

    def test_4_compoff_claim_and_approval(self):
        t = date.today()
        saturday = t - timedelta(days=(t.weekday() - 5) % 7)
        kavita = self.new_page()
        self.login(kavita, "kavita")
        kavita.goto(self.base + "/compoff/")
        kavita.fill("#wd", saturday.isoformat())
        option = kavita.locator("#ap option", has_text="Meera Kulkarni")
        kavita.select_option("#ap", value=option.get_attribute("value"))
        kavita.fill("#wk", "District review meeting preparation")
        kavita.click("button:has-text('Submit claim')")
        self.assertIn("sent to the officer", self.flash(kavita))
        js = self.new_page()
        self.login(js, "js")
        js.goto(self.base + "/compoff/approvals")
        js.locator("tr", has_text="Kavita Pawar").locator("button[value=approve]").click()
        js.wait_for_load_state()
        self.assertIn("credited", self.flash(js))
        kavita.goto(self.base + "/compoff/")
        self.assertIn("Approved", kavita.content())
        self.assert_clean(kavita)
        self.assert_clean(js)

    def test_5_hr_issues_salary_slip_and_employee_prints_it(self):
        hr = self.new_page()
        self.login(hr, "hr")
        hr.goto(self.base + "/payroll/?month=2026-09")
        hr.locator("tr", has_text="Amit Shinde").locator("a:has-text('Set up')").click()
        hr.fill("[name=earning_name_0]", "Consolidated remuneration")
        hr.fill("[name=earning_amount_0]", "52,000")
        hr.fill("[name=deduction_name_0]", "TDS")
        hr.fill("[name=deduction_amount_0]", "5200")
        hr.fill("#pan", "abcde1234f")
        hr.click("button:has-text('Save pay structure')")
        self.assertIn("Pay structure saved", self.flash(hr))
        hr.locator("tr", has_text="Amit Shinde").locator("button:has-text('Prepare')").click()
        hr.wait_for_load_state()
        self.assertIn("Draft", hr.content())
        hr.fill("#lop", "2")
        hr.click("button[value=recalculate]")
        self.assertIn("48533.00", hr.input_value("[name=earning_amount_0]"))  # 52,000 x 28/30, to the rupee
        hr.click("button:has-text('Issue slip')")
        hr.wait_for_load_state()
        self.assertIn("Issued", hr.content())
        self.assert_clean(hr)

        amit = self.new_page()
        self.login(amit, "amit")
        amit.click("nav >> text=Salary slips")
        amit.click("text=View")
        text = amit.inner_text(".slip")
        self.assertIn("September 2026", text)
        self.assertIn("ABCDE1234F", text)                 # PAN stored in capitals
        self.assertIn("43,333.00", text)                  # 52,000 x 28/30 - 5,200, rounded to the rupee
        self.assertIn("Rupees Forty Three Thousand Three Hundred Thirty Three Only", text)
        amit.click("button[data-print]")
        self.assertEqual(amit.evaluate("window.__printed"), 1)
        self.assert_clean(amit)

    def test_6_phone_layout_has_no_sideways_scroll(self):
        pages = {"rahul": ("/dashboard", "/leave/apply", "/leave/my", "/compoff/", "/holidays", "/profile", "/salary/",
                           "/timesheet/"),
                 "hr": ("/supervise", "/approvals/", "/compoff/approvals", "/leave/records", "/payroll/", "/audit/",
                        "/timesheet/team", "/timesheet/report", "/timesheet/projects"),
                 "admin": ("/admin/", "/admin/users", "/admin/leave-types", "/admin/roles", "/admin/workflows",
                           "/admin/org", "/admin/settings")}
        for user, paths in pages.items():
            page = self.new_page(width=390, height=844)
            self.login(page, user)
            for path in paths:
                page.goto(self.base + path)
                overflow = page.evaluate("document.scrollingElement.scrollWidth - window.innerWidth")
                self.assertLessEqual(overflow, 1, f"{path} scrolls sideways by {overflow}px on a phone")
            self.assert_clean(page)

    def test_7_every_form_field_has_a_label(self):
        page = self.new_page()
        self.login(page, "hr")
        missing = {}
        for path in ("/dashboard", "/leave/apply", "/compoff/", "/supervise", "/profile", "/payroll/",
                     "/account/password", "/holidays", "/timesheet/", "/timesheet/projects", "/timesheet/report"):
            page.goto(self.base + path)
            bad = page.evaluate("""() => [...document.querySelectorAll('input, select, textarea')]
                .filter(el => el.type !== 'hidden' && !el.disabled)
                .filter(el => !(el.labels && el.labels.length) && !el.getAttribute('aria-label') && el.type !== 'submit')
                .map(el => el.name || el.id)""")
            if bad:
                missing[path] = bad
        self.assertEqual(missing, {}, "form fields without a label (screen readers cannot name them)")
        self.assert_clean(page)

    def test_9_header_search_pending_actions_sorting_and_phone_menu(self):
        boss = self.new_page()
        self.login(boss, "jtceo")
        boss.fill(".topsearch input", "Kavita")
        boss.press(".topsearch input", "Enter")
        boss.wait_for_load_state()
        self.assertIn("Kavita Pawar", boss.inner_text("main"))
        boss.click("main >> text=Kavita Pawar")
        self.assertIn("/employees/", boss.url)                    # her reporting officer can open her record
        boss.click(".userbar >> text=Pending actions")
        self.assertIn("Pending actions", boss.inner_text("h1"))
        hr = self.new_page()
        self.login(hr, "hr")
        hr.goto(self.base + "/timesheet/team")
        first = hr.locator("th.sortable").first
        first.click()
        self.assertEqual(first.get_attribute("aria-sort"), "ascending")
        names = hr.locator("tbody tr td:first-child strong").all_inner_texts()
        self.assertEqual(names, sorted(names, key=str.lower))
        first.click()
        self.assertEqual(first.get_attribute("aria-sort"), "descending")
        phone = self.new_page(width=390, height=844)
        self.login(phone, "rahul")
        self.assertFalse(phone.is_visible("#sidenav"))           # menu folded away on phones
        phone.click(".menu-toggle")
        self.assertTrue(phone.is_visible("#sidenav"))
        phone.click("#sidenav >> text=Timesheet")
        self.assertIn("/timesheet/", phone.url)
        for p in (boss, hr, phone):
            self.assert_clean(p)

    def test_8_book_days_on_the_calendar_and_submit_the_week(self):
        t = date.today()
        monday = t - timedelta(days=t.weekday() + 7)
        last_week = monday.isoformat()
        js = self.new_page()
        self.login(js, "js")
        js.goto(f"{self.base}/timesheet/?month={monday.strftime('%Y-%m')}")
        js.click(f"#d{monday.isoformat()} a.cell")                          # the editor opens beside the calendar
        self.assertTrue(js.is_visible("#editor"))
        js.select_option("#editor [name=e0_0_project]", label="MURWEP")
        js.select_option("#editor [name=e0_0_hours]", "6")
        js.fill("#editor [name=e0_0_desc]", "Reviewed tender documents")
        js.select_option("#editor [name=e0_1_project]", label="Administration")
        js.select_option("#editor [name=e0_1_hours]", "2")
        js.click("#editor button[value=calendar]")
        self.assertIn("add a short note", js.inner_text(".flash.error"))   # note missing on the 2nd row
        self.assertEqual(js.input_value("#editor [name=e0_1_hours]"), "2")   # what was typed is kept
        js.fill("#editor [name=e0_1_desc]", "Leave files")
        js.click("#editor button[value=next-day]")                          # save and move to Tuesday
        self.assertIn("8 project hours saved", self.flash(js))
        js.select_option("#editor [name=e0_0_project]", label="Sector research")
        js.select_option("#editor [name=e0_0_hours]", "8")
        js.fill("#editor [name=e0_0_desc]", "Drafted sector note")
        js.click("#editor button[value=calendar]")
        self.assertIn(f'class="st-complete" id="d{monday.isoformat()}"', js.content())
        js.locator("form", has=js.locator(f"input[name=week][value='{last_week}']")).locator("button").click()
        self.assertIn("submitted (16 hours)", self.flash(js))
        boss = self.new_page()
        self.login(boss, "ceo")
        boss.goto(f"{self.base}/timesheet/team?week={last_week}")
        row = boss.locator("tr", has_text="Meera Kulkarni")
        self.assertIn("Submitted", row.inner_text())
        row.locator("a", has_text="Week").click()
        self.assertIn("Reviewed tender documents", boss.content())
        self.assert_clean(js)
        self.assert_clean(boss)


if __name__ == "__main__":
    unittest.main()
