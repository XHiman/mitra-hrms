"""Weekly timesheets: filling, expected hours (weekly offs, holidays, leave, joining date), validation,
submit / reopen, team view scope, monthly report and project list.

Run:  python -m unittest tests.test_timesheet -v
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


class TimesheetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.app = create_app({"DATABASE": str(cls.tmp / "t.db"), "UPLOAD_DIR": str(cls.tmp / "u"),
                              "BACKUP_DIR": str(cls.tmp / "b"), "TESTING": True})
        assert cls.app.test_cli_runner().invoke(args=["seed-demo"]).exit_code == 0
        t = date.today()
        cls.week = t - timedelta(days=t.weekday()) - timedelta(days=14)   # a fully past week
        cls.days = [cls.week + timedelta(days=i) for i in range(7)]
        cls.sql("INSERT INTO holidays (date, name) VALUES (?, 'Test Holiday')", (cls.days[2].isoformat(),))  # Wednesday
        rahul = cls.sql("SELECT employee_id FROM users WHERE username = 'rahul'")[0][0]
        lt = cls.sql("SELECT id FROM leave_types WHERE code = 'CL'")[0][0]
        cls.sql("""INSERT INTO leave_requests (employee_id, leave_type_id, from_date, to_date, days, leave_year, reason,
                   status, submitted_at) VALUES (?, ?, ?, ?, 1, 2026, 'x', 'APPROVED', 'x')""",
                (rahul, lt, cls.days[3].isoformat(), cls.days[3].isoformat()))             # Thursday on leave
        cls.proj = {r["name"]: r["id"] for r in cls.sql("SELECT id, name FROM timesheet_projects")}

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

    def post_day(self, c, d, rows):
        """Book one day from the calendar editor: rows of (project, hours, note)."""
        data = {"_csrf": c.tok, "next": "calendar"}
        for n, (project, hours, desc) in enumerate(rows):
            data.update({f"e0_{n}_project": str(self.proj.get(project, project)), f"e0_{n}_hours": str(hours),
                         f"e0_{n}_desc": desc})
        return c.post(f"/timesheet/day/{d.isoformat()}", data=data)

    def post_week(self, c, entries, action="save", week=None):
        """Book several days (entries of (weekday index, project, hours, note)), then optionally submit."""
        start = week or self.week
        by_day = {}
        for day, project, hours, desc in entries:
            by_day.setdefault(day, []).append((project, hours, desc))
        r = None
        for day, rows in sorted(by_day.items()):
            r = self.post_day(c, start + timedelta(days=day), rows)
        if action == "submit":
            r = c.post("/timesheet/week/submit", data={"_csrf": c.tok, "week": start.isoformat()})
        return r

    def test_1_fill_save_submit_reopen(self):
        rahul = self.login("rahul")
        page = rahul.get(f"/timesheet/week?week={self.week}").get_data(as_text=True)
        self.assertIn("Holiday: Test Holiday", page)
        self.assertIn("On paid leave (Casual Leave)", page)
        self.assertIn("Weekly off", page)
        self.assertNotIn('name="e0_0_project"', page)              # the week view is read only

        invalid = [
            ([("MURWEP", 2, "")], "add a short note"),
            ([("MURWEP", "abc", "x")], "whole number from 1 to 8"),
            ([("MURWEP", 2.5, "x")], "whole number from 1 to 8"),      # no fractions of an hour
            ([("MURWEP", 9, "x")], "whole number from 1 to 8"),        # at most 8 on one row
            ([(999, 2, "x")], "choose a project"),
            ([("MURWEP", 8, "Review"), ("MRDP", 8, "Field visit"), ("MRDP", 1, "Call")], "more than 16"),
        ]
        for rows, msg in invalid:
            r = self.post_day(rahul, self.days[0], rows)
            self.assertEqual(r.status_code, 200, msg)
            self.assertIn(msg, r.get_data(as_text=True))
            self.assertIn('id="editor"', r.get_data(as_text=True))  # the error shows in the calendar editor
        self.assertFalse(self.sql("SELECT 1 FROM timesheet_entries"))  # nothing saved when anything is wrong

        week = [(0, "MURWEP", 5, "Bid evaluation"), (0, "Sector research", 3, "Data analysis"),
                (1, "MRDP", 6, "District review"), (4, "Administration", 8, "Budget note")]
        r = self.post_week(rahul, week)
        self.assertEqual(r.status_code, 302)
        page = rahul.get(f"/timesheet/week?week={self.week}").get_data(as_text=True)
        # Mon 8 + Tue 6 + Fri 8 on projects, Thursday's paid leave counts 8, Wednesday's holiday expects 8
        self.assertIn("30 hours booked (22 on projects, 8 paid leave)", page)
        self.assertIn("Bid evaluation", page)

        self.post_week(rahul, [], action="submit")
        s = self.sql("SELECT * FROM timesheets")[0]
        self.assertEqual((s["status"], s["total_hours"]), ("SUBMITTED", 22))
        r = self.post_day(rahul, self.days[1], [("MRDP", 6, "District review"), ("MRDP", 2, "More")])
        self.assertIn("already been submitted", r.get_data(as_text=True))
        rahul.post("/timesheet/week/reopen", data={"_csrf": rahul.tok, "week": self.week.isoformat()})
        self.assertEqual(self.sql("SELECT status FROM timesheets")[0][0], "DRAFT")
        self.post_day(rahul, self.days[1], [("MRDP", 6, "District review"), ("MRDP", 2, "Follow-up note")])
        self.post_week(rahul, [], action="submit")
        self.assertEqual(self.sql("SELECT total_hours FROM timesheets")[0][0], 24)
        actions = [r[0] for r in self.sql("SELECT action FROM audit_logs WHERE entity = 'timesheet' ORDER BY id")]
        self.assertEqual(actions, ["TIMESHEET_SAVED"] * 3 + ["TIMESHEET_SUBMITTED", "TIMESHEET_REOPENED",
                                                            "TIMESHEET_SAVED", "TIMESHEET_SUBMITTED"])

    def test_2_no_hours_before_joining_after_contract_or_in_future(self):
        priya = self.login("priya")
        self.sql("UPDATE employees SET joining_date = ? WHERE emp_code = 'M102'", (self.days[1].isoformat(),))
        r = self.post_day(priya, self.days[0], [("MURWEP", 4, "Before joining")])
        self.assertIn("cannot be entered", r.get_data(as_text=True))
        self.assertIn("Before date of joining", priya.get(f"/timesheet/week?week={self.week}").get_data(as_text=True))
        self.sql("UPDATE employees SET joining_date = '2024-04-01', contract_end_date = ? WHERE emp_code = 'M102'",
                 (self.days[1].isoformat(),))
        r = self.post_day(priya, self.days[4], [("MURWEP", 4, "After the contract")])
        self.assertIn("cannot be entered", r.get_data(as_text=True))
        page = priya.get(f"/timesheet/?month={self.days[4].strftime('%Y-%m')}").get_data(as_text=True)
        self.assertIn(f'class="st-after-contract" id="d{self.days[4].isoformat()}"', page)
        self.assertNotIn(f'href="/timesheet/?day={self.days[4].isoformat()}', page)   # not clickable
        self.sql("UPDATE employees SET contract_end_date = NULL WHERE emp_code = 'M102'")
        t = date.today()
        r = self.post_day(priya, t + timedelta(days=1), [("MURWEP", 4, "Tomorrow's work")])
        self.assertEqual(r.status_code, 302)
        self.assertFalse(self.sql("SELECT 1 FROM timesheet_entries WHERE work_date = ?",
                                  ((t + timedelta(days=1)).isoformat(),)))
        self.assertEqual(priya.get(f"/timesheet/week?week={t + timedelta(days=21)}").status_code, 302)  # future week

    def test_3_team_view_scope_and_report(self):
        kavita = self.login("kavita")  # reports to jtceo
        self.post_week(kavita, [(0, "MURWEP", 8, "Procurement plan"), (1, "MURWEP", 4, "Meeting")], action="submit")
        amit = self.login("amit")      # reports to js
        self.post_week(amit, [(0, "Administration", 8, "Files")])
        jt = self.login("jtceo")
        page = jt.get(f"/timesheet/team?week={self.week}").get_data(as_text=True)
        self.assertIn("Kavita Pawar", page)
        self.assertNotIn("Amit Shinde", page)
        kav = self.sql("SELECT employee_id FROM users WHERE username = 'kavita'")[0][0]
        am = self.sql("SELECT employee_id FROM users WHERE username = 'amit'")[0][0]
        self.assertIn("Procurement plan", jt.get(f"/timesheet/view/{kav}?week={self.week}").get_data(as_text=True))
        self.assertEqual(jt.get(f"/timesheet/view/{am}?week={self.week}").status_code, 403)
        self.assertEqual(self.login("rahul").get("/timesheet/team").status_code, 403)
        hr = self.login("hr")
        self.assertIn("Amit Shinde", hr.get(f"/timesheet/team?week={self.week}").get_data(as_text=True))

        month = self.days[0].strftime("%Y-%m")
        page = hr.get(f"/timesheet/report?month={month}").get_data(as_text=True)
        self.assertIn("Kavita Pawar", page)
        csv = hr.get(f"/timesheet/report?month={month}&format=csv")
        text = csv.get_data(as_text=True)
        self.assertTrue(text.startswith("﻿Employee ID,Name,Date,Project,Hours,Task,Week status"))
        self.assertIn("Procurement plan", text)
        jt_csv = jt.get(f"/timesheet/report?month={month}&format=csv").get_data(as_text=True)
        self.assertIn("Kavita Pawar", jt_csv)
        self.assertNotIn("Amit Shinde", jt_csv)  # supervisors' reports cover only their own team

    def test_4_projects_with_details(self):
        hr = self.login("hr")
        rahul = self.login("rahul")
        for url in ("/timesheet/projects", "/timesheet/projects/new"):
            self.assertEqual(rahul.get(url).status_code, 403)
        dept = self.sql("SELECT id FROM departments LIMIT 1")[0][0]
        head = self.sql("SELECT employee_id FROM users WHERE username = 'jtceo'")[0][0]
        details = {"_csrf": hr.tok, "name": "MahasTRIDE", "code": "ms-01", "description": "World Bank programme",
                   "department_id": str(dept), "head_employee_id": str(head), "start_date": "2026-04-01",
                   "end_date": "2029-03-31", "funding": "World Bank", "status": "ACTIVE"}
        r = hr.post("/timesheet/projects/new", data=details)
        self.assertEqual(r.status_code, 302)
        p = self.sql("SELECT * FROM timesheet_projects WHERE name = 'MahasTRIDE'")[0]
        self.assertEqual((p["code"], p["department_id"], p["head_employee_id"], p["funding"], p["status"], p["is_active"]),
                         ("MS-01", dept, head, "World Bank", "ACTIVE", 1))
        page = hr.get(f"/timesheet/projects/{p['id']}").get_data(as_text=True)
        self.assertIn('value="World Bank"', page)
        self.assertIn("Vikram Deshmukh", hr.get("/timesheet/projects").get_data(as_text=True))   # head shown in list
        checks = [({"name": "mahastride", "code": ""}, "already exists"),
                  ({"name": "Another", "code": "MS-01"}, "already used"),
                  ({"name": "Another", "start_date": "2026-05-01", "end_date": "2026-04-01"}, "end date cannot be before"),
                  ({"name": "Another", "head_employee_id": "99999"}, "valid project head"),
                  ({"name": "Another", "status": "ARCHIVED"}, "valid status")]
        for change, msg in checks:
            r = hr.post("/timesheet/projects/new", data={**details, **change})
            self.assertIn(msg, r.get_data(as_text=True), msg)
        editor = f"/timesheet/?day={self.days[0].isoformat()}"
        self.assertIn("MahasTRIDE (MS-01)", self.login("ceo").get(editor).get_data(as_text=True))
        hr.post(f"/timesheet/projects/{p['id']}", data={**details, "status": "ON_HOLD"})
        self.assertEqual(self.sql("SELECT is_active FROM timesheet_projects WHERE id = ?", (p["id"],))[0][0], 0)
        self.assertNotIn(">MahasTRIDE", self.login("ceo").get(editor).get_data(as_text=True))   # only Active offered
        on_hold = hr.get("/timesheet/projects?status=ON_HOLD").get_data(as_text=True)
        self.assertIn("MahasTRIDE", on_hold)
        self.assertNotIn(">MURWEP<", on_hold)
        actions = [r[0] for r in self.sql("SELECT action FROM audit_logs WHERE entity = 'timesheet_project' ORDER BY id")]
        self.assertEqual(actions[-2:], ["TIMESHEET_PROJECT_ADDED", "TIMESHEET_PROJECT_UPDATED"])
        self.assertEqual(hr.get("/timesheet/projects/99999").status_code, 404)

    def test_6_month_calendar_day_booking_and_summary(self):
        import calendar as cal
        wk = self.week - timedelta(days=7)            # Priya's week, untouched by the other tests
        days = [wk + timedelta(days=i) for i in range(7)]
        priya_emp = self.sql("SELECT employee_id FROM users WHERE username = 'priya'")[0][0]
        self.sql("INSERT INTO holidays (date, name) VALUES (?, 'Calendar Holiday')", (days[2].isoformat(),))
        lt = self.sql("SELECT id FROM leave_types WHERE code = 'CL'")[0][0]
        self.sql("""INSERT INTO leave_requests (employee_id, leave_type_id, from_date, to_date, days, leave_year, reason,
                    status, submitted_at) VALUES (?, ?, ?, ?, 1, 2026, 'x', 'APPROVED', 'x')""",
                 (priya_emp, lt, days[3].isoformat(), days[3].isoformat()))
        priya = self.login("priya")

        def book(d, rows, nxt="calendar"):
            data = {"_csrf": priya.tok, "next": nxt}
            for n, (p, h, desc) in enumerate(rows):
                data.update({f"e0_{n}_project": str(self.proj[p]), f"e0_{n}_hours": str(h), f"e0_{n}_desc": desc})
            return priya.post(f"/timesheet/day/{d.isoformat()}", data=data)

        self.assertEqual(book(days[0], [("MURWEP", 5, "Bid review"), ("MRDP", 3, "Field notes")]).status_code, 302)
        r = book(days[1], [("Sector research", 5, "Draft note")], nxt="next-day")
        self.assertIn(f"/timesheet/?day={days[2].isoformat()}", r.headers["Location"])   # Save and go to next day
        self.assertIn("add a short note", book(days[4], [("MURWEP", 4, "")]).get_data(as_text=True))
        book(days[1], [("Sector research", 6, "Draft note, revised")])               # editing replaces the day
        self.assertEqual(self.sql("SELECT total_hours FROM timesheets t JOIN users u ON u.employee_id = t.employee_id "
                                  "WHERE u.username = 'priya' AND week_start = ?", (wk.isoformat(),))[0][0], 14)

        month = days[0].strftime("%Y-%m")
        page = priya.get(f"/timesheet/?month={month}").get_data(as_text=True)
        self.assertIn("total time booked", page)
        self.assertIn(f'class="st-complete" id="d{days[0].isoformat()}"', page)
        self.assertIn("Short 2 h", page)                                              # Tuesday 6 of 8
        self.assertIn("Calendar Holiday", page)
        self.assertIn(f'class="st-pending" id="d{days[2].isoformat()}"', page)        # holidays expect 8 h
        leave_page = page if days[3].month == days[0].month else             priya.get(f"/timesheet/?month={days[3].strftime('%Y-%m')}").get_data(as_text=True)  # week may span two months
        self.assertIn(f'class="st-leave" id="d{days[3].isoformat()}"', leave_page)
        self.assertIn(f'href="/timesheet/?day={days[0].isoformat()}#editor"', page)   # days open in the editor
        page = priya.get(f"/timesheet/?day={days[0].isoformat()}").get_data(as_text=True)
        self.assertIn('id="editor"', page)
        self.assertIn('value="Bid review"', page)                                     # editing in the calendar
        self.assertIn(f'class="st-complete sel" id="d{days[0].isoformat()}"', page)

        from hrms.services.timesheet import month_view
        with self.app.app_context():
            mv = month_view(priya_emp, days[0].year, days[0].month)
        y, m = days[0].year, days[0].month
        weekdays = [date(y, m, d) for d in range(1, cal.monthrange(y, m)[1] + 1) if date(y, m, d).weekday() < 5]
        s = mv["summary"]
        self.assertEqual(s["ideal"], 8 * len(weekdays))                    # holidays included: Oct 2026 = 176 h
        self.assertEqual(s["booked"], s["project"] + s["leave_hours"])
        self.assertEqual(s["shortfall"], max(0, s["ideal"] - s["booked"]))  # ideal minus booked, whole month
        if days[3].month == m:
            self.assertGreaterEqual(s["leave_hours"], 8)
        if (y, m) == (2026, 10):
            self.assertEqual(s["ideal"], 176)

        r = priya.post("/timesheet/week/submit", data={"_csrf": priya.tok, "week": wk.isoformat(), "month": month})
        self.assertEqual(r.status_code, 302)
        self.assertIn("already been submitted", book(days[0], [("MURWEP", 8, "Change")]).get_data(as_text=True))
        self.assertEqual(priya.get(f"/timesheet/day/{(date.today() + timedelta(days=3)).isoformat()}").status_code, 302)

        jt = self.login("jtceo")                                                       # Priya's reporting officer
        view = jt.get(f"/timesheet/view/{priya_emp}?month={month}").get_data(as_text=True)
        self.assertIn("Read only", view)
        self.assertNotIn("/timesheet/?day=", view)
        self.assertNotIn('id="editor"', view)
        self.assertEqual(self.login("js").get(f"/timesheet/view/{priya_emp}?month={month}").status_code, 403)

    def test_7_half_day_unpaid_leave_and_month_wise_summary(self):
        wk = self.week - timedelta(days=14)
        days = [wk + timedelta(days=i) for i in range(7)]
        priya_emp = self.sql("SELECT employee_id FROM users WHERE username = 'priya'")[0][0]
        cl = self.sql("SELECT id FROM leave_types WHERE code = 'CL'")[0][0]
        self.sql("""INSERT INTO leave_types (code, name, balance_tracked, is_paid, is_active, created_at, updated_at)
                    VALUES ('LWP', 'Leave Without Pay', 0, 0, 1, 'x', 'x')""")
        lwp = self.sql("SELECT id FROM leave_types WHERE code = 'LWP'")[0][0]
        for lt, d, half in ((cl, days[0], "FIRST"), (lwp, days[1], None)):
            self.sql("""INSERT INTO leave_requests (employee_id, leave_type_id, from_date, to_date, days, half_day, leave_year,
                        reason, status, submitted_at) VALUES (?, ?, ?, ?, ?, ?, 2026, 'x', 'APPROVED', 'x')""",
                     (priya_emp, lt, d.isoformat(), d.isoformat(), 0.5 if half else 1, half))
        priya = self.login("priya")
        self.post_day(priya, days[0], [("MURWEP", 4, "Afternoon after half-day leave")])

        from hrms.services.timesheet import day_info
        with self.app.app_context():
            info = {d["iso"]: d for d in day_info(priya_emp, wk)}
        mon, tue = info[days[0].isoformat()], info[days[1].isoformat()]
        self.assertEqual((mon["leave_hours"], mon["expected"], mon["leave"]["half"]), (4, 4, "FIRST"))  # 4 + 4 = 8
        self.assertEqual((tue["leave_hours"], tue["expected"], tue["leave"]["paid"]), (0, 8, False))   # unpaid books 0

        page = priya.get(f"/timesheet/?month={days[0].strftime('%Y-%m')}").get_data(as_text=True)
        self.assertIn(f'class="st-complete" id="d{days[0].isoformat()}"', page)
        self.assertIn(f'class="st-unpaid" id="d{days[1].isoformat()}"', page)
        page = priya.get(f"/timesheet/?day={days[0].isoformat()}").get_data(as_text=True)
        self.assertIn("first half", page)
        self.assertIn("<strong>8 h booked</strong> (4 project + 4 leave)", page)

        year = days[0].year
        page = priya.get(f"/timesheet/summary?year={year}").get_data(as_text=True)
        self.assertIn(f"Month-wise summary {year}", page)
        self.assertIn(days[0].strftime("%B"), page)
        self.assertIn("Total time booked", page)
        # months before the configured timesheet start date are left out, and their days need no hours
        start = days[0].replace(day=1)
        self.sql("INSERT OR REPLACE INTO settings (key, value, description) VALUES ('timesheet_start_date', ?, 'x')",
                 (start.isoformat(),))
        page = priya.get(f"/timesheet/summary?year={year}").get_data(as_text=True)
        if start.month > 1:
            self.assertNotIn(f"<strong>{date(year, start.month - 1, 1).strftime('%B')}</strong>", page)
        with self.app.app_context():
            before = {d["iso"]: d for d in day_info(priya_emp, start - timedelta(days=start.weekday() + 7))}
        self.assertTrue(all(d["ideal"] == 0 for d in before.values() if d["iso"] < start.isoformat()))
        self.sql("DELETE FROM settings WHERE key = 'timesheet_start_date'")
        jt = self.login("jtceo")
        self.assertIn("Priya Nair", jt.get(f"/timesheet/view/{priya_emp}?year={year}").get_data(as_text=True))
        self.assertEqual(self.login("js").get(f"/timesheet/view/{priya_emp}?year={year}").status_code, 403)

    def test_5_dashboard_card(self):
        page = self.login("js").get("/dashboard").get_data(as_text=True)
        self.assertIn("This week's timesheet", page)


if __name__ == "__main__":
    unittest.main()
