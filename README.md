# MITRA HRMS – Pilot (Leave Management & Role-based Dashboards)

A deliberately limited pilot of the MITRA Human Resource Management System. It covers the full basic
leave lifecycle and nothing else:

> Employee login → Apply leave → Supervisor receives request → Approve / Reject → Employee notified →
> Leave balance updated → Transaction recorded in the audit log

Out of scope for the pilot: recruitment, onboarding, attendance, WFH/field duty, payroll, reimbursement,
LMS/training, performance/KRA, contracts, helpdesk, exit, advanced analytics and external integrations
(email/SMS/WhatsApp).

**Stack:** Python 3.11+, Flask 3, SQLite (schema kept portable to PostgreSQL). No front-end build step.

---

## 1. Quick start (pilot walkthrough)

```powershell
pip install -r requirements.txt
flask --app run init-db          # tables, permissions, default roles, settings, default workflow
flask --app run seed-demo        # fictional organisation for the walkthrough
python run.py                    # http://127.0.0.1:5000
```

All accounts use the shared pilot password `Mitra@2026` (set for every user on 18 Oct 2026). Give each person their own password before real use.

| Username | Role | Reports to |
|---|---|---|
| `ceo` | CEO | – |
| `jtceo` | Joint CEO | ceo |
| `js` | Joint Secretary | ceo |
| `hr` | HR | js |
| `rahul`, `priya`, `kavita` | Employee | jtceo |
| `amit` | Employee | js |
| `admin` | System Administrator / Developer | js |

**Leave entitlements are intentionally blank.** The pilot does not invent entitlements. Log in as `admin`
→ *Leave configuration* and enter the values from MITRA's approved HR policy. Until a leave type has an
entitlement, employees cannot apply for it, and the admin dashboard shows an alert.

Suggested walkthrough: `admin` configures entitlements → `rahul` applies for leave → `jtceo` sees it on the
supervisory dashboard and approves/rejects with remarks → `rahul` sees the status, remarks, notification and
updated balance → `admin` reviews the audit log (*Audit log → Verify chain*).

## 2. Production setup

```powershell
pip install -r requirements.txt
flask --app run init-db
flask --app run create-admin --username <name> --name "<Full Name>" --emp-code <code>
waitress-serve --listen=127.0.0.1:8080 --call run:create_app
```

`create-admin` prints a one-time password that must be changed at first login. Do **not** run
`seed-demo` in production.

Put the app behind an HTTPS reverse proxy (IIS/ARR, nginx) and set `HRMS_SECURE_COOKIES=1`.

| Environment variable | Purpose | Default |
|---|---|---|
| `HRMS_INSTANCE_DIR` | Folder for database, uploads, backups, secret key | `./instance` |
| `HRMS_DATABASE` | Database file path | `<instance>/hrms.db` |
| `HRMS_SECRET_KEY` | Session signing key | generated into `<instance>/secret_key` |
| `HRMS_SECURE_COOKIES` | `1` = cookies sent over HTTPS only | `0` |
| `HRMS_SESSION_IDLE_MINUTES` | Idle session timeout | `30` |
| `HRMS_HOST` / `HRMS_PORT` | Development server bind (`run.py` only) | `127.0.0.1` / `5000` |

The `instance/` folder holds the database, uploaded documents and the secret key. Restrict file-system
access to it and keep it out of version control.

### Onboarding staff from Excel

HR fills `onboarding/MITRA_Staff_Onboarding_Template.xlsx` (one row per person). Then:

```powershell
flask --app run import-staff path\to\sheet.xlsx --dry-run      # check only, changes nothing
flask --app run import-staff path\to\sheet.xlsx --login-url https://hrms.example.in
```

- The whole sheet is checked first: duplicate IDs, unknown roles, reporting officers missing from the
  sheet, circular reporting lines, bad dates and emails. If anything is wrong, nothing is imported.
- **New staff** get a username (`firstname.lastname` unless one is given) and a one-time password.
- **Staff already in the system** (matched on Employee ID) are updated: name, designation, department,
  role and reporting officer. Their passwords are not touched, so the sheet can be re-imported whenever
  it changes.
- New logins are written to `instance/login-slips-<date>.html`, a printable sheet of cut-out slips.
  It is confidential: print it, hand out the slips, then delete the file.

### Backups

- From the UI: *Admin → Backups → Create backup* (download is available to the administrator).
- Scheduled: run `flask --app run backup-db` daily, for example from Windows Task Scheduler.
  Backups use SQLite's online backup API, so the app can stay running. Retention is set by the
  `backup_retention` setting.
- The admin dashboard warns if no backup has been taken in the last 24 hours. Also copy the backups off
  the server.

## 3. Roles and access

| Feature | Employee | CEO / Jt CEO / JS / HR | Admin / Developer |
|---|---|---|---|
| Own profile, apply and view own leave | ✓ | ✓ | ✓ |
| Approve / reject leave | – | ✓ (only requests the workflow assigns to them) | – |
| Approve / reject comp-off | – | ✓ (only claims for work they assigned) | – |
| View employee leave data | Own only | Reporting chain (CEO and HR: all) | ✓ read only |
| Users, roles, leave configuration, workflows, organisation, settings | – | – | ✓ |
| Audit log | Own requests | Requests in scope | Complete |

- Roles and permissions are stored in the database and editable under *Admin → Roles*.
- **Segregation of duties is enforced:** a role holding `leave.approve` cannot also hold user, role,
  workflow or leave-configuration management permissions.
- Every permission is checked on the server. Hiding menu items is only cosmetic.

### Compensatory off (comp-off)

1. **Claim:** the employee opens *Comp-off claims* and enters the date worked (full or half day), the work
   done, and **the officer who assigned the work**. That officer can be anyone with comp-off approval
   rights: CEO, Joint CEO, Joint Secretary, HR or a Reporting Manager. The employee's own reporting officer
   is listed first.
2. **Approve / reject:** only that officer can decide, from *Comp-off approvals* or the supervisory
   dashboard. Rejection needs remarks. If the officer is deactivated or loses approval rights, HR users take
   over the claim.
3. **Credit:** on approval, the days are added to the employee's *Compensatory Off* balance for the leave
   year in which the work was done. Every step is audited and notified.
4. **Avail:** the employee applies for *Compensatory Off* leave like any other leave. It goes through the
   normal approval workflow and uses up the credited balance.

Rules enforced:
- Claims can't be made for future dates, the same date twice, or a day the employee was on approved leave.
- Claims are limited to weekly offs and holidays unless the `compoff_non_working_only` setting is set to `0`.
- The comp-off balance can't be overridden manually; it changes only through claims.
- Not yet implemented, pending MITRA policy: expiry of comp-off credits and carry-forward into the next
  leave year.

The **Reporting Manager** role is for team leads who aren't CEO, Joint CEO, JS or HR. It can approve
leave and comp-off and see its own reporting chain.

## 4. Configuration (all from the Admin dashboard)

- **Leave types:** name, code, entitlement, minimum/maximum days per request, whether to count weekly offs
  and holidays, backdated applications, minimum notice, eligible employment types, whether a document is
  mandatory (always, or above N days), approval workflow, active/inactive.
- **Per-employee balance override:** on the user edit page, for exceptions approved by HR policy. Overrides
  are audited.
- **Holidays:** count as non-working days in the leave-day calculation.
- **Approval workflows:** ordered stages. Each stage's approver is the *reporting officer*, a *role*, or a
  *specific user*. A stage can have a fallback role and can apply only above N days. In-flight requests keep
  the stages they were submitted with, so configuration changes never alter them.
- **Organisation:** departments (with parent), designations, reporting officers.
- **Settings:** weekly off days, leave-year start month, whether approved leave can be cancelled, whether
  comp-off is limited to weekly offs and holidays, employment types, password policy, lockout and backup
  retention.
- **Compensatory Off leave type:** created automatically and marked *credit-based*, so its balance comes
  only from approved claims. Its rules (minimum/maximum days, notice, eligibility, workflow) are configured
  like any other leave type.

### Analytics (charts for supervisors)

*Team → Analytics* (`/analytics`) shows charts and comparisons. You choose **who** and **which period**.

- **Who:**
  - *My team*: everyone in your reporting chain. Permission `analytics.view_team`: CEO, Joint CEO,
    Joint Secretary, HR and reporting managers.
  - *Whole office*, or *Department: …*. Permission `analytics.view_office`: CEO, Joint CEO and HR.
  - Admins see no analytics. Reporting officers cannot see other teams.
- **Period:** this month, last month, or the last 3, 6 or 12 months. Figures run up to today.
- **Staff:** All staff, Young Professionals, Research Officers, or All others. The group comes from the
  designation name. *Team timesheets* has the same filter as tabs.
- **Summary:** staff, time booked against ideal, utilisation (booked ÷ ideal), shortfall, leave days,
  and the share of completed weeks whose timesheet was submitted.
- **Views:**
  - **Compare people:** a stacked bar per person (project hours, paid leave, shortfall), highest
    utilisation first. Names open the person's timesheet calendar where you may view it.
  - **Projects:** hours per project with its share. Click a project to see hours by person.
  - **Departments** (whole office only): utilisation per department.
  - **Leave:** leave days by type and by person, paid and unpaid.
  - **Monthly trend:** a line chart of ideal against booked hours per month.
- Every chart has its figures in a table underneath.
- The charts use the timesheet's own day rules: holidays, half days, paid and unpaid leave, joining and
  contract dates, and the timesheet start date.
- Only totals are shown, never leave reasons or task notes. Only active employees are included.
- The charts are drawn on the server; no outside chart library is loaded.

### Half-day leave and paid / unpaid leave types

- **Half day:** on *Apply for leave*, choose *Duration: Half day, first half (morning)* or *second half
  (afternoon)*.
  - A half day is one working date and counts as 0.5 day against the balance.
  - The other half of the same date can be taken as a second half day. A full day or the same half
    overlaps and is refused.
  - Lists and notifications show "(first half)" or "(second half)".
- **Paid flag:** every leave type has *Paid leave* (on by default). Paid leave counts as booked time on
  the timesheet; unpaid leave does not.

### Contracts and leave cycles

- **Staff with a date of joining** follow contract leave cycles:
  - A cycle starts on the joining date and again on every contract renewal date.
  - A term longer than a year is split at each joining anniversary. After the last recorded date, the
    cycle repeats yearly.
  - Earned Leave is credited after each completed month of the cycle (1.5 days, up to 18), and the
    balance restarts with each cycle.
- **Staff without a joining date** (e.g. officers on deputation) follow the calendar leave year.
- **Contract end date:**
  - HR records each person's last contract day under *Contract* on the employee page (permission
    `contracts.manage`, audited as CONTRACT_END_SET), and extends it on renewal. HR cannot set their own.
  - **No leave can be applied for, and no timesheet hours booked, after that date.**
  - If no end date is recorded, nothing is blocked. *Pending actions* reminds HR how many staff have no
    end date, and the Contracts list shows the date for everyone.
- **Where it shows:**
  - Joining date, current contract and renewal due date appear on each person's dashboard and profile.
  - Renewals due in the next 60 days are listed on the supervisory dashboard.
  - Admins add or remove renewal dates on the user's page (*Contract dates*); balances are recalculated
    automatically.
- **Importing HR's employee datasheet** (sheets MITRA, SDA, MURWEP):
  `flask --app run import-datasheet "Employees data sheet.xlsx" --dry-run`, then run it without
  `--dry-run`.
  - It records joining and renewal dates, dates of birth and mobile numbers, adds people not yet in the
    system, and lists every unclear value for HR to check.
  - `flask --app run rebuild-balances` recalculates every balance after bulk date changes.

### Timesheets

- **Staff (*My timesheet*)** opens as a month calendar, and all editing happens on it:
  - The title gives the month's **total time booked** against the **ideal time**, for example
    "October 2026: total time booked 69.5 of 176 h".
  - Summary at the top:
    - **Ideal time:** days × 8 h.
    - **Total time booked:** project hours + paid leave.
    - **Shortfall:** ideal time − total time booked, for the whole month.
    - Leave days, paid and unpaid.
    - Up to today: booked against ideal so far.
  - Each day is coloured: complete, short, not booked, paid leave, unpaid leave, holiday, weekly off,
    upcoming, or closed (before joining, before timesheets started, after the contract end).
  - Click a past day and an **editor opens beside the calendar**, below it on a phone:
    - Rows of project, hours (a whole number from 1 to 8, chosen from a list) and a short note.
    - 8 h can go on one project, or be split, e.g. 4 + 4.
    - *Save and next day* moves along the week.
  - **Approved leave is linked to its day automatically:**
    - A paid full day counts 8 h; a paid half day counts 4 h, and the other 4 h are booked on projects.
    - Unpaid leave counts 0 h, so the day shows a shortfall.
    - Leave is changed from the Leave page, not the timesheet.
  - The right-hand column shows each week's status, with a *Submit* button.
  - Below the calendar, *Where the time went* totals hours by project, plus paid leave.
  - *Week view* is read only.
  - A submitted week is locked. The employee can reopen it from the editor, and this is audited.
  - There is no approval step.
- **Month-wise summary** (*Month-wise summary* button; `/timesheet/summary`): one row per month with
  ideal time, project hours, paid leave, total time booked, shortfall and leave days. Supervisors open
  it for their staff from the calendar.
- **Ideal hours** (*System settings*):
  - `timesheet_daily_hours`: 8 by default.
  - `timesheet_holidays_expected`: 1 by default, so **public holidays count as weekdays**
    (October 2026 = 22 weekdays × 8 = 176 h). Set it to 0 to give holidays no ideal hours (160 h).
  - `timesheet_start_date`: set to 2026-10-01 on the live system. Earlier days have no ideal hours,
    and earlier months are left out of the summary.
  - Weekly offs have no ideal. Days below the ideal are flagged, never blocked.
  - Hours cannot be entered for future dates, before the joining date, before the timesheet start
    date, or **after the contract end date**. At most 16 hours a day.
- **Supervisors** see their own team, and HR / admin / CEO see everyone:
  - *Team timesheets*: weekly status (submitted, draft, not started, short of hours), and each
    person's week or month calendar (read only).
  - *Timesheet report*: hours by person and by project for a month, with a *Download for Excel* CSV.
- **Projects** (*HR → Projects*, permission `timesheet.manage`): *Add project* opens a form with name, code
  (optional, unique), department, project head, start and end dates, funding source or client, status and
  description.
  - Status is Active, On hold or Completed. **Only Active projects can be chosen in timesheets.** Past
    entries always keep their project.
  - The list can be filtered by status and shows hours booked and the number of people. Each project's
    page shows who has booked time on it.
  - Every change is audited.

### Salary slips

- **Who:** HR and the Accounts Officer role (permission `payroll.manage`). System administrators have no
  access, and salary amounts are kept out of the audit log.
- **Pay structure:** *Payroll → Salary slips → Set up* for each person. Enter the monthly earnings and
  deductions lines with amounts, plus PAN and bank details. The system does not invent pay rules.
- **Each month:**
  1. *Prepare drafts for all* creates draft slips from the pay structures.
  2. Open a draft to enter loss-of-pay days. *Recalculate* reduces earnings in proportion; any amount can
     also be corrected by hand.
  3. *Issue* locks the slip and notifies the employee.
- **Staff** see their own issued slips under *My salary slips* and can print or save them as PDF.
- **Mistakes:** withdraw the issued slip with a reason, then prepare a corrected one.
- **Own slip:** nobody can prepare, issue or change their own slip; another payroll officer must.

### Contract renewal reminders

- **Who is notified:** the employee and HR (everyone with the HR role).
- **When:**
  - Renewal date recorded: the employee and HR are reminded 30, 15, 7 and 1 days before. The days can be
    changed as `renewal_reminder_days` in *System settings*.
  - No next renewal date recorded: no end date is assumed. HR gets one notification listing everyone
    without a date, weekly, and at once when someone new joins the list (e.g. a renewal date passes with no
    further date). HR records the dates on *Contract renewals*. Employees are not notified in this case.
- **No duplicates:** each reminder is sent once, and reminders missed while the server was off are not
  sent late.
- **How the check runs:**
  - automatically on the first signed-in visit of each day
  - on a server, also schedule `flask --app run send-reminders` daily, e.g. in Windows Task Scheduler
- **Recording renewals:** HR (permission `contracts.manage`) records or removes renewal dates on the
  employee's page (*Contract* box).
  - Leave cycles and reminders follow automatically.
  - HR cannot record her own renewal; the administrator does.
  - Renewals due within 60 days are listed on the supervisory dashboard.

### Resignation and exit

- **Employee (*Resignation* in the menu):** chooses a reason, writes the letter, asks for a last working
  day (today or later) and ticks a confirmation box. Only one resignation can be open at a time. It can
  be withdrawn until HR accepts it.
- **Reporting officer:** adds remarks (required) and forwards it to HR. The officer cannot reject it.
  If the officer can't act (none recorded, inactive, or without approval rights), the resignation goes
  straight to HR.
- **HR (permission `exit.manage`):** accepts it and fixes the last working day, which may differ from
  the one asked for.
  - When HR resigns, the CEO handles it, because no one may act on their own resignation.
  - After acceptance, HR can *revoke* it with remarks if the employee stays on.
- **Exit clearance:** a checklist created when HR accepts. The items are set in *System settings* as
  `exit_clearance_items`, separated by `;`. The defaults are:
  - handover
  - equipment
  - ID card
  - no dues from Accounts
  - email and system access
- **Relieve:** available to HR only when:
  - the last working day has been reached
  - all clearance items are done
  - no leave requests or comp-off claims are pending
  - the person is not the last user with role-management rights

  Relieving closes the login, ends any open sessions and marks the employee inactive. Their records stay.
  HR is reminded of any staff who report to the person, so a new reporting officer can be assigned.
- **Notice period:** `resignation_notice_days` in *System settings*, 0 by default (not set). It is only
  shown, as notice given against notice required, and never blocks anything.
- Every step notifies the people involved and appears on *Pending actions*. Every step is audited:
  - RESIGNATION_SUBMITTED, RESIGNATION_FORWARDED, RESIGNATION_ACCEPTED
  - RESIGNATION_WITHDRAWN, RESIGNATION_REVOKED
  - EXIT_CLEARANCE_UPDATED, EMPLOYEE_RELIEVED

### Holidays and birthdays

- **Holidays:** the Government of Maharashtra public holiday list for 2026 (GAD notification, December
  2025) ships in `hrms/data/holidays/maharashtra-2026.csv`, with the occasion for each holiday. Load it
  with *Leave configuration → Load official holidays*, or `flask --app run load-holidays 2026`.
  - Holidays appear on every calendar and on the *Holiday list* page, and are not counted as leave days.
  - Each year, when GAD notifies the next list, add `maharashtra-<year>.csv` in the same format (or enter
    the dates by hand) and load it.
- **Birthdays:** staff enter their date of birth under *My profile*. Admins can also enter it on the user
  form, and the onboarding sheet has a *Date of Birth* column.
  - Birthdays appear on everyone's calendar and in the dashboard's *Birthdays* box.
  - Only the day and month are shown, never the year or age.
  - Anyone can hide theirs by unticking *Show my birthday on the MITRA calendar*.

### Upgrading an existing database

After pulling a new version, run `flask --app run init-db` once. It is safe to repeat. It adds new
tables and columns, new permissions (granted to the matching existing roles), new default roles and the
Compensatory Off leave type, without touching existing data.

## 5. Security controls

- Passwords hashed with scrypt. Minimum length and maximum age are configurable. Users must change one-time
  and reset passwords at next login.
- Account lockout after repeated failed logins. Idle and absolute session timeouts. Deactivating a user or
  resetting their access ends their existing sessions.
- CSRF token on every form, security headers, and parameterised SQL throughout.
- Uploads: PDF/JPG/PNG only, content checked against the file extension, 5 MB limit. Files are stored
  outside the web root under random names and served only to users authorised for that request.
- Audit log: append-only (database triggers block UPDATE and DELETE) and SHA-256 hash-chained. The chain
  can be verified from the audit page. Each entry records user, role, timestamp, action, record, previous
  and new state, remarks, IP address and user agent.

## 6. Code layout

```
hrms/
  core/       db, security (auth/session/CSRF), rbac, audit, utils, schema.sql
  services/   leave (rules, balances), workflow (approval engine), notifications
  modules/    web blueprints: auth, dashboards, leave, approvals, notifications, audit_views, admin/
  templates/  Jinja templates per module
  static/     css / js (no external CDN)
tests/        end-to-end tests
```

Each concern (authentication, RBAC, employee master, leave, workflow, notifications, audit, dashboards) is a
separate module. Future HRMS modules can be added as new `services/` + `modules/` blueprints that reuse the
same RBAC, workflow, notification and audit services.

## 7. Tests

```powershell
python -m unittest discover -s tests -v
```

The browser journeys need Playwright, which uses the installed Google Chrome. Without it they are
skipped:

```powershell
pip install playwright
```

- `test_e2e_browser.py` drives a real browser through the users' journeys:
  - first login and setting a password
  - apply, approve and reject leave
  - comp-off
  - HR issuing a salary slip and the employee printing it
  - a phone-width check with no sideways scrolling
  - every form field having a label
  - no JavaScript or security-policy errors on any page
- `test_robustness.py` covers the client's (MITRA's) concerns:
  - every page for every role, with no crashes and no access leaks
  - CSRF protection on every form
  - hostile and malformed input
  - other people's records
  - sessions
  - simultaneous clicks
  - 250 random actions with balance and audit-chain checks
  - performance with 130 staff

The other test files cover each feature.

The tests cover:
- the full lifecycle, including balance updates, notifications and audit entries
- out-of-scope approval attempts: an approver outside the chain, the admin, and self-approval
- rejection without remarks, and overlapping requests
- document rules and upload validation
- page-level access control, CSRF, and segregation of duties
- audit immutability and hash-chain verification
- account lockout
- multi-stage workflows, and creating a user with a one-time password
- comp-off: claim rules, approval only by the assigning officer, crediting the balance, and availing it as
  leave
