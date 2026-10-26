"""Shared scan semantics, worker updates, and real HTTP/CDP task-card regression."""
from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arca import engine, jobs, server, util
from arca.settings import Settings
import cdp
from menu_test import UI, api


class EngineReached(Exception):
    pass


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="arca-scan-")
        self.root = Path(self.tmp.name)
        self.files = self.root / "files"
        self.files.mkdir()
        for n in range(2305):
            (self.files / f"{n:04}.txt").write_bytes(b"small-file\n")
        self.total = 2305 * 11

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, inputs=None, cancel_at=None):
        job = jobs.Job("compress", "scan fixture")
        updates = []
        original = jobs.Job.update

        def update(target, **kw):
            original(target, **kw)
            if target is job:
                updates.append(target.to_dict())
                if cancel_at and target.item:
                    target.cancel.set()

        def run(*args, **kw):
            self.assertEqual(job.item, "")
            self.assertEqual(job.total, kw["total_bytes"])
            kw["on_progress"](25, "25 + payload.txt", 100)
            raise EngineReached()

        mgr = SimpleNamespace(engine=SimpleNamespace(build_add_cmd=lambda *a: [], run=run))
        opts = {"inputs": inputs or [str(self.files)], "format": "7z",
                "output": str(self.root / "out.7z")}
        with patch.object(jobs.Job, "update", update):
            with self.assertRaises(RuntimeError if cancel_at else EngineReached):
                jobs.make_compress_task(mgr, opts)(job)
        return job, updates

    def test_many_files(self):
        self.assertEqual(util.scan_inputs([str(self.files)]), (2305, self.total))

    def test_overlap_and_hardlink(self):
        os.link(self.files / "0000.txt", self.files / "hardlink")
        self.assertEqual(util.scan_inputs([str(self.files), str(self.files / "0000.txt"),
                                           str(self.files)]), (2305, self.total))

    def test_symlink_file_unknown(self):
        link = self.root / "link"
        link.symlink_to(self.files / "0000.txt")
        with self.assertRaises(OSError):
            util.scan_inputs([str(link)])

    def test_symlink_directory_unknown(self):
        (self.files / "linked").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            util.scan_inputs([str(self.files)])

    def test_stat_error_unknown(self):
        with patch.object(util.os, "stat", side_effect=PermissionError("controlled stat")):
            with self.assertRaises(OSError):
                util.scan_inputs([str(self.files)])

    def test_walk_error_unknown(self):
        def denied(path, *, onerror):
            onerror(PermissionError("controlled walk"))
        with patch.object(util.os, "walk", denied):
            with self.assertRaises(OSError):
                util.scan_inputs([str(self.files)])

    def test_inner_loop_cancel(self):
        cancel = threading.Event()
        seen = []
        def progress(count, size):
            seen.append(count)
            if count == 19:
                cancel.set()
        with self.assertRaisesRegex(RuntimeError, "已取消"):
            util.scan_inputs([str(self.files)], on_progress=progress, cancel=cancel)
        self.assertEqual(seen, list(range(1, 20)))

    def test_worker_updates_and_real_progress(self):
        job, updates = self.task()
        scans = [u for u in updates if u["item"].startswith("已扫描")]
        counts = [int(re.search(r"已扫描 (\d+)", u["item"])[1]) for u in scans]
        self.assertGreaterEqual(len(counts), 18)
        self.assertEqual(counts, sorted(set(counts)))
        self.assertTrue(all(u["total"] == u["percent"] == 0 for u in scans))
        self.assertTrue(all(u["phase"] == "正在扫描" and "约 " in u["item"] for u in scans))
        self.assertEqual(updates[-1]["item"], "")
        self.assertEqual(job.total, self.total)
        self.assertEqual((job.percent, job.done, job.item), (25, int(self.total / 4), "payload.txt"))
        print(f"WORKER: {len(counts)} increasing scan updates, cleared before engine; total={job.total}")

    def test_worker_cancel_clears_item(self):
        job, updates = self.task(cancel_at=128)
        self.assertTrue(job.cancel.is_set())
        self.assertEqual(updates[-1]["item"], "")
        self.assertEqual(job.total, 0)

    def test_worker_unknown_discards_partial_size(self):
        link = self.root / "unknown"
        link.symlink_to(self.files / "0000.txt")
        job, updates = self.task([str(self.files), str(link)])
        self.assertTrue(any(u["item"].startswith("已扫描") for u in updates))
        self.assertEqual(job.total, 0)
        self.assertEqual(updates[-1]["item"], "")

    def test_real_http_task_card(self):
        print("UI FIXTURE: 16 worker scan checkpoints acknowledged by real task-card polling; not a natural performance measurement")
        ctx = server.AppContext(engine.Engine(), Settings(), str(ROOT / "src/arca/web"))
        ctx.settings.patch({"checkEngineUpdates": False, "suppressSound": True})
        service = server.Server(ctx)
        service.start()
        original_scan = util.scan_inputs
        checkpoints = {count: threading.Event() for count in range(1, 2306)}
        held_counts = []
        def observed_scan(inputs, *, on_progress=None, cancel=None):
            def progress(count, size):
                on_progress(count, size)
                published = any(job.item.startswith(f"已扫描 {count} 个文件")
                                for job in ctx.manager.jobs.values())
                if published and len(held_counts) < 16:
                    held_counts.append(count)
                    if not checkpoints[count].wait(timeout=10):
                        raise RuntimeError(f"task card did not acknowledge scan checkpoint {count}")
            return original_scan(inputs, on_progress=progress if on_progress else None, cancel=cancel)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        debug_port = sock.getsockname()[1]
        sock.close()
        proc = ws = None
        try:
            proc, ws = cdp.launch(debug_port, str(self.root / "chrome"))
            ui = UI(ws)
            ws.call("Page.enable")
            ws.call("Page.navigate", {"url": f"http://127.0.0.1:{service.port}/index.html?t={ctx.token}"})
            deadline = time.monotonic() + 20
            while not ui.js("document.querySelector('[data-nav=queue]')?.click(); !!document.querySelector('#view-queue.is-active')"):
                self.assertLess(time.monotonic(), deadline, "queue navigation never became interactive")
                time.sleep(.1)
            print("UI READY: queue navigation activated the real queue view before HTTP submission")
            samples = []
            with patch.object(util, "scan_inputs", observed_scan):
                result = api(service.port, ctx.token, "/compress", {
                    "inputs": [str(self.files)], "destDir": str(self.root),
                    "name": "real-scan", "format": "7z"})
                self.assertTrue(result["ok"], result)
                job_id = result["job"]["id"]
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    rows = ui.js("[...document.querySelectorAll('#q-list .job')].map(e => ({id:e.dataset.job, text:e.querySelector('.job-sub').textContent, indet:!!e.querySelector('.bar.indet')}))")
                    for row in rows:
                        if row["id"] != job_id:
                            continue
                        match = re.search(r"已扫描 (\d+) 个文件 · 约 (.+)", row["text"])
                        if match and (not samples or samples[-1][0] != int(match[1])):
                            count = int(match[1])
                            self.assertTrue(row["indet"], row)
                            samples.append((count, row["text"], row["indet"]))
                            if count in checkpoints:
                                checkpoints[count].set()
                    current = next((j for j in api(service.port, ctx.token, "/jobs")["jobs"]
                                    if j["id"] == job_id), None)
                    if current and current["status"] in ("done", "error", "canceled"):
                        break
                    time.sleep(.1)
                else:
                    self.fail(f"real compression timed out; samples={samples!r}")
            self.assertEqual(current["status"], "done", current)
            self.assertEqual(current["item"], "")
            self.assertEqual(current["total"], self.total)
            self.assertTrue((self.root / "real-scan.7z").is_file())
            self.assertGreaterEqual(len(samples), 16, samples)
            self.assertEqual(len(held_counts), 16, held_counts)
            self.assertTrue(all(checkpoints[count].is_set() for count in held_counts), samples)
            self.assertEqual([s[0] for s in samples], sorted(set(s[0] for s in samples)))
            self.assertTrue(all(s[2] for s in samples))
            print("REAL TASK CARD: " + repr(samples))
            deadline = time.monotonic() + 5
            while "已扫描" in str(ui.js("document.querySelector('#q-list .job-sub').textContent")):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.1)
            print("REAL TASK CARD: scan subtitle cleared after actual 7zz completion; archive exists")
        finally:
            for job in ctx.manager.jobs.values():
                job.cancel.set()
            for event in checkpoints.values():
                event.set()
            service.stop()
            service.httpd.server_close()
            if ws:
                ws.close()
            if proc:
                proc.terminate()
                proc.wait(timeout=10)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="arca-scan-home-") as home:
        with patch.dict(os.environ, {"HOME": home}):
            result = unittest.main(verbosity=2, exit=False).result
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, {len(result.failures) + len(result.errors)} failed")
    raise SystemExit(not result.wasSuccessful())
