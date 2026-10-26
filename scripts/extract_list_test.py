from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from arca import engine, server
from arca.settings import Settings
import cdp
from menu_test import UI, api


class ExtractListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="arca-extract-list-")
        cls.root = Path(cls.tmp.name)
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.engine = engine.Engine()
        for count in (800, 805):
            with zipfile.ZipFile(cls.root / f"{count}.zip", "w") as archive:
                for n in range(count - 60):
                    archive.writestr(f"file-{n:04}.txt", b"abc")
                for n in range(60):
                    archive.writestr(f"dir-{n:04}/", b"")
        with zipfile.ZipFile(cls.root / "empty.zip", "w"):
            pass
        (cls.root / "broken.zip").write_bytes(b"not an archive")
        (cls.root / "secret.txt").write_text("secret fixture")
        subprocess.run([cls.engine.zz, "a", "-t7z", "-pfixture-password", "-mhe=on",
                        str(cls.root / "locked.7z"), "secret.txt"], cwd=cls.root,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        cls.ctx = server.AppContext(cls.engine, Settings(), str(ROOT / "src/arca/web"))
        cls.ctx.settings.patch({"checkEngineUpdates": False})
        cls.service = server.Server(cls.ctx)
        cls.service.start()
        cls.addClassCleanup(cls.service.httpd.server_close)
        cls.addClassCleanup(cls.service.stop)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.proc, cls.ws = cdp.launch(port, str(cls.root / "chrome"))
        cls.addClassCleanup(cls.stop_browser)
        cls.ui = UI(cls.ws)
        cls.ws.call("Page.enable")
        cls.ws.call("Page.navigate", {"url": f"http://127.0.0.1:{cls.service.port}/index.html?t={cls.ctx.token}"})
        deadline = time.monotonic() + 20
        while not cls.ui.js("document.querySelector('[data-nav=extract]')?.click(); !!document.querySelector('#view-extract.is-active') && typeof window.__arcaDrop === 'function'"):
            if time.monotonic() >= deadline:
                raise AssertionError("real extract navigation never became interactive")
            time.sleep(.1)
        print("UI READY: real extract navigation active and native drop hook installed")

    @classmethod
    def stop_browser(cls):
        cls.ws.close()
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    def load(self, fixture, expected):
        path = self.root / f"{self._testMethodName}-{fixture}"
        shutil.copyfile(self.root / fixture, path)
        self.ui.js(f"window.__arcaDrop([{json.dumps(str(path))}]); true")
        deadline = time.monotonic() + 20
        while not self.ui.js(f"document.querySelector('#x-name').textContent === {json.dumps(path.name)} && document.querySelector('#x-table').textContent.includes({json.dumps(expected)})"):
            self.assertLess(time.monotonic(), deadline, f"archive DOM never settled: {fixture}")
            time.sleep(.1)
        return api(self.service.port, self.ctx.token, "/inspect", {"path": str(path)})["info"] if fixture != "broken.zip" else None

    def snapshot(self):
        return self.ui.js("""(() => {
          const button = document.querySelector('#x-show-all');
          return {paths: [...document.querySelectorAll('#x-table tr[data-entry]')].map(e => e.dataset.entry),
            columns: [...document.querySelectorAll('#x-table tr[data-entry]')].every(e => e.children.length === 4),
            visible: getComputedStyle(button).display !== 'none', label: button.textContent,
            hint: document.querySelector('#x-more').textContent,
            text: document.querySelector('#x-table').textContent,
            disabled: document.querySelector('#x-start').disabled};
        })()""")

    def expand(self):
        self.ui.js("document.querySelector('#x-show-all').click(); true")
        result = self.snapshot()
        self.assertEqual(len(result["paths"]), 800)
        self.assertFalse(result["visible"])
        self.assertTrue(result["columns"])
        return result

    @staticmethod
    def ordered(info):
        return [e["path"] for e in info["entries"] if e["isDir"]] + [e["path"] for e in info["entries"] if not e["isDir"]]

    def test_800_entries_expand_and_directories(self):
        info = self.load("800.zip", "dir-0059")
        self.assertEqual(len(info["entries"]), 800)
        self.assertEqual(info["entriesTruncated"], 0)
        first = self.snapshot()
        self.assertEqual(first["paths"], self.ordered(info)[:200])
        self.assertTrue(first["columns"])
        self.assertTrue(first["visible"])
        self.assertEqual(first["label"], "显示全部 800 条")
        self.assertEqual(sum(p.startswith("dir-") for p in first["paths"]), 60)
        self.assertEqual(self.expand()["paths"], self.ordered(info))
        print("REAL HTTP/DOM: 800 ZIP entries, initial 200 includes all 60 directories; expanded 800 in directory/file order, four columns (selection + name/size/time) intact, button hidden")

    def test_new_archive_resets_expansion(self):
        self.load("800.zip", "dir-0059")
        self.expand()
        self.load("805.zip", "file-0000")
        result = self.snapshot()
        self.assertEqual(len(result["paths"]), 200)
        self.assertTrue(result["visible"])
        print("REAL HTTP/DOM: opening a new archive resets expanded 800 to initial 200")

    def test_backend_truncation_is_separate(self):
        info = self.load("805.zip", "file-0000")
        self.assertEqual(len(info["entries"]), 800)
        self.assertEqual(info["entriesTruncated"], 5)
        self.assertEqual(self.snapshot()["label"], "显示全部 800 条")
        self.assertIn("另有 5 条未返回（后端列表上限）", self.snapshot()["hint"])
        result = self.expand()
        self.assertEqual(result["paths"], self.ordered(info))
        self.assertIn("另有 5 条未返回（后端列表上限）", result["hint"])
        print("REAL HTTP/DOM: 805 ZIP entries => returned 800, backend omitted 5 separately reported before/after expansion; no promise to show 805")

    def test_empty_hides_old_button(self):
        self.load("800.zip", "dir-0059")
        self.expand()
        info = self.load("empty.zip", "空归档")
        self.assertEqual(info["entries"], [])
        result = self.snapshot()
        self.assertEqual(result["paths"], [])
        self.assertFalse(result["visible"])
        self.assertEqual(result["hint"], "0 个文件 · 0 个文件夹")
        print("REAL HTTP/DOM: empty ZIP clears old rows and hides expansion button")

    def test_password_hides_old_button(self):
        self.load("800.zip", "dir-0059")
        self.expand()
        info = self.load("locked.7z", "目录已加密")
        self.assertTrue(info["needsPassword"])
        self.assertEqual(info["entries"], [])
        result = self.snapshot()
        self.assertEqual(result["paths"], [])
        self.assertFalse(result["visible"])
        self.assertTrue(result["disabled"])
        self.assertEqual(result["hint"], "文件列表已加密")
        print("REAL HTTP/DOM: actual 7zz header-encrypted archive clears old rows, hides expansion button and gates extraction")

    def test_error_hides_old_button(self):
        self.load("800.zip", "dir-0059")
        self.expand()
        self.load("broken.zip", "无法读取目录")
        result = self.snapshot()
        self.assertEqual(result["paths"], [])
        self.assertFalse(result["visible"])
        self.assertTrue(result["disabled"])
        self.assertEqual(result["hint"], "目录读取失败")
        print("REAL HTTP/DOM: corrupt ZIP error clears old rows and hides expansion button")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="arca-extract-list-home-") as home:
        with patch.dict(os.environ, {"HOME": home}):
            result = unittest.main(verbosity=2, exit=False).result
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, {len(result.failures) + len(result.errors)} failed")
    raise SystemExit(not result.wasSuccessful())
