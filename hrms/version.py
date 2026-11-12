"""Application identity: name, version, and the deployed commit / instance when known."""
import os
import subprocess
from functools import lru_cache
from pathlib import Path

APP_NAME = "MITRA HRMS"
VERSION = "1.1.0"


@lru_cache(maxsize=1)
def commit():
    """Git commit SHA of the running code: from the host's environment (Render sets RENDER_GIT_COMMIT),
    else from a local git checkout, else None."""
    for key in ("HRMS_COMMIT", "RENDER_GIT_COMMIT"):
        if os.environ.get(key):
            return os.environ[key].strip()
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent.parent,
                             capture_output=True, text=True, timeout=3)
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and len(sha) >= 7 else None
    except (OSError, subprocess.SubprocessError):
        return None


def deployment_id():
    """Identifier of the running deployment/instance, when the host provides one."""
    return os.environ.get("RENDER_INSTANCE_ID") or os.environ.get("RENDER_SERVICE_ID") or None


def on_render():
    return os.environ.get("RENDER", "").lower() == "true"
