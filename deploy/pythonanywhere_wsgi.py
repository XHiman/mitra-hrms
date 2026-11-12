# PythonAnywhere WSGI configuration for MITRA HRMS.
# Paste this whole file into the WSGI configuration file linked on the PythonAnywhere "Web" tab,
# replacing everything there. Change YOUR_USERNAME to your PythonAnywhere username.
import os
import sys

PROJECT = "/home/YOUR_USERNAME/mitra-hrms"
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

os.environ.setdefault("HRMS_SECURE_COOKIES", "1")    # site is served over HTTPS
os.environ.setdefault("HRMS_TRUST_PROXY", "1")       # record real client IPs in the audit log
os.environ.setdefault("HRMS_SQLITE_JOURNAL", "DELETE")  # PythonAnywhere's disk does not support SQLite WAL well

from hrms import create_app  # noqa: E402

application = create_app()
