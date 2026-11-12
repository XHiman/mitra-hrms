"""Small shared helpers: time, settings, validation."""
from datetime import date, datetime, timedelta, timezone

from flask import current_app

from .db import query, scalar


class ValidationError(Exception):
    """Raised by services for user-correctable input problems."""


def utcnow_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today():
    """Today's date in the organisation's local time (IST by default)."""
    offset = timedelta(minutes=current_app.config["DISPLAY_UTC_OFFSET_MINUTES"])
    return (datetime.now(timezone.utc) + offset).date()


def to_local(iso_ts):
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    offset = timedelta(minutes=current_app.config["DISPLAY_UTC_OFFSET_MINUTES"])
    return (dt.astimezone(timezone.utc) + offset).strftime("%d %b %Y, %H:%M")


def parse_date(value, field="Date"):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise ValidationError(f"{field} is not a valid date.")


def fmt_date(value):
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%d %b %Y")


def fmt_size(n):
    """Human-readable byte count: 1.4 MB."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_days(value):
    if value is None:
        return "—"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS = {
    "weekend_days": ("5,6", "Weekly off days (0=Mon … 6=Sun), comma separated"),
    "leave_year_start_month": ("1", "Month in which the leave year starts (1=Jan, 4=Apr)"),
    "allow_cancel_approved": ("1", "Employees may cancel approved leave before it starts (1/0)"),
    "renewal_reminder_days": ("30,15,7,1", "Days before a contract renewal / end date to remind the employee and HR"),
    "timesheet_daily_hours": ("8", "Hours expected on each working day in the timesheet"),
    "timesheet_start_date": ("", "First date timesheets are required (YYYY-MM-DD); earlier days have no ideal hours. Blank = no limit"),
    "timesheet_holidays_expected": ("1", "Public holidays expect the daily hours on the timesheet like other weekdays (1/0)"),
    "compoff_non_working_only": ("1", "Comp-off may be claimed only for work on a weekly off or holiday (1/0)"),
    "resignation_notice_days": ("0", "Notice period for resignations in days, shown to staff (0 = not set; never blocks)"),
    "exit_clearance_items": ("Work and files handed over to the reporting officer;Laptop and office equipment returned;"
                             "ID card and access card returned;No dues from Accounts;Email and system access closed",
                             "Exit clearance checklist for HR, items separated by ;"),
    "employment_types": ("Permanent,Contractual,Consultant,Deputation,Intern",
                         "Employment types available in the employee master"),
    "password_min_length": ("10", "Minimum password length"),
    "password_max_age_days": ("0", "Force password change after N days (0 = never)"),
    "max_failed_logins": ("5", "Failed logins before temporary lockout"),
    "lockout_minutes": ("15", "Lockout duration in minutes"),
    "backup_retention": ("30", "Number of database backups to keep"),
}


def get_setting(key):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    if row is not None:
        return row["value"]
    return DEFAULT_SETTINGS[key][0]


def get_int_setting(key):
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return int(DEFAULT_SETTINGS[key][0])


def weekend_days():
    raw = get_setting("weekend_days")
    return {int(x) for x in raw.split(",") if x.strip().isdigit() and 0 <= int(x) <= 6}


STAFF_GROUPS = {"": "All staff", "yp": "Young Professionals", "ro": "Research Officers", "other": "All others"}


def staff_group(designation):
    """'yp', 'ro' or 'other', from the designation name."""
    d = (designation or "").lower()
    if "young professional" in d:
        return "yp"
    if "research officer" in d:
        return "ro"
    return "other"


def staff_group_arg(value):
    """A valid group key from a request argument ('' = all staff)."""
    return value if value in STAFF_GROUPS else ""


def employment_types():
    return [t.strip() for t in get_setting("employment_types").split(",") if t.strip()]


def leave_year_of(d):
    start = get_int_setting("leave_year_start_month")
    return d.year if d.month >= start else d.year - 1


def leave_year_label(year):
    start = get_int_setting("leave_year_start_month")
    return str(year) if start == 1 else f"{year}-{str(year + 1)[-2:]}"


def leave_year_bounds(year):
    start = get_int_setting("leave_year_start_month")
    first = date(year, start, 1)
    nxt = date(year + 1, start, 1)
    return first, nxt - timedelta(days=1)


def clean_text(value, field, required=False, max_len=200):
    value = (value or "").strip()
    if required and not value:
        raise ValidationError(f"{field} is required.")
    if len(value) > max_len:
        raise ValidationError(f"{field} must be at most {max_len} characters.")
    return value


def opt_float(value, field, minimum=0):
    value = (value or "").strip()
    if value == "":
        return None
    try:
        num = float(value)
    except ValueError:
        raise ValidationError(f"{field} must be a number.")
    if num < minimum or num > 10000:
        raise ValidationError(f"{field} is out of range.")
    return num


def opt_int(value, field, minimum=0, maximum=100000):
    value = (value or "").strip()
    if value == "":
        return None
    if not value.lstrip("-").isdigit():
        raise ValidationError(f"{field} must be a whole number.")
    num = int(value)
    if num < minimum or num > maximum:
        raise ValidationError(f"{field} is out of range.")
    return num


def count_scalar(sql, params=()):
    return scalar(sql, params) or 0
