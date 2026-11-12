-- MITRA HRMS Pilot schema (SQLite). Kept ANSI-ish so it can be ported to PostgreSQL.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- RBAC
CREATE TABLE IF NOT EXISTS roles (
    id           INTEGER PRIMARY KEY,
    code         TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL,
    description  TEXT,
    is_system    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS permissions (
    code         TEXT PRIMARY KEY,
    module       TEXT NOT NULL,
    description  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id          INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_code  TEXT    NOT NULL REFERENCES permissions(code),
    PRIMARY KEY (role_id, permission_code)
);

-- ---------------------------------------------------------------- Organisation / employee master
CREATE TABLE IF NOT EXISTS departments (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    code       TEXT,
    parent_id  INTEGER REFERENCES departments(id),
    is_active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS designations (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    level      INTEGER,
    is_active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS employees (
    id                    INTEGER PRIMARY KEY,
    emp_code              TEXT    NOT NULL UNIQUE,
    full_name             TEXT    NOT NULL,
    email                 TEXT,
    phone                 TEXT,
    designation_id        INTEGER REFERENCES designations(id),
    department_id         INTEGER REFERENCES departments(id),
    reporting_officer_id  INTEGER REFERENCES employees(id),
    joining_date          TEXT,
    date_of_birth         TEXT,
    show_birthday         INTEGER NOT NULL DEFAULT 1,   -- day and month shown to colleagues (never the year)
    employment_type       TEXT,
    contract_end_date     TEXT,                         -- last day of the current contract; nothing is booked after it
    status               TEXT    NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'INACTIVE')),
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

-- Contract renewals. Each date starts a new contract term; with the joining date they define the
-- employee's leave cycles. Staff without a joining date follow the calendar leave year.
CREATE TABLE IF NOT EXISTS contract_renewals (
    id            INTEGER PRIMARY KEY,
    employee_id   INTEGER NOT NULL REFERENCES employees(id),
    renewal_date  TEXT    NOT NULL,
    note          TEXT,
    UNIQUE (employee_id, renewal_date)
);

-- ---------------------------------------------------------------- Authentication
CREATE TABLE IF NOT EXISTS users (
    id                    INTEGER PRIMARY KEY,
    employee_id           INTEGER NOT NULL UNIQUE REFERENCES employees(id),
    username              TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash         TEXT    NOT NULL,
    role_id               INTEGER NOT NULL REFERENCES roles(id),
    is_active             INTEGER NOT NULL DEFAULT 1,
    must_change_password  INTEGER NOT NULL DEFAULT 1,
    failed_attempts       INTEGER NOT NULL DEFAULT 0,
    locked_until          TEXT,
    password_changed_at   TEXT,
    last_login_at         TEXT,
    session_version       INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT    NOT NULL
);

-- ---------------------------------------------------------------- Approval workflow configuration
CREATE TABLE IF NOT EXISTS approval_workflows (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    description  TEXT,
    is_default   INTEGER NOT NULL DEFAULT 0,
    is_active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS workflow_stages (
    id                INTEGER PRIMARY KEY,
    workflow_id       INTEGER NOT NULL REFERENCES approval_workflows(id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL,
    name              TEXT    NOT NULL,
    approver_type     TEXT    NOT NULL CHECK (approver_type IN ('REPORTING_OFFICER', 'ROLE', 'USER')),
    approver_role_id  INTEGER REFERENCES roles(id),
    approver_user_id  INTEGER REFERENCES users(id),
    fallback_role_id  INTEGER REFERENCES roles(id),
    min_days          REAL,     -- stage applies only when request days > min_days (NULL = always)
    UNIQUE (workflow_id, seq)
);

-- ---------------------------------------------------------------- Leave configuration
CREATE TABLE IF NOT EXISTS leave_types (
    id                         INTEGER PRIMARY KEY,
    code                       TEXT    NOT NULL UNIQUE,
    name                       TEXT    NOT NULL UNIQUE,
    description                TEXT,
    balance_tracked            INTEGER NOT NULL DEFAULT 1,
    annual_entitlement         REAL,              -- NULL = not yet configured per MITRA policy
    accrual_per_month          REAL,              -- set = credited monthly (1st of month), capped at annual_entitlement
    min_days                   REAL,
    max_days                   REAL,
    requires_document          INTEGER NOT NULL DEFAULT 0,
    document_after_days        REAL,              -- document mandatory when days exceed this
    count_non_working_days     INTEGER NOT NULL DEFAULT 0,
    allow_backdated            INTEGER NOT NULL DEFAULT 0,
    min_notice_days            INTEGER,
    eligible_employment_types  TEXT,              -- comma separated; blank = all
    workflow_id                INTEGER REFERENCES approval_workflows(id),
    credit_based               INTEGER NOT NULL DEFAULT 0,  -- balance earned only through approved comp-off claims
    is_paid                    INTEGER NOT NULL DEFAULT 1,  -- paid leave counts as booked time on the timesheet
    is_active                 INTEGER NOT NULL DEFAULT 1,
    created_at                 TEXT    NOT NULL,
    updated_at                 TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS holidays (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    description  TEXT                -- what the holiday marks, shown to staff
);

CREATE TABLE IF NOT EXISTS leave_balances (
    id             INTEGER PRIMARY KEY,
    employee_id    INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id  INTEGER NOT NULL REFERENCES leave_types(id),
    leave_year     INTEGER NOT NULL,
    entitlement    REAL,
    is_override    INTEGER NOT NULL DEFAULT 0,
    used           REAL    NOT NULL DEFAULT 0,
    pending        REAL    NOT NULL DEFAULT 0,
    UNIQUE (employee_id, leave_type_id, leave_year)
);

-- ---------------------------------------------------------------- Leave transactions
CREATE TABLE IF NOT EXISTS leave_requests (
    id                INTEGER PRIMARY KEY,
    employee_id       INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id     INTEGER NOT NULL REFERENCES leave_types(id),
    from_date         TEXT    NOT NULL,
    to_date           TEXT    NOT NULL,
    days              REAL    NOT NULL,
    half_day          TEXT    CHECK (half_day IN ('FIRST', 'SECOND')),  -- set for a half-day application
    leave_year        INTEGER NOT NULL,
    reason            TEXT    NOT NULL,
    attachment_path   TEXT,
    attachment_name   TEXT,
    attachment_mime   TEXT,
    status            TEXT    NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
    workflow_id       INTEGER REFERENCES approval_workflows(id),
    current_stage     INTEGER,
    submitted_at      TEXT    NOT NULL,
    decided_at        TEXT,
    decided_by        INTEGER REFERENCES users(id),
    decision_remarks  TEXT,
    cancelled_at      TEXT,
    cancelled_by      INTEGER REFERENCES users(id),
    cancel_reason     TEXT
);
CREATE INDEX IF NOT EXISTS ix_leave_requests_emp ON leave_requests(employee_id, status);
CREATE INDEX IF NOT EXISTS ix_leave_requests_dates ON leave_requests(from_date, to_date);

-- Snapshot of the workflow stages taken at submission, so config changes never alter in-flight requests.
CREATE TABLE IF NOT EXISTS request_approvals (
    id                INTEGER PRIMARY KEY,
    request_id        INTEGER NOT NULL REFERENCES leave_requests(id),
    seq               INTEGER NOT NULL,
    stage_name        TEXT    NOT NULL,
    approver_type     TEXT    NOT NULL,
    approver_role_id  INTEGER,
    approver_user_id  INTEGER,
    fallback_role_id  INTEGER,
    status            TEXT    NOT NULL CHECK (status IN ('WAITING', 'PENDING', 'APPROVED', 'REJECTED', 'SKIPPED', 'CANCELLED')),
    acted_by          INTEGER REFERENCES users(id),
    acted_at          TEXT,
    remarks           TEXT,
    UNIQUE (request_id, seq)
);

-- ---------------------------------------------------------------- Compensatory off credits
-- An employee claims credit for work done on a non-working day; the officer who assigned the
-- work approves it, and the approved days are added to the credit-based leave type's balance.
CREATE TABLE IF NOT EXISTS compoff_claims (
    id                INTEGER PRIMARY KEY,
    employee_id       INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id     INTEGER NOT NULL REFERENCES leave_types(id),
    worked_date       TEXT    NOT NULL,
    days              REAL    NOT NULL CHECK (days IN (0.5, 1)),
    leave_year        INTEGER NOT NULL,
    work_details      TEXT    NOT NULL,
    approver_user_id  INTEGER NOT NULL REFERENCES users(id),   -- officer who assigned the work
    status            TEXT    NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
    submitted_at      TEXT    NOT NULL,
    decided_at        TEXT,
    decided_by        INTEGER REFERENCES users(id),
    decision_remarks  TEXT,
    cancelled_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_compoff_emp ON compoff_claims(employee_id, status);
CREATE INDEX IF NOT EXISTS ix_compoff_approver ON compoff_claims(approver_user_id, status);

-- ---------------------------------------------------------------- Resignation and exit
-- The employee resigns from the portal; the reporting officer adds remarks and forwards it;
-- HR accepts it and fixes the last working day, completes the exit clearance and relieves the
-- employee (which closes their login). No notice-period rule is enforced.
CREATE TABLE IF NOT EXISTS resignations (
    id               INTEGER PRIMARY KEY,
    employee_id      INTEGER NOT NULL REFERENCES employees(id),
    status           TEXT    NOT NULL CHECK (status IN ('WITH_OFFICER', 'WITH_HR', 'ACCEPTED', 'RELIEVED',
                                                         'WITHDRAWN', 'REVOKED')),
    reason_category  TEXT    NOT NULL,
    reason           TEXT    NOT NULL,
    proposed_lwd     TEXT    NOT NULL,                       -- last working day asked for by the employee
    notice_days      INTEGER,                                -- notice period setting when submitted (NULL = not set)
    submitted_at     TEXT    NOT NULL,
    officer_user_id  INTEGER REFERENCES users(id),           -- reporting officer at the time of submission
    officer_remarks  TEXT,
    officer_at       TEXT,
    accepted_lwd     TEXT,                                   -- last working day fixed by HR
    hr_remarks       TEXT,
    accepted_by      INTEGER REFERENCES users(id),
    accepted_at      TEXT,
    relieved_by      INTEGER REFERENCES users(id),
    relieved_at      TEXT,
    closed_remarks   TEXT,                                   -- withdrawal / revocation remarks
    closed_by        INTEGER REFERENCES users(id),
    closed_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_resign_emp ON resignations(employee_id, status);

CREATE TABLE IF NOT EXISTS exit_clearance (
    id              INTEGER PRIMARY KEY,
    resignation_id  INTEGER NOT NULL REFERENCES resignations(id),
    seq             INTEGER NOT NULL,
    item            TEXT    NOT NULL,
    done_by         INTEGER REFERENCES users(id),
    done_at         TEXT,
    note            TEXT,
    UNIQUE (resignation_id, seq)
);

-- ---------------------------------------------------------------- Salary slips
-- Amounts are stored in paise (integer) to avoid rounding errors.
CREATE TABLE IF NOT EXISTS salary_structures (
    id              INTEGER PRIMARY KEY,
    employee_id     INTEGER NOT NULL UNIQUE REFERENCES employees(id),
    pan             TEXT,
    bank_name       TEXT,
    bank_account    TEXT,
    ifsc            TEXT,
    updated_by      INTEGER REFERENCES users(id),
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS salary_structure_lines (
    id            INTEGER PRIMARY KEY,
    structure_id  INTEGER NOT NULL REFERENCES salary_structures(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL CHECK (kind IN ('EARNING', 'DEDUCTION')),
    name          TEXT    NOT NULL,
    amount        INTEGER NOT NULL CHECK (amount >= 0),
    seq           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS salary_slips (
    id                INTEGER PRIMARY KEY,
    employee_id       INTEGER NOT NULL REFERENCES employees(id),
    month             TEXT    NOT NULL,                 -- YYYY-MM
    status            TEXT    NOT NULL CHECK (status IN ('DRAFT', 'ISSUED', 'CANCELLED')),
    days_in_month     INTEGER NOT NULL,
    lop_days          REAL    NOT NULL DEFAULT 0,       -- loss of pay (unpaid) days
    gross             INTEGER NOT NULL DEFAULT 0,
    total_deductions  INTEGER NOT NULL DEFAULT 0,
    net               INTEGER NOT NULL DEFAULT 0,
    details           TEXT    NOT NULL,                 -- JSON snapshot: name, designation, PAN, bank ... at issue time
    remarks           TEXT,
    created_by        INTEGER REFERENCES users(id),
    created_at        TEXT    NOT NULL,
    issued_by         INTEGER REFERENCES users(id),
    issued_at         TEXT,
    cancelled_by      INTEGER REFERENCES users(id),
    cancelled_at      TEXT,
    cancel_reason     TEXT
);
CREATE INDEX IF NOT EXISTS ix_salary_slips ON salary_slips(employee_id, month, status);

CREATE TABLE IF NOT EXISTS salary_slip_lines (
    id       INTEGER PRIMARY KEY,
    slip_id  INTEGER NOT NULL REFERENCES salary_slips(id) ON DELETE CASCADE,
    kind     TEXT    NOT NULL CHECK (kind IN ('EARNING', 'DEDUCTION')),
    name     TEXT    NOT NULL,
    amount   INTEGER NOT NULL CHECK (amount >= 0),
    seq      INTEGER NOT NULL
);

-- ---------------------------------------------------------------- Timesheets
CREATE TABLE IF NOT EXISTS timesheet_projects (
    id                INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL UNIQUE,
    description       TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,      -- 1 only while status is ACTIVE: offered in the timesheet
    code              TEXT,                            -- short project code, unique when given
    department_id     INTEGER REFERENCES departments(id),
    head_employee_id  INTEGER REFERENCES employees(id),  -- project head / manager
    start_date        TEXT,
    end_date          TEXT,
    funding           TEXT,                            -- funding source, sponsor or client
    status            TEXT    NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ON_HOLD', 'COMPLETED'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_project_code ON timesheet_projects(code) WHERE code IS NOT NULL;

-- Staff assigned to a project (set by HR on the person's page). Assigned projects are listed first in
-- that person's timesheet; anyone may still book time on any active project.
CREATE TABLE IF NOT EXISTS project_members (
    project_id   INTEGER NOT NULL REFERENCES timesheet_projects(id),
    employee_id  INTEGER NOT NULL REFERENCES employees(id),
    PRIMARY KEY (project_id, employee_id)
);

CREATE TABLE IF NOT EXISTS timesheets (
    id            INTEGER PRIMARY KEY,
    employee_id   INTEGER NOT NULL REFERENCES employees(id),
    week_start    TEXT    NOT NULL,                        -- Monday of the week
    status        TEXT    NOT NULL CHECK (status IN ('DRAFT', 'SUBMITTED')),
    total_hours   REAL    NOT NULL DEFAULT 0,
    submitted_at  TEXT,
    updated_at    TEXT    NOT NULL,
    UNIQUE (employee_id, week_start)
);

CREATE TABLE IF NOT EXISTS timesheet_entries (
    id            INTEGER PRIMARY KEY,
    timesheet_id  INTEGER NOT NULL REFERENCES timesheets(id) ON DELETE CASCADE,
    work_date     TEXT    NOT NULL,
    project_id    INTEGER NOT NULL REFERENCES timesheet_projects(id),
    hours         REAL    NOT NULL CHECK (hours > 0 AND hours <= 16),
    description   TEXT    NOT NULL,
    seq           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_timesheet_entries ON timesheet_entries(timesheet_id, work_date);

-- Reminders already sent (so each one goes out once, however often the check runs).
CREATE TABLE IF NOT EXISTS reminder_log (
    key      TEXT PRIMARY KEY,
    sent_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------- Notifications
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    link        TEXT,
    is_read     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, is_read);

-- ---------------------------------------------------------------- Audit (append-only, hash-chained)
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    user_id     INTEGER,
    username    TEXT,
    role_code   TEXT,
    action      TEXT    NOT NULL,
    entity      TEXT    NOT NULL,
    entity_id   TEXT,
    prev_state  TEXT,
    new_state   TEXT,
    remarks     TEXT,
    ip          TEXT,
    user_agent  TEXT,
    prev_hash   TEXT    NOT NULL,
    hash        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_logs(entity, entity_id);

CREATE TRIGGER IF NOT EXISTS audit_logs_no_update BEFORE UPDATE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete BEFORE DELETE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END;

-- ---------------------------------------------------------------- System settings
CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    description  TEXT
);
