"""Maintenance mode: while a restore replaces the database, every other request gets a 503.

In-process flag (the app runs one Gunicorn worker) plus a lock file so a second process fails safe."""
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_lock = threading.Lock()
_active = False
STALE_SECONDS = 3600


class Busy(Exception):
    """Another restore is already running."""


def active():
    return _active


@contextmanager
def exclusive(lock_path):
    global _active
    if not _lock.acquire(blocking=False):
        raise Busy("A restore is already in progress.")
    path = Path(lock_path)
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and time.time() - path.stat().st_mtime > STALE_SECONDS:
            path.unlink()  # left behind by a crashed restore
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Busy("A restore is already in progress.")
        _active = True
        time.sleep(0.3)  # let requests that already passed the gate finish their statements
        yield
    finally:
        _active = False
        if fd is not None:
            os.close(fd)
            try:
                path.unlink()
            except OSError:
                pass
        _lock.release()
