"""Background retention worker.

Every RETENTION_POLL_SECONDS (default hourly) deletes recordings older than the
`retention_days` setting (0 = keep forever). Runs one pass at startup so a freshly
restarted server reclaims disk immediately. Shares delete_recordings_older_than with
the manual 'delete now' button so both paths behave identically.
"""
import os
import threading

from db import get_settings
from routers.recordings import delete_recordings_older_than

POLL_SECONDS = float(os.environ.get("RETENTION_POLL_SECONDS", "3600"))
_stop = threading.Event()


def _worker():
    print("[retention] worker started", flush=True)
    while not _stop.is_set():
        try:
            days = int(get_settings().get("retention_days", 0) or 0)
            if days > 0:
                n = delete_recordings_older_than(days)
                if n:
                    print(f"[retention] deleted {n} recordings older than {days}d", flush=True)
        except Exception as exc:
            print(f"[retention] error: {exc}", flush=True)
        _stop.wait(POLL_SECONDS)


def start_worker():
    t = threading.Thread(target=_worker, name="retention-worker", daemon=True)
    t.start()
    return t


def stop_worker():
    _stop.set()
