"""Job waiting primitives shared by menu_test (and unit-tested in isolation).

The backend's terminal statuses are exactly ("done", "error", "cancelled")
-- note the double-l, matching src/arca/jobs.py. "canceled" is not a status
this app ever reports.
"""
from __future__ import annotations

import time

TERMINAL = ("done", "error", "cancelled")
POLL = 0.4
DEFAULT_GRACE = 30.0


def await_job(fetch, cancel, timeout, grace=DEFAULT_GRACE,
              sleep=time.sleep, clock=time.monotonic):
    """Wait for one job (fetch() -> dict | None) to reach a terminal status.

    Timing contract:
    - The full ``timeout`` is pure waiting: a slow task is never cancelled
      halfway. ``cancel`` is called at most once, only after the whole
      timeout has elapsed with the job still queued/running.
    - After cancelling, a separate ``grace`` window allows the backend to
      land on a real terminal status (cancelled / error / done).

    Returns ``(job, reclaimed)``: ``job`` is the last snapshot (possibly
    None if the job vanished from the list); ``reclaimed`` is False only
    when the job stayed active even after cancellation -- the caller must
    surface that as a diagnostic instead of proceeding blindly.
    """
    deadline = clock() + timeout
    job = fetch()
    while job is not None and job.get("status") not in TERMINAL and clock() < deadline:
        sleep(POLL)
        job = fetch()
    if job is None or job.get("status") in TERMINAL:
        return job, True
    try:
        cancel()
    except Exception:
        pass
    deadline = clock() + grace
    while job is not None and job.get("status") not in TERMINAL and clock() < deadline:
        sleep(POLL)
        job = fetch()
    return job, job is not None and job.get("status") in TERMINAL


def reclaim_active(list_jobs, cancel, timeout=60.0,
                   sleep=time.sleep, clock=time.monotonic):
    """Cancel every non-terminal job in list_jobs() and wait for recovery.

    Used on teardown paths: no test directory may be deleted while jobs of
    this round are still running. Returns ``(left_active, jobs)`` where
    ``left_active`` is 0 on full recovery.
    """
    def actives():
        return [j for j in (list_jobs() or []) if j.get("status") not in TERMINAL]

    for job in actives():
        try:
            cancel(job["id"])
        except Exception:
            pass
    deadline = clock() + timeout
    while clock() < deadline:
        if not actives():
            return 0, list_jobs()
        sleep(POLL)
    left = actives()
    return len(left), list_jobs()
