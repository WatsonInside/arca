"""Background job queue: two directory-isolated workers, progress + history."""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid

from . import archtxn
from . import engine as eng
from . import par2 as par2_mod
from . import procs
from . import util
from . import keyring
from . import zipfix
from . import zst


class Job:
    __slots__ = ("id", "kind", "title", "subtitle", "status", "percent", "phase",
                 "item", "speed", "eta", "total", "done", "outputs", "error",
                 "created", "started", "finished", "cancel", "result", "log",
                 "srcPath", "destPath", "format", "sizeText", "inputs", "bucket",
                 "planned", "mode", "_lock", "_task", "_secrets")

    def __init__(self, kind: str, title: str, **extra):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.subtitle = extra.get("subtitle", "")
        self.status = "queued"
        self.percent = 0.0
        self.phase = "等待中"
        self.item = ""
        self.speed = 0.0
        self.eta = None
        self.total = int(extra.get("total", 0) or 0)
        self.done = 0
        self.outputs = []
        self.error = ""
        self.created = time.time()
        self.started = None
        self.finished = None
        self.cancel = threading.Event()
        self.result = {}
        self.log = ""
        self.srcPath = extra.get("srcPath", "")
        self.destPath = extra.get("destPath", "")
        self.bucket = extra.get("bucket", "")
        self.format = extra.get("format", "")
        # Where this job is expected to write (archive target / extract dest).
        # Kept in the crash journal so an interrupted run can name its output
        # without guessing from the (possibly missing) final outputs.
        self.planned = extra.get("planned", "")
        # fresh vs merge is decided by the running extract task; the UI reads it
        # to word cancel/retry hints with the real cleanup contract.
        self.mode = extra.get("mode", "")
        self.sizeText = ""
        # Kept so a finished compression can be repeated from its context menu
        # without the user re-picking every file.
        self.inputs = list(extra.get("inputs") or [])
        self._lock = threading.RLock()
        self._secrets = tuple(p for p in extra.get("secrets", ()) if p)

    def to_dict(self):
        with self._lock:
            return keyring.redact({
                "id": self.id, "kind": self.kind, "title": self.title,
                "subtitle": self.subtitle, "status": self.status,
                "percent": round(self.percent, 1), "phase": self.phase,
                "item": self.item, "speed": round(self.speed, 1),
                "eta": self.eta, "total": self.total, "done": self.done,
                "outputs": list(self.outputs), "error": self.error,
                "created": self.created, "started": self.started,
                "finished": self.finished, "srcPath": self.srcPath,
                "destPath": self.destPath, "format": self.format,
                "sizeText": self.sizeText, "result": self.result,
                "inputs": list(self.inputs),
                "mode": self.mode,
            }, self._secrets)

    def update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, keyring.redact(v, self._secrets))

    def progress(self, percent, item="", speed=0.0):
        with self._lock:
            if percent is not None:
                self.percent = float(percent)
                if self.total:
                    self.done = int(self.total * self.percent / 100.0)
            if item:
                self.item = keyring.redact(item, self._secrets)
            if speed:
                self.speed = speed
            if self.total and self.speed > 1 and self.percent < 100:
                remain = self.total * (1 - self.percent / 100.0)
                self.eta = int(remain / self.speed) if self.speed else None
            elif self.percent >= 99.9:
                self.eta = 0


class JobManager:
    def __init__(self, engine: eng.Engine, settings):
        self.engine = engine
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self._q: list[str] = []
        self._cv = threading.Condition()
        self._buckets: dict[str, str] = {}
        self._busy: set[str] = set()
        self._targets: dict[str, str] = {}
        # Set by shutdown(): after that point new submits are refused so a
        # quitting app cannot start work it will never let finish.
        self._closing = False
        self.history = util.read_json(util.state_path("history.json"), [])
        self.history = self.history[:40] if isinstance(self.history, list) else []
        # In-flight journal: one small JSON per queued/running job, removed when
        # the job lands on a terminal state.  On launch the leftovers can only
        # belong to a previous process that died before it could clean up, so
        # they are turned into visible "interrupted" records (never replayed).
        self._pending_dir = util.state_path("pending-jobs")
        self._pending_lock = threading.Lock()
        try:
            os.makedirs(self._pending_dir, exist_ok=True)
        except OSError:
            pass
        self.interrupted = self._recover_interrupted()
        # T06: 处理上次进程崩溃在“覆盖压缩切换”中途留下的 manifest。
        try:
            archtxn.recover_pending(engine)
        except Exception as exc:  # noqa: BLE001
            print("[arca] 恢复未完成的覆盖压缩失败: %r" % (exc,), file=sys.stderr)
        self._workers = [threading.Thread(target=self._loop, daemon=True)
                         for _ in range(2)]
        for worker in self._workers:
            worker.start()

    # ------------------------------------------------------------------ public
    def submit(self, job: Job, task) -> Job:
        with self._cv:
            if self._closing:
                raise RuntimeError("应用正在退出，不能提交新任务")
            directory = job.destPath if job.kind in ("compress", "extract") else os.path.dirname(job.srcPath)
            self._buckets[job.id] = os.path.realpath(job.bucket or directory)
            self.jobs[job.id] = job
            self.order.insert(0, job.id)
            self._q.append(job.id)
            job._task = task
            self._journal_write(job)
            self._cv.notify_all()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._cv:
            return self.jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._cv:
            return [self.jobs[i].to_dict() for i in self.order if i in self.jobs]

    def history_snapshot(self, limit=40):
        with self._cv:
            return list(self.history[:limit])

    def active_count(self) -> int:
        """Jobs that would be damaged by replacing the app or its engines."""
        with self._cv:
            return sum(1 for jid in self.order
                       if self.jobs.get(jid) and self.jobs[jid].to_dict()["status"]
                       in ("queued", "running"))

    def cancel(self, job_id: str) -> bool:
        """Ask a queued/running job to stop and terminate its child processes.

        A queued job has no child and no output, so it is marked cancelled
        right here instead of waiting for a worker.  For a running job this
        returns once its registered children were terminated/reaped; the
        worker finishes the task itself (and clears the journal) afterwards.
        Never blocks on the task body and never touches outputs.
        """
        with self._cv:
            job = self.jobs.get(job_id)
            if not job:
                return False
            status = job.to_dict()["status"]
            if status not in ("queued", "running"):
                return False
            job.cancel.set()
            if status == "queued":
                job.update(status="cancelled", phase="已取消",
                           finished=time.time(), error="已取消")
                self._q = [i for i in self._q if i != job_id]
                self._buckets.pop(job_id, None)
                self._targets.pop(job_id, None)
                self._forget_pending(job_id)
                self._cv.notify_all()
                return True
            job.update(phase="正在取消…")
            self._cv.notify_all()
        procs.terminate_job(job_id)   # outside the lock: bounded but blocking
        return True

    def remove(self, job_id: str) -> None:
        if self.cancel(job_id):
            return
        with self._cv:
            self.jobs.pop(job_id, None)
            if job_id in self.order:
                self.order.remove(job_id)

    def shutdown(self, timeout: float = 10.0) -> dict:
        """Cancel active jobs, reap their children and drain the workers.

        Called from the UI terminate path and from headless SIGTERM/SIGINT so
        both behave the same.  Bounded by *timeout*: whatever is still running
        afterwards is reported in ``unfinished`` instead of being claimed as
        stopped.  Never re-submits or replays jobs.
        """
        started = time.time()
        with self._cv:
            self._closing = True
            active = [jid for jid in self.order
                      if self.jobs.get(jid) and self.jobs[jid].to_dict()["status"]
                      in ("queued", "running")]
            # Queued jobs never started: they produce no files and no child, so
            # they can be finished right here without waiting for a worker.
            for jid in list(active):
                job = self.jobs.get(jid)
                if job is None or job.to_dict()["status"] != "queued":
                    continue
                job.cancel.set()
                job.update(status="cancelled", phase="已取消", finished=time.time(),
                           error="应用退出，任务已取消")
                self._q = [i for i in self._q if i != jid]
                self._buckets.pop(jid, None)
                self._targets.pop(jid, None)
                self._forget_pending(jid)
                active.remove(jid)
            for jid in active:
                job = self.jobs.get(jid)
                if job is not None:
                    job.cancel.set()
                    job.update(phase="应用退出，正在取消…")
            self._cv.notify_all()
        cancelled = list(active)
        # Outside the lock: terminate children first so blocked reads unwind.
        for jid in active:
            procs.terminate_job(jid)
        deadline = started + max(0.5, float(timeout))
        with self._cv:
            while True:
                remaining = [jid for jid in self.order
                             if self.jobs.get(jid) and self.jobs[jid].to_dict()["status"]
                             in ("queued", "running")]
                if not remaining or time.time() >= deadline:
                    break
                self._cv.wait(min(0.1, max(0.01, deadline - time.time())))
            unfinished = []
            for jid in remaining:
                job = self.jobs.get(jid)
                if job is None:
                    continue
                unfinished.append({"id": jid, "kind": job.kind,
                                   "status": job.to_dict()["status"]})
        return {"ok": not unfinished, "cancelled": cancelled,
                "unfinished": unfinished,
                "seconds": round(time.time() - started, 3)}

    def clear_finished(self) -> None:
        with self._cv:
            for jid in list(self.order):
                job = self.jobs.get(jid)
                if job and job.to_dict()["status"] not in ("queued", "running"):
                    self.jobs.pop(jid, None)
                    self.order.remove(jid)
            # The panel shows finished jobs and, when none are left, the persisted
            # history.  Clearing only the in-memory list left that history behind,
            # so the button appeared to do nothing.
            had_history = bool(self.history)
            self.history = []
        if had_history:
            util.write_json(util.state_path("history.json"), [])

    # ------------------------------------------------------- crash journal
    def _journal_path(self, job_id: str) -> str:
        return os.path.join(self._pending_dir, f"{job_id}.json")

    def _journal_write(self, job: Job, **extra) -> None:
        """Persist just enough to identify this task after an unclean exit.

        Deliberately excludes passwords, request bodies and error text: the
        journal is a local ownership marker, not a replay log.  Best-effort --
        a read-only state dir must never stop a real job from running.
        """
        record = {
            "id": job.id, "kind": job.kind, "title": job.title,
            "subtitle": job.subtitle, "pid": os.getpid(),
            "created": job.created, "srcPath": job.srcPath,
            "destPath": job.destPath, "planned": job.planned,
            "format": job.format, "total": job.total,
            "bucket": job.bucket,
        }
        record.update(extra)
        with self._pending_lock:
            util.write_json(self._journal_path(job.id), record)

    def journal_update(self, job: Job, **extra) -> None:
        """Add facts only the running task knows (e.g. fresh vs merge dest)."""
        with self._pending_lock:
            path = self._journal_path(job.id)
            record = util.read_json(path, None)
            if isinstance(record, dict):
                record.update(extra)
                util.write_json(path, record)

    def _forget_pending(self, job_id: str) -> None:
        with self._pending_lock:
            # The build volume materialises xattrs as AppleDouble sidecars, so
            # removing only the JSON would leave one 4 KB "._<id>.json" per job.
            for name in (f"{job_id}.json", f"._{job_id}.json"):
                try:
                    os.remove(os.path.join(self._pending_dir, name))
                except OSError:
                    pass

    def pending_count(self) -> int:
        try:
            return len([name for name in os.listdir(self._pending_dir)
                        if name.endswith(".json") and not name.startswith(".")])
        except OSError:
            return 0

    def _recover_interrupted(self) -> list[dict]:
        """Turn journal leftovers into visible history records, once.

        A record whose owning pid is still alive is left alone: a second
        instance must not declare another instance's live job interrupted.
        Nothing on disk is deleted here -- the outputs are user-visible and
        their completeness cannot be judged safely, so the record only states
        what happened and where to look.
        """
        records = []
        my_pid = os.getpid()
        with self._pending_lock:
            try:
                names = sorted(os.listdir(self._pending_dir))
            except OSError:
                return []
            for name in names:
                if name.startswith(".") or not name.endswith(".json"):
                    continue
                path = os.path.join(self._pending_dir, name)
                record = util.read_json(path, None)
                if isinstance(record, dict) and record.get("id"):
                    pid = record.get("pid")
                    if isinstance(pid, int) and pid != my_pid and _pid_alive(pid):
                        continue          # owned by a live instance: keep
                try:
                    os.remove(path)       # consumed even if unusable
                    os.remove(os.path.join(self._pending_dir,
                                           "." + os.path.basename(path)))
                except OSError:
                    pass
                if isinstance(record, dict) and record.get("id"):
                    records.append(record)
            # Drop sidecars orphaned by an older crash (no matching JSON left).
            for name in names:
                if not name.startswith("._") or not name.endswith(".json"):
                    continue
                if not os.path.exists(os.path.join(self._pending_dir, name[2:])):
                    try:
                        os.remove(os.path.join(self._pending_dir, name))
                    except OSError:
                        pass
        if not records:
            return []
        recovered = []
        with self._cv:
            known = {item.get("id") for item in self.history
                     if isinstance(item, dict)}
            now = time.time()
            for record in sorted(records, key=lambda r: r.get("created") or 0):
                job_id = record["id"]
                if job_id in known:
                    continue              # it actually finished before the crash
                mode = record.get("mode") or ""
                item = {
                    "id": job_id,
                    "kind": record.get("kind") or "extract",
                    "title": record.get("title") or "未完成任务",
                    "subtitle": record.get("subtitle") or "",
                    "finished": record.get("created") or now,
                    "outputs": [record["planned"]] if record.get("planned") else [],
                    "srcPath": record.get("srcPath") or "",
                    "destPath": record.get("destPath") or "",
                    "format": record.get("format") or "",
                    "sizeText": "",
                    "status": "interrupted",
                    "result": {"interrupted": True, "mode": mode,
                               "expected": int(record.get("total") or 0)},
                    "note": _interrupted_note(mode),
                    "recoveredAt": now,
                }
                self.history.insert(0, item)
                recovered.append(item)
            if recovered:
                self.history = self.history[:40]
                util.write_json(util.state_path("history.json"), self.history)
        return recovered

    # ------------------------------------------------------------------ worker
    def _loop(self):
        while True:
            with self._cv:
                while True:
                    jid = next((i for i in self._q if self._buckets[i] not in self._busy), None)
                    if jid is not None:
                        break
                    self._cv.wait()
                self._q.remove(jid)
                job = self.jobs[jid]
                bucket = self._buckets[jid]
                if job.cancel.is_set():
                    job.update(status="cancelled", phase="已取消", finished=time.time())
                    self._buckets.pop(jid)
                    self._targets.pop(jid, None)
                    self._forget_pending(jid)
                    self._cv.notify_all()
                    continue
                self._busy.add(bucket)
                job.update(status="running", started=time.time(), phase="准备中")
            procs.bind(jid)
            try:
                job._task(job)  # noqa: SLF001
                if job.cancel.is_set():
                    job.update(status="cancelled", phase="已取消",
                               finished=time.time(), error="已取消")
                else:
                    # item is the file being worked on: it must not linger on a
                    # finished card (the subtitle falls back to the summary).
                    job.update(status="done", percent=100.0, phase="完成",
                               finished=time.time(), eta=0, item="")
            except Exception as exc:  # noqa: BLE001
                if job.cancel.is_set():
                    job.update(status="cancelled", phase="已取消", error="已取消",
                               finished=time.time())
                else:
                    msg = util.friendly_error(exc) or str(exc) or exc.__class__.__name__
                    job.update(status="error", phase="失败", error=msg,
                               finished=time.time())
            finally:
                try:
                    self._record(job)
                finally:
                    procs.unbind()
                    procs.forget(jid)
                    self._forget_pending(jid)
                    with self._cv:
                        self._busy.remove(bucket)
                        self._buckets.pop(jid, None)
                        self._targets.pop(jid, None)
                        self._cv.notify_all()

    def _record(self, job: Job):
        snapshot = job.to_dict()
        if snapshot["status"] == "done":
            item = {key: snapshot[key] for key in (
                "id", "kind", "title", "subtitle", "finished", "outputs",
                "srcPath", "destPath", "format", "sizeText", "status", "result")}
            with self._cv:
                self.history.insert(0, item)
                self.history = self.history[:40]
                util.write_json(util.state_path("history.json"), self.history)
            if not self.settings.get("suppressSound", False):
                util.beep()


# ------------------------------------------------------------------ task makers
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _interrupted_note(mode: str) -> str:
    """One plain sentence per ownership case; never claims more than we know."""
    if mode == "merge":
        return ("上次任务在应用退出时中断：合并目标里可能留下本次新增的未完成文件，"
                "产物可能不完整；既有文件没有被主动删除，但被覆盖过的同名旧文件能否"
                "恢复不作保证。")
    if mode == "fresh":
        return ("上次任务在应用退出时中断：目标目录里的文件可能没写完或缺少条目"
                "（产物可能不完整），应用没有删除它们；确认不需要后可手动清理。")
    return ("上次任务在应用退出时中断：输出可能不完整，应用没有删除任何文件，"
            "请检查任务对应的输出位置。")


def make_compress_task(mgr: JobManager, opts: dict):
    def task(job: Job):
        inputs = [p for p in opts["inputs"] if os.path.exists(p)]
        if not inputs:
            raise eng.EngineError("没有可压缩的文件")
        job.update(phase="正在扫描", item="", total=0, done=0, percent=0,
                   speed=0, eta=None)
        last_count = 0
        last_update = time.monotonic()

        def scanned(count, size):
            nonlocal last_count, last_update
            now = time.monotonic()
            if count - last_count >= 128 or now - last_update >= .2:
                job.update(item=f"已扫描 {count} 个文件 · 约 {util.human_size(size)}",
                           phase="正在扫描", total=0, percent=0)
                last_count, last_update = count, now

        try:
            _, total = util.scan_inputs(inputs, on_progress=scanned, cancel=job.cancel)
        except OSError:
            total = 0
        finally:
            job.update(item="")
        if job.cancel.is_set():
            raise RuntimeError("已取消")
        job.update(total=total, done=0, percent=0, phase="正在压缩")
        base = os.path.commonpath([os.path.dirname(os.path.abspath(p)) for p in inputs]) \
            if len(inputs) > 1 else os.path.dirname(os.path.abspath(inputs[0]))
        if base == "/":
            base = os.path.expanduser("~")
        rel = [os.path.relpath(os.path.abspath(p), base) for p in inputs]
        cmd_opts = dict(opts)
        cmd_opts["relative"] = rel

        if opts["format"] == "tar.zst":
            return _compress_zstd(job, mgr, opts, inputs, base, total)

        cmd = mgr.engine.build_add_cmd(cmd_opts, base)

        def on_progress(percent, line, speed):
            if percent is None:
                if "Scan" in line:
                    job.update(phase="正在扫描")
                return
            if job.phase == "正在扫描":
                # The scan line only appears at the start; once a real percent
                # arrives the job is compressing again.
                job.update(phase="正在压缩")
            m = re.search(r"\d+\s+\+\s+(.+)$", line)
            job.progress(percent, m.group(1) if m else "", speed)

        code, log = mgr.engine.run(cmd, cwd=base, on_progress=on_progress,
                                   cancel=job.cancel, total_bytes=total)
        job.update(log=log[-4000:])
        cancelled = job.cancel.is_set()
        if cancelled:
            _cleanup_outputs(opts, job.started or job.created)
            raise RuntimeError("已取消")
        if code != 0 and code != 1:
            # A crashed or killed 7zz leaves a half-written archive behind; it
            # cannot be opened, so it is never worth keeping.
            _cleanup_outputs(opts, job.started or job.created)
            raise eng.EngineError(eng.describe_error(
                log, code, format=opts["format"],
                has_password=bool(opts.get("password"))) or "压缩失败")

        entry = mgr.engine.resolve(opts["output"])
        files = mgr.engine.parts(entry) or [entry]
        files = [f for f in files if os.path.exists(f)]
        warning = eng.describe_error(log, code) if code == 1 else ""
        if _txn_commit(job, mgr, opts, total, files, warning):
            return
        _finish_compress(job, mgr, opts, total, files, entry, warning)
    return task


def _compress_zstd(job: Job, mgr: JobManager, opts: dict, inputs: list[str],
                   base: str, total: int):
    """tar + zstd, streamed; no password and no volumes by design."""
    job.update(phase="正在压缩（zstd）")
    seen = {"files": 0}

    def on_progress(pct, name, speed):
        job.progress(pct, name, speed)

    started = job.started or job.created
    try:
        out = zst.compress(inputs, base, opts["output"], level=opts.get("level", 3),
                           exclude_meta=bool(opts.get("excludeMeta", True)),
                           threads=int(opts.get("threads") or 0),
                           on_progress=on_progress, cancel=job.cancel)
    except zst.ZstdError as exc:
        if str(exc) == "已取消":
            _cleanup_outputs(opts, started)
            raise RuntimeError("已取消")
        raise eng.EngineError(str(exc))
    seen["files"] = out.get("files", 0)
    if job.cancel.is_set():
        _cleanup_outputs(opts, started)
        raise RuntimeError("已取消")
    entry = os.path.abspath(opts["output"])
    if not os.path.exists(entry):
        raise eng.EngineError("压缩没有生成文件")
    job.update(log=f"zstd {mgr.engine.zstd_version}: {seen['files']} 个文件, "
                   f"{out.get('seconds', 0):.1f}s")
    if _txn_commit(job, mgr, opts, total, [entry], ""):
        job.update(result={**job.result, "zstd": mgr.engine.zstd_version,
                           "files": seen["files"]})
        return
    _finish_compress(job, mgr, opts, total, [entry], entry, "")
    job.update(result={**job.result, "zstd": mgr.engine.zstd_version,
                       "files": seen["files"]})


def _finish_compress(job: Job, mgr: JobManager, opts: dict, total: int,
                     files: list[str], entry: str, warning: str,
                     skip_par2: bool = False):
    """Record the archive, then add recovery data when the user asked for it."""
    size = sum(os.path.getsize(f) for f in files)
    job.update(outputs=files, destPath=os.path.dirname(entry),
               result={
                   "archive": entry, "parts": len(files), "size": size,
                   "sizeText": util.human_size(size), "total": total,
                   "ratio": round(total / size, 2) if size else 0,
                   "warning": warning,
               })
    job.update(sizeText=util.human_size(size))
    if job.total and job.percent < 99:
        job.progress(100)
    if skip_par2:
        return

    percent = int(opts.get("par2Percent") or 0)
    if percent <= 0 or not mgr.engine.par2:
        return
    single = bool(opts.get("par2Single"))
    job.update(phase="正在生成恢复记录（PAR2）")
    try:
        rec = par2_mod.create(
            mgr.engine.par2, files, percent, single=single,
            on_progress=lambda p, m: job.progress(None, m),
            cancel=job.cancel)
    except par2_mod.Par2Error as exc:
        if str(exc) == "已取消":
            raise RuntimeError("已取消")
        # The archive itself is fine -- a failed recovery record must not turn
        # a successful compression into an error.
        job.update(result={**job.result, "par2Error": str(exc)})
        return
    job.update(outputs=files + rec["made"],
               result={**job.result, "par2": {
                   "files": rec["files"], "bytes": rec["bytes"],
                   "percent": percent, "sizeText": util.human_size(rec["bytes"]),
                   "single": single,
                   "index": rec["made"][0] if rec["made"] else "",
               }})


def _txn_commit(job: Job, mgr: JobManager, opts: dict, total: int,
                files: list[str], warning: str) -> bool:
    """T06：覆盖压缩先回读校验，再以同卷 rename 把 staging 换成正式名。"""
    txn = opts.get("_txn")
    if txn is None:
        return False
    if not files:
        archtxn.abort(txn)
        raise eng.EngineError("压缩没有生成文件")
    job.update(phase="正在校验新归档")
    try:
        archtxn.verify(mgr.engine, files, opts.get("password") or "",
                      opts.get("format") or "")
    except Exception:
        archtxn.abort(txn)
        raise
    if job.cancel.is_set():
        archtxn.abort(txn)
        raise RuntimeError("已取消")
    job.update(phase="正在替换旧归档")
    try:
        committed = archtxn.commit(txn, mgr.engine, files, cancel=job.cancel)
    except RuntimeError:
        archtxn.abort(txn)
        raise
    except Exception:
        archtxn.abort(txn)
        raise
    _finish_compress(job, mgr, opts, total, committed["files"],
                     txn.target, warning, skip_par2=True)
    extra = {}
    if committed.get("par2"):
        extra["par2"] = committed["par2"]
        made = committed["par2"].get("made") or []
        if made:
            job.update(outputs=committed["files"] + made)
    if committed.get("par2Error"):
        extra["par2Error"] = committed["par2Error"]
    if extra:
        job.update(result={**job.result, **extra})
    return True


def make_extract_task(mgr: JobManager, opts: dict):
    def task(job: Job):
        entry = mgr.engine.resolve(opts["archive"])
        dest = opts["dest"]
        entries = opts.get("entries") or None
        fresh = not os.path.isdir(dest)          # did we create this folder?
        job.update(mode="fresh" if fresh else "merge")
        # Record ownership semantics before any write: after a crash the next
        # launch can say whether existing files were supposed to be preserved.
        mgr.journal_update(job, mode="fresh" if fresh else "merge")
        # Merge destinations cannot be rmtree'd on rollback: pre-existing files
        # and other tasks' outputs must survive.  Snapshot the tree first, then
        # delete only paths that appeared during this job (and empty directories
        # this job created).  Buckets serialise jobs sharing a dest_root, so the
        # diff is attributable to this task; external writers are out of scope.
        before = None if fresh else _snapshot_tree(dest)
        os.makedirs(dest, exist_ok=True)

        def guarded(run):
            # 失败与取消同策：fresh 删本次新建目录；merge 只清本次新增文件与
            # 新建空目录，既有文件/其他任务产物保留。
            try:
                return run()
            except Exception:
                _rollback_extract(dest, fresh, before)
                raise

        if zst.is_zstd(entry):
            if entries:
                # Server validates this before submitting; keep a defensive
                # error so a direct job submission can never silently fall
                # back to extracting the whole archive.
                raise eng.EngineError("该格式不支持只解压选中项")
            return guarded(lambda: _extract_zstd(job, mgr, entry, dest, opts))

        # A Windows-made ZIP stores GBK names without the UTF-8 flag; 7-Zip
        # turns them into private-use characters.  Decode the directory
        # ourselves in that one case (see zipfix for the guards).
        plan = None
        if not opts.get("password"):
            try:
                plan = zipfix.plan(entry)
            except OSError:
                plan = None
        if plan:
            return guarded(lambda: _extract_zipfix(job, entry, dest, opts, plan,
                                                   fresh, entries))

        def run_engine():
            # T04a: 路径/链接预检放在任务开始（而不是路由），重试路径同样生效；
            # 失败时 guarded() 按 T00 回滚（fresh 删本次目录，merge 只清新增）。
            mgr.engine.precheck_extract(entry, opts.get("password") or "")
            cmd = mgr.engine.build_extract_cmd({**opts, "archive": entry})
            total = int(opts.get("totalBytes") or 0)
            before_files = len(before[0]) if before else 0
            job.update(total=total, phase="正在解压")

            def on_progress(percent, line, speed):
                if percent is None:
                    return
                m = re.search(r"-\s+(.+)$", line)
                job.progress(percent, m.group(1) if m else "", speed)

            # If run() raises (read/callback/OSError), it has already reaped the
            # child; guarded() below then applies the same rollback contract as
            # the zstd/zipfix paths.
            code, log = mgr.engine.run(cmd, cwd=None, on_progress=on_progress,
                                       cancel=job.cancel, total_bytes=total)
            job.update(log=log[-4000:])
            if job.cancel.is_set():
                raise RuntimeError("已取消")
            if code not in (0, 1):
                raise eng.EngineError(eng.describe_error(log, code) or "解压失败")
            written = max(0, _count_files(dest) - before_files)
            job.update(outputs=[dest], destPath=dest, srcPath=entry,
                       result={"dest": dest, "files": written,
                               **({"partial": len(entries)} if entries else {}),
                               "warning": eng.describe_error(log, code) if code == 1 else ""},
                       sizeText=util.human_size(total))
            job.progress(100)

        return guarded(run_engine)
    return task


def _count_files(root: str) -> int:
    return sum(len(f) for _r, _d, f in os.walk(root))


def _snapshot_tree(root: str) -> tuple[set, set]:
    """Relative file/dir inventory used to attribute merge-mode rollback."""
    files, dirs = set(), set()
    for base, names, filenames in os.walk(root):
        rel_base = os.path.relpath(base, root)
        for name in list(names):
            full = os.path.join(base, name)
            if os.path.islink(full):
                # A symlink to a directory is walked as a dir name; treat it
                # as a file so rollback unlinks it instead of rmdir'ing.
                names.remove(name)
                filenames.append(name)
                continue
            dirs.add(name if rel_base == "." else os.path.join(rel_base, name))
        for name in filenames:
            files.add(name if rel_base == "." else os.path.join(rel_base, name))
    return files, dirs


def _rollback_extract(dest: str, fresh: bool, before=None) -> None:
    """Roll back only what this job can claim.

    fresh: the directory itself was created by this job -> remove all of it.
    merge: keep every pre-existing path (and other tasks' outputs); delete only
    files/dirs that appeared after `before` was taken.  A file overwritten by
    the engine cannot be restored here and is deliberately left alone rather
    than guessed at by mtime/inode heuristics.
    """
    if fresh:
        import shutil as _sh
        _sh.rmtree(dest, ignore_errors=True)
        return
    if before is None:
        return
    old_files, old_dirs = before
    new_files, new_dirs = _snapshot_tree(dest)
    for rel in sorted(new_files - old_files, reverse=True):
        try:
            os.unlink(os.path.join(dest, rel))
        except OSError:
            pass
    for rel in sorted(new_dirs - old_dirs, key=lambda p: p.count(os.sep),
                      reverse=True):
        full = os.path.join(dest, rel)
        try:
            if os.path.islink(full):
                os.unlink(full)
            else:
                os.rmdir(full)
        except OSError:
            pass


def _extract_zipfix(job: Job, entry: str, dest: str, opts: dict,
                    plan: dict, fresh: bool, entries=None):
    only = None
    total = plan.get("bytes") or 0
    if entries:
        only = {str(name).rstrip("/") for name in entries}

        def selected(name):
            norm = name.rstrip("/")
            return any(norm == sel or norm.startswith(sel + "/") for sel in only)

        total = sum(e["size"] for e in plan["entries"]
                    if not e["isDir"] and selected(e["path"])) or 0
    job.update(total=total, phase="正在解压")

    def on_progress(pct, name):
        job.progress(pct, name)

    try:
        out = zipfix.extract(entry, dest, plan["names"],
                             overwrite=opts.get("overwrite") or "rename",
                             on_progress=on_progress, cancel=job.cancel,
                             only=only)
    except zipfix.ZipFixError as exc:
        if str(exc) == "已取消":
            raise RuntimeError("已取消")
        raise eng.EngineError(str(exc))
    if job.cancel.is_set():
        raise RuntimeError("已取消")
    job.update(outputs=[dest], destPath=dest, srcPath=entry,
               result={"dest": dest, "files": out["files"],
                       "bytes": out["bytes"],
                       "skipped": out.get("skipped", 0),
                       **({"partial": len(entries)} if entries else {}),
                       "renamed": plan.get("fixed"),
                       "encoding": plan.get("encoding")},
               sizeText=util.human_size(out["bytes"] or job.total))
    job.progress(100)


def make_test_task(mgr: JobManager, archive: str, password: str = ""):
    def task(job: Job):
        entry = mgr.engine.resolve(archive)
        total = os.path.getsize(entry) if os.path.exists(entry) else 0
        job.update(total=total, phase="正在校验")
        if zst.is_zstd(entry):
            return _test_zstd(job, mgr, entry, total)
        cmd = mgr.engine.build_test_cmd(entry, password)

        def on_progress(percent, line, speed):
            if percent is None:
                return
            job.progress(percent, "", speed)

        code, log = mgr.engine.run(cmd, on_progress=on_progress, cancel=job.cancel,
                                   total_bytes=total)
        job.update(log=log[-4000:], srcPath=entry)
        if job.cancel.is_set():
            raise RuntimeError("已取消")
        if code != 0:
            raise eng.EngineError(eng.describe_error(log, code) or "校验未通过")
        job.update(result={"ok": True, "detail": "所有文件完好"},
                   phase="校验通过")
    return task


def _extract_zstd(job: Job, mgr: JobManager, entry: str, dest: str, opts: dict):
    total = int(opts.get("totalBytes") or 0)
    job.update(total=total, phase="正在解压（zstd）")

    def on_progress(pct, name, speed):
        job.progress(pct, name, speed)

    try:
        out = zst.extract(entry, dest, overwrite=opts.get("overwrite") or "rename",
                          on_progress=on_progress, cancel=job.cancel)
    except zst.ZstdError as exc:
        if str(exc) == "已取消":
            raise RuntimeError("已取消")
        raise eng.EngineError(str(exc))
    if job.cancel.is_set():
        raise RuntimeError("已取消")
    job.update(outputs=[dest], destPath=dest, srcPath=entry,
               result={"dest": dest, "files": out.get("files", 0),
                       "bytes": out.get("bytes", 0),
                       "skipped": out.get("skipped", 0),
                       "zstd": mgr.engine.zstd_version},
               sizeText=util.human_size(out.get("bytes", 0) or total))
    job.progress(100)


def _test_zstd(job: Job, mgr: JobManager, entry: str, total: int):
    binary = mgr.engine.zstd
    if not binary:
        raise eng.EngineError("未找到 zstd 程序")
    import subprocess
    p = subprocess.run([binary, "-t", entry], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=3600)
    log = p.stdout.decode("utf-8", "replace")
    job.update(log=log[-4000:], srcPath=entry)
    if p.returncode != 0:
        raise eng.EngineError("校验未通过：文件已损坏")
    job.update(result={"ok": True, "detail": "zstd 数据校验通过"}, phase="校验通过")
    job.progress(100)


def make_par2_task(mgr: JobManager, archive: str, action: str = "verify"):
    """Verify or repair the recovery records that sit next to `archive`."""
    def task(job: Job):
        binary = mgr.engine.par2
        if not binary:
            raise eng.EngineError("未找到 par2 程序，无法使用恢复记录")
        files = par2_mod.recovery_files(archive)
        index = next((f for f in files if ".vol" not in os.path.basename(f)), None)
        if not index:
            raise eng.EngineError("这个压缩包没有恢复记录")
        job.update(phase="正在校验恢复记录", srcPath=archive)
        if action == "repair":
            res = par2_mod.repair(binary, index,
                                  on_progress=lambda p, m: job.progress(None, m),
                                  cancel=job.cancel)
            if job.cancel.is_set():
                raise RuntimeError("已取消")
            job.update(log=res["log"])
            if not res["ok"]:
                raise eng.EngineError(_par2_reason(res["log"]) or "修复未能完成")
            job.update(result={"ok": True, "repaired": True}, phase="修复完成")
        else:
            res = par2_mod.verify(binary, index,
                                  on_progress=lambda p, m: job.progress(None, m),
                                  cancel=job.cancel)
            if job.cancel.is_set():
                raise RuntimeError("已取消")
            job.update(log=res["log"])
            job.update(result={"ok": res["ok"], "damaged": res["damaged"],
                               "missing": res["missing"],
                               "damagedCount": res["damagedCount"],
                               "missingCount": res["missingCount"],
                               "repairable": res["repairable"],
                               "recoveryFiles": len(files)},
                       phase="校验通过" if res["ok"] else "发现损坏")
            if not res["ok"] and not res["repairable"]:
                raise eng.EngineError(_par2_reason(res["log"]) or "文件已损坏且无法修复")
        job.progress(100)
    return task


def _par2_reason(log: str) -> str:
    low = (log or "").lower()
    if "repair is not possible" in low:
        return "损坏超出恢复记录的修复能力"
    if "missing" in low:
        return "缺少文件，且恢复记录不足"
    return ""


def _cleanup_outputs(opts, after_ts: float | None = None):
    """Remove half-written volumes after a cancelled job."""
    txn = opts.get("_txn")
    if txn is not None:
        try:
            archtxn.abort(txn)
        except Exception:  # noqa: BLE001
            pass
    try:
        entry = opts["output"]
        base = entry[:-4] if re.search(r"\.\d{3}$", entry) else entry
        import glob as _g
        for f in _g.glob(_g.escape(base) + "*"):
            if not (f == entry or re.search(r"\.\d{3}$", f)):
                continue
            try:
                if after_ts and os.path.getmtime(f) < after_ts:
                    continue
                os.remove(f)
            except OSError:
                pass
    except Exception:
        pass
