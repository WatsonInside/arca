"""Direct-child process registry for running jobs.

Cancellation is cooperative: tasks poll a ``threading.Event``.  A child that
is blocked writing into a full pipe, or that takes a long time inside a single
file, will not notice that flag until its next read returns.  Registering each
engine child here lets the *cancelling* thread terminate it, which makes the
blocked read return EOF/error so the task can unwind and the worker can reap
it.

Only direct children spawned by this process are registered.  Cancellation
never walks process groups or matches by name -- killing an unrelated process
would be far worse than a slow cancel.
"""

import threading

_lock = threading.Lock()
_local = threading.local()
_by_job: dict[str, set] = {}


def bind(job_id: str) -> None:
    """Bind the current worker thread to a job id (called by the worker)."""
    _local.job_id = job_id


def unbind() -> None:
    _local.job_id = ""


def current() -> str:
    return getattr(_local, "job_id", "")


def register(proc):
    """Remember *proc* as a child of the current job; returns it unchanged."""
    job_id = current()
    if not job_id or proc is None:
        return proc
    with _lock:
        _by_job.setdefault(job_id, set()).add(proc)
    return proc


def unregister(proc) -> None:
    job_id = current()
    if not job_id:
        return
    with _lock:
        procs = _by_job.get(job_id)
        if procs is not None:
            procs.discard(proc)
            if not procs:
                _by_job.pop(job_id, None)


def pids(job_id: str) -> list[int]:
    with _lock:
        return [p.pid for p in _by_job.get(job_id, ()) if p.pid]


def forget(job_id: str) -> None:
    """Drop any registrations left behind by a task that did not clean up."""
    with _lock:
        _by_job.pop(job_id, None)


def terminate_job(job_id: str, grace: float = 3.0) -> list[int]:
    """Terminate and reap every registered child of *job_id* (bounded).

    Runs on the cancel/quit caller's thread: ``terminate`` first, a bounded
    wait, then ``kill`` and another bounded wait.  The owning task thread still
    closes the pipes in its own ``finally`` -- closing another thread's stream
    here would race with the reader.
    """
    with _lock:
        procs = list(_by_job.get(job_id, ()))
    stopped: list[int] = []
    for proc in procs:
        if proc.poll() is not None:
            stopped.append(proc.pid or 0)
            continue
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 - best effort: see docstring
            pass
        try:
            proc.wait(timeout=grace)
        except Exception:  # noqa: BLE001 - fall through to kill
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=grace)
            except Exception:  # noqa: BLE001
                pass
        stopped.append(proc.pid or 0)
    return stopped
