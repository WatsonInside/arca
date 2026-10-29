"""T01 regression: per-job child registry and graceful shutdown.

These tests do not need 7zz/par2/zstd: they spawn ordinary Python children and
register them exactly like engine.run()/par2._run()/zst do.  That keeps the
cancel-latency and reaping contract deterministic and fast.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from arca import jobs, procs, util  # noqa: E402


class _Settings:
    def get(self, key, default=None):
        return default


def _wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class ShutdownTest(unittest.TestCase):
    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self.home = tempfile.mkdtemp(prefix="arca-shutdown-home-")
        os.environ["HOME"] = self.home
        self.owned = []
        self.manager = jobs.JobManager(object(), _Settings())

    def tearDown(self):
        try:
            self.manager.shutdown(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        for proc in self.owned:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self.home, ignore_errors=True)

    # ------------------------------------------------------------- helpers
    def _spawn(self, ignore_term=False):
        if ignore_term:
            code = ("import signal,time;"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                    "time.sleep(30)")
        else:
            code = "import time;time.sleep(30)"
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.owned.append(proc)
        return proc

    def _submit_child_job(self, bucket, ignore_term=False, on_task=None):
        started = threading.Event()
        holder = {}

        def task(job):
            proc = self._spawn(ignore_term)
            procs.register(proc)      # what engine.run / par2._run / zst do
            holder["proc"] = proc
            started.set()
            if on_task is not None:
                on_task(job, proc)
            else:
                proc.wait()

        tmp = tempfile.mkdtemp(prefix="dest-", dir=self.home)
        job = jobs.Job("compress", "测试", srcPath=tmp, destPath=tmp,
                       bucket=bucket or tmp)
        self.manager.submit(job, task)
        self.assertTrue(started.wait(8), "任务未在预期时间内启动")
        return job, holder["proc"]

    def _status(self, job_id):
        job = self.manager.get(job_id)
        return job.to_dict()["status"] if job else None

    # --------------------------------------------------------------- tests
    def test_cancel_running_job_terminates_and_reaps_child(self):
        job, proc = self._submit_child_job(None)
        self.assertTrue(_alive(proc.pid))
        started = time.time()
        self.assertTrue(self.manager.cancel(job.id))
        elapsed = time.time() - started
        self.assertLess(elapsed, 4.0, f"取消耗时 {elapsed:.2f}s")
        self.assertTrue(_wait_for(lambda: self._status(job.id) == "cancelled"))
        self.assertIsNotNone(proc.poll(), "子进程没有被回收")
        self.assertFalse(_alive(proc.pid))
        self.assertTrue(_wait_for(lambda: not os.path.exists(
            os.path.join(util.state_path("pending-jobs"), job.id + ".json"))))

    def test_cancel_escalates_to_kill_for_silent_child(self):
        job, proc = self._submit_child_job(None, ignore_term=True)
        started = time.time()
        self.assertTrue(self.manager.cancel(job.id))
        elapsed = time.time() - started
        self.assertLess(elapsed, 8.0, f"忽略 SIGTERM 的子进程取消耗时 {elapsed:.2f}s")
        self.assertTrue(_wait_for(lambda: self._status(job.id) == "cancelled"))
        self.assertIsNotNone(proc.poll(), "忽略 SIGTERM 的子进程没有被 kill/reap")
        self.assertFalse(_alive(proc.pid))

    def test_shutdown_cancels_running_and_refuses_new_jobs(self):
        job, proc = self._submit_child_job(None)
        result = self.manager.shutdown(timeout=6.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["unfinished"], [])
        self.assertEqual(self._status(job.id), "cancelled")
        self.assertIsNotNone(proc.poll())
        with self.assertRaises(RuntimeError):
            self.manager.submit(jobs.Job("compress", "迟到任务",
                                         srcPath=self.home,
                                         destPath=self.home,
                                         bucket=self.home), lambda j: None)

    def test_shutdown_finishes_queued_jobs_without_running_them(self):
        jobs_started = []

        def hold(job, proc):
            jobs_started.append(job.id)
            proc.wait()

        first, _ = self._submit_child_job("bucket-a", on_task=hold)
        second, _ = self._submit_child_job("bucket-b", on_task=hold)
        self.assertTrue(_wait_for(lambda: self.manager.active_count() == 2))
        third = jobs.Job("compress", "排队任务", srcPath=self.home,
                         destPath=self.home, bucket="bucket-c")
        ran = threading.Event()

        def never_run(_job):
            ran.set()

        self.manager.submit(third, never_run)
        self.assertTrue(_wait_for(lambda: third.to_dict()["status"] == "queued"))
        result = self.manager.shutdown(timeout=6.0)
        self.assertTrue(result["ok"], result)
        self.assertFalse(ran.is_set(), "queued 任务不应该被执行")
        self.assertEqual(self._status(third.id), "cancelled")
        for jid in (first.id, second.id, third.id):
            self.assertTrue(_wait_for(lambda jid=jid: not os.path.exists(
                os.path.join(util.state_path("pending-jobs"), jid + ".json")),
                timeout=3))

    def test_cancel_queued_job_is_immediate(self):
        self._submit_child_job("bucket-a")
        self._submit_child_job("bucket-b")
        third = jobs.Job("compress", "排队任务", srcPath=self.home,
                         destPath=self.home, bucket="bucket-c")
        ran = threading.Event()
        self.manager.submit(third, lambda _job: ran.set())
        self.assertTrue(_wait_for(lambda: third.to_dict()["status"] == "queued"))
        self.assertTrue(self.manager.cancel(third.id))
        self.assertFalse(ran.is_set())
        self.assertEqual(self._status(third.id), "cancelled")

    def test_cancel_does_not_touch_unrelated_children(self):
        unrelated = self._spawn()
        job, _proc = self._submit_child_job(None)
        self.assertTrue(self.manager.cancel(job.id))
        self.assertTrue(_wait_for(lambda: self._status(job.id) == "cancelled"))
        self.assertIsNone(unrelated.poll(), "未注册的进程被误杀")
        self.assertTrue(_alive(unrelated.pid))

    def test_blocked_pipe_read_unblocks_after_cancel(self):
        job, proc = self._submit_child_job(
            None,
            on_task=lambda _job, p: p.stdout.read())  # blocks until process ends

        def cancel_now():
            time.sleep(0.3)
            self.manager.cancel(job.id)

        threading.Thread(target=cancel_now, daemon=True).start()
        self.assertTrue(_wait_for(lambda: self._status(job.id) == "cancelled",
                                  timeout=6), "阻塞 read 未返回")
        self.assertTrue(_wait_for(lambda: proc.poll() is not None),
                        "取消后子进程未退出")

    def test_shutdown_is_idempotent(self):
        self._submit_child_job(None)
        first = self.manager.shutdown(timeout=6.0)
        second = self.manager.shutdown(timeout=2.0)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)

    # ------------------------------------------------- engine-wrapper paths
    def test_engine_run_cancel_terminates_registered_child(self):
        from arca import engine as eng

        engine = eng.Engine.__new__(eng.Engine)   # run() only needs the args
        cancel = threading.Event()
        job_id = "engine-cancel-job"
        procs.bind(job_id)
        try:
            def trigger():
                time.sleep(0.3)
                cancel.set()
                procs.terminate_job(job_id)

            threading.Thread(target=trigger, daemon=True).start()
            started = time.time()
            code, _log = engine.run(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                cancel=cancel)
            elapsed = time.time() - started
        finally:
            procs.unbind()
        self.assertLess(elapsed, 6.0, f"engine.run 取消耗时 {elapsed:.2f}s")
        self.assertIsInstance(code, int)

    def test_par2_run_cancel_reports_cancelled(self):
        from arca import par2

        cancel = threading.Event()
        job_id = "par2-cancel-job"
        procs.bind(job_id)
        try:
            def trigger():
                time.sleep(0.3)
                cancel.set()
                procs.terminate_job(job_id)

            threading.Thread(target=trigger, daemon=True).start()
            started = time.time()
            with self.assertRaises(par2.Par2Error) as ctx:
                par2._run([sys.executable, "-c", "import time;time.sleep(30)"],
                          cancel=cancel)
            elapsed = time.time() - started
        finally:
            procs.unbind()
        self.assertLess(elapsed, 6.0, f"par2._run 取消耗时 {elapsed:.2f}s")
        self.assertIn("已取消", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
