"""Background job queue: two directory-isolated workers, progress + history."""
from __future__ import annotations

import os
import re
import threading
import time
import uuid

from . import engine as eng
from . import par2 as par2_mod
from . import util
from . import keyring
from . import zipfix
from . import zst


class Job:
    __slots__ = ("id", "kind", "title", "subtitle", "status", "percent", "phase",
                 "item", "speed", "eta", "total", "done", "outputs", "error",
                 "created", "started", "finished", "cancel", "result", "log",
                 "srcPath", "destPath", "format", "sizeText", "inputs", "bucket",
                 "_lock", "_task", "_secrets")

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
        self.history = util.read_json(util.state_path("history.json"), [])
        self.history = self.history[:40] if isinstance(self.history, list) else []
        self._workers = [threading.Thread(target=self._loop, daemon=True)
                         for _ in range(2)]
        for worker in self._workers:
            worker.start()

    # ------------------------------------------------------------------ public
    def submit(self, job: Job, task) -> Job:
        with self._cv:
            directory = job.destPath if job.kind in ("compress", "extract") else os.path.dirname(job.srcPath)
            self._buckets[job.id] = os.path.realpath(job.bucket or directory)
            self.jobs[job.id] = job
            self.order.insert(0, job.id)
            self._q.append(job.id)
            job._task = task
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
        return sum(j["status"] in ("queued", "running") for j in self.list())

    def cancel(self, job_id: str) -> bool:
        with self._cv:
            job = self.jobs.get(job_id)
            if job and job.to_dict()["status"] in ("queued", "running"):
                job.cancel.set()
                job.update(phase="正在取消…")
                self._cv.notify_all()
                return True
            return False

    def remove(self, job_id: str) -> None:
        with self._cv:
            if self.cancel(job_id):
                return
            self.jobs.pop(job_id, None)
            if job_id in self.order:
                self.order.remove(job_id)

    def active_count(self) -> int:
        """Jobs that would be damaged by replacing the app or its engines."""
        with self._cv:
            return sum(1 for jid in self.order
                       if self.jobs.get(jid) and self.jobs[jid].to_dict()["status"]
                       in ("queued", "running"))

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
                    self._cv.notify_all()
                    continue
                self._busy.add(bucket)
                job.update(status="running", started=time.time(), phase="准备中")
            try:
                job._task(job)  # noqa: SLF001
                if job.cancel.is_set():
                    job.update(status="cancelled", phase="已取消",
                               finished=time.time(), error="已取消")
                else:
                    job.update(status="done", percent=100.0, phase="完成",
                               finished=time.time(), eta=0)
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
            raise eng.EngineError(eng.describe_error(
                log, code, format=opts["format"],
                has_password=bool(opts.get("password"))) or "压缩失败")

        entry = mgr.engine.resolve(opts["output"])
        files = mgr.engine.parts(entry) or [entry]
        files = [f for f in files if os.path.exists(f)]
        _finish_compress(job, mgr, opts, total, files, entry,
                         eng.describe_error(log, code) if code == 1 else "")
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
    _finish_compress(job, mgr, opts, total, [entry], entry, "")
    job.update(result={**job.result, "zstd": mgr.engine.zstd_version,
                       "files": seen["files"]})


def _finish_compress(job: Job, mgr: JobManager, opts: dict, total: int,
                     files: list[str], entry: str, warning: str):
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


def make_extract_task(mgr: JobManager, opts: dict):
    def task(job: Job):
        entry = mgr.engine.resolve(opts["archive"])
        dest = opts["dest"]
        fresh = not os.path.isdir(dest)          # did we create this folder?
        os.makedirs(dest, exist_ok=True)
        if zst.is_zstd(entry):
            return _extract_zstd(job, mgr, entry, dest, opts)

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
            return _extract_zipfix(job, entry, dest, opts, plan, fresh)

        cmd = mgr.engine.build_extract_cmd({**opts, "archive": entry})
        total = int(opts.get("totalBytes") or 0)
        before = _count_files(dest) if not fresh else 0
        job.update(total=total, phase="正在解压")

        def on_progress(percent, line, speed):
            if percent is None:
                return
            m = re.search(r"-\s+(.+)$", line)
            job.progress(percent, m.group(1) if m else "", speed)

        code, log = mgr.engine.run(cmd, cwd=None, on_progress=on_progress,
                                   cancel=job.cancel, total_bytes=total)
        job.update(log=log[-4000:])
        if job.cancel.is_set():
            _rollback_extract(dest, fresh)
            raise RuntimeError("已取消")
        if code not in (0, 1):
            raise eng.EngineError(eng.describe_error(log, code) or "解压失败")
        written = max(0, _count_files(dest) - before)
        job.update(outputs=[dest], destPath=dest, srcPath=entry,
                   result={"dest": dest, "files": written,
                           "warning": eng.describe_error(log, code) if code == 1 else ""},
                   sizeText=util.human_size(total))
        job.progress(100)
    return task


def _count_files(root: str) -> int:
    return sum(len(f) for _r, _d, f in os.walk(root))


def _rollback_extract(dest: str, fresh: bool) -> None:
    """Cancelling should not leave half an extraction folder behind.

    Only a folder this job created is removed: merging into an existing one
    cannot tell our files from the user's.
    """
    if fresh:
        import shutil as _sh
        _sh.rmtree(dest, ignore_errors=True)


def _extract_zipfix(job: Job, entry: str, dest: str, opts: dict,
                    plan: dict, fresh: bool):
    job.update(total=plan.get("bytes") or 0, phase="正在解压")

    def on_progress(pct, name):
        job.progress(pct, name)

    try:
        out = zipfix.extract(entry, dest, plan["names"],
                             overwrite=opts.get("overwrite") or "rename",
                             on_progress=on_progress, cancel=job.cancel)
    except zipfix.ZipFixError as exc:
        if str(exc) == "已取消":
            _rollback_extract(dest, fresh)
            raise RuntimeError("已取消")
        raise eng.EngineError(str(exc))
    if job.cancel.is_set():
        _rollback_extract(dest, fresh)
        raise RuntimeError("已取消")
    job.update(outputs=[dest], destPath=dest, srcPath=entry,
               result={"dest": dest, "files": out["files"],
                       "bytes": out["bytes"],
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
                       "bytes": out.get("bytes", 0), "zstd": mgr.engine.zstd_version},
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
