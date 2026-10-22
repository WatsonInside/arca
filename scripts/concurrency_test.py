from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arca import engine, server, util
from arca.settings import Settings
import cdp
from menu_test import UI, api


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="arca-concurrency-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.engine = engine.Engine()
        self.ctx = server.AppContext(self.engine, Settings(), str(ROOT / "src/arca/web"))
        self.ctx.settings.patch({"checkEngineUpdates": False, "suppressSound": True})
        self.service = server.Server(self.ctx)
        self.service.start()
        self.addCleanup(self.service.httpd.server_close)
        self.addCleanup(self.service.stop)
        self.source = self.root / "input.txt"
        self.source.write_text("real concurrent engine fixture\n" * 1000)
        self.release = threading.Event()
        self.completed = []
        self.lock = threading.Lock()
        original = self.engine.run

        def controlled(*args, **kwargs):
            result = original(*args, **kwargs)
            with self.lock:
                self.completed.append(args[0])
            if not self.release.wait(30):
                raise AssertionError("test engine completion boundary timed out")
            return result

        self.patcher = patch.object(self.engine, "run", side_effect=controlled)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.drain)

    def drain(self):
        self.release.set()
        self.wait(lambda: self.ctx.manager.active_count() == 0)

    def wait(self, condition, timeout=25):
        deadline = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), deadline, "condition timed out")
            time.sleep(.05)

    def request(self, path, body=None):
        return api(self.service.port, self.ctx.token, path, body)

    def compress(self, directory, name="archive", **extra):
        dest = self.root / directory
        dest.mkdir(exist_ok=True)
        return self.request("/compress", dict(inputs=[str(self.source)], destDir=str(dest),
                                             name=name, format="zip", **extra))

    def states(self):
        return {j["id"]: j for j in self.request("/jobs")["jobs"]}

    def settled(self, responses):
        ids = [r["job"]["id"] for r in responses]
        self.wait(lambda: all(self.states()[i]["status"] not in ("queued", "running") for i in ids))
        self.wait(lambda: not self.ctx.manager._busy)
        return [self.states()[i] for i in ids]

    def assert_history(self, states):
        expected = {s["id"] for s in states if s["status"] == "done"}
        rows = self.request("/history")["history"]
        actual = [r["id"] for r in rows if r["id"] in {s["id"] for s in states}]
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(util.read_json(util.state_path("history.json"), []), rows)

    def browser(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc, ws = cdp.launch(port, str(self.root / "chrome"))
        def close():
            ws.close()
            proc.terminate()
            proc.wait(timeout=10)
        self.addCleanup(close)
        ui = UI(ws)
        ws.call("Page.enable")
        ws.call("Page.navigate", {"url": f"http://127.0.0.1:{self.service.port}/index.html?t={self.ctx.token}"})
        self.wait(lambda: ui.js("document.querySelector('[data-nav=queue]')?.click(); !!document.querySelector('#view-queue.is-active')"))
        return ui

    def test_distinct_buckets_fifo_skip_and_real_dom(self):
        ui = self.browser()
        a = self.compress("a", "first")
        self.wait(lambda: len(self.completed) == 1)
        a2 = self.compress("a", "second")
        b = self.compress("b")
        c = self.compress("c")
        d = self.compress("d")
        responses = [a, a2, b, c, d]
        self.wait(lambda: len(self.completed) == 2)
        states = self.states()
        self.assertEqual(sum(j["status"] == "running" for j in states.values()), 2)
        self.assertEqual(states[a2["job"]["id"]]["status"], "queued")
        self.assertEqual(states[b["job"]["id"]]["status"], "running")
        self.assertEqual(self.request("/jobs")["active"], 5)
        self.wait(lambda: ui.js("document.querySelector('#nav-count').textContent === '2' && !document.querySelector('#nav-count').classList.contains('hidden')"))
        print("REAL HTTP/DOM: four distinct buckets plus same-bucket follower; 2 running, 3 queued; nav-count=2 (running), API active=5 (queued+running)")
        self.release.set()
        final = self.settled(responses)
        self.assertTrue(all(j["status"] == "done" for j in final), final)
        self.assertGreaterEqual(final[1]["started"], final[0]["finished"])
        for response in responses:
            self.assertTrue(Path(response["target"]).is_file())
            code, log = self.engine.run(self.engine.build_test_cmd(response["target"], ""))
            self.assertEqual(code, 0, log)
        self.assert_history(final)
        print("REAL ENGINE: all five archives pass 7zz test; same-bucket started >= previous finished; history exact")

    def test_cancel_rolls_back_only_own_real_output(self):
        a = self.compress("cancel")
        b = self.compress("survive")
        self.wait(lambda: len(self.completed) == 2)
        self.assertTrue(Path(a["target"]).is_file())
        self.assertTrue(Path(b["target"]).is_file())
        self.request("/job/cancel", {"id": a["job"]["id"]})
        self.assertEqual(self.states()[b["job"]["id"]]["status"], "running")
        self.release.set()
        final = self.settled([a, b])
        self.assertEqual([j["status"] for j in final], ["cancelled", "done"])
        self.assertFalse(Path(a["target"]).exists())
        self.assertTrue(Path(b["target"]).exists())
        self.assert_history(final)
        print("REAL ENGINE: cancellation at controlled post-process/pre-result boundary rolls back actual output only; peer completes")

    def test_queued_names_and_overwrite_do_not_delete_running_output(self):
        first = self.compress("same", "name")
        self.wait(lambda: len(self.completed) == 1)
        original = Path(first["target"]).read_bytes()
        second = self.compress("same", "name")
        third = self.compress("same", "name")
        self.assertEqual(len({r["target"] for r in (first, second, third)}), 3)
        preflight = self.request("/preflight", {"destDir": str(self.root / "same"), "name": "name", "format": "zip"})
        self.assertTrue(preflight["exists"])
        self.assertNotIn(preflight["suggestion"], [r["target"] for r in (first, second, third)])
        overwrite = self.compress("same", "name", overwrite=True)
        self.assertEqual(overwrite["target"], first["target"])
        self.assertEqual(Path(first["target"]).read_bytes(), original)
        self.request("/job/cancel", {"id": overwrite["job"]["id"]})
        self.release.set()
        final = self.settled([first, second, third, overwrite])
        self.assertEqual([j["status"] for j in final], ["done", "done", "done", "cancelled"])
        self.assertEqual(Path(first["target"]).read_bytes(), original)
        self.assert_history(final)
        overwrite2 = self.compress("same", "name", overwrite=True)
        self.assertEqual(self.settled([overwrite2])[0]["status"], "done")
        self.assertTrue(Path(overwrite2["target"]).is_file())
        print("REAL HTTP: queued unique names/preflight agree; queued overwrite preserves running output, cancelled overwrite never deletes; executed overwrite succeeds")

    def test_extract_root_reservations_and_symlink_bucket(self):
        self.release.set()
        archive = self.compress("archives", "fixture")
        self.assertEqual(self.settled([archive])[0]["status"], "done")
        self.release.clear()
        self.completed.clear()
        dest = self.root / "extracted"
        dest.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(dest, target_is_directory=True)
        body = {"path": archive["target"], "destDir": str(dest), "autoFolder": True}
        first = self.request("/extract", body)
        self.wait(lambda: len(self.completed) == 1)
        second = self.request("/extract", dict(body, destDir=str(alias), onExists="rename"))
        third = self.request("/extract", dict(body, onExists="rename"))
        self.assertEqual(len({os.path.realpath(r["dest"]) for r in (first, second, third)}), 3)
        try:
            self.request("/extract", body)
            self.fail("expected existing-target conflict")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
            conflict = json.load(exc)
        self.assertEqual(conflict["code"], "exists")
        self.assertNotIn(os.path.realpath(conflict["suggestion"]), [os.path.realpath(r["dest"]) for r in (first, second, third)])
        self.assertEqual(self.states()[second["job"]["id"]]["status"], "queued")
        self.release.set()
        final = self.settled([first, second, third])
        self.assertTrue(all(j["status"] == "done" for j in final), final)
        for previous, current in zip(final, final[1:]):
            self.assertGreaterEqual(current["started"], previous["finished"])
        for response in (first, second, third):
            self.assertEqual((Path(response["dest"]) / self.source.name).read_bytes(), self.source.read_bytes())
        self.assert_history(final)
        print("REAL HTTP/ENGINE: extract dest_root realpath serializes distinct autoFolders and symlink alias; queued rename/409 suggestions reserve unique real targets")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="arca-concurrency-home-") as home:
        with patch.dict(os.environ, {"HOME": home}):
            result = unittest.main(verbosity=2, exit=False).result
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, {len(result.failures) + len(result.errors)} failed")
    raise SystemExit(not result.wasSuccessful())
