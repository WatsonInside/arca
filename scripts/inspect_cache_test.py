from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from arca import server


class InspectCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "archive.zip"
        self.write_zip(100)
        self.handler = object.__new__(server.Handler)
        self.engine = server.eng.Engine()
        self.real_inspect = self.engine.inspect
        self.engine.inspect = Mock(wraps=self.real_inspect)
        self.handler.ctx = SimpleNamespace(engine=self.engine, inspect_cache={},
                                           inspect_lock=server.threading.RLock(),
                                           manager=Mock(_cv=server.threading.Condition(), _targets={}),
                                           settings={"overwrite": "rename"})
        self.handler._json = lambda value: value
        self.handler._err = lambda message: {"ok": False, "error": message}
        self.body = {"path": str(self.path), "destDir": str(self.root),
                     "autoFolder": False, "info": {"uncompressed": 1, "type": "stale"}}
        self.task = patch.object(server.jobs_mod, "make_extract_task", return_value=Mock())
        self.make_task = self.task.start()
        self.addCleanup(self.task.stop)

    def write_zip(self, size):
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("data", b"x" * size)

    def inspect(self):
        return self.handler._inspect({"path": str(self.path)})["info"]

    def extract(self):
        with patch.object(server.shutil, "disk_usage", return_value=SimpleNamespace(free=10**12)):
            result = self.handler._extract(self.body)
        self.assertTrue(result["ok"])
        return self.make_task.call_args.args[1]

    def test_same_path_replacement_and_old_frontend_info(self):
        self.inspect()
        replacement = self.root / "replacement.zip"
        with zipfile.ZipFile(replacement, "w") as archive:
            archive.writestr("data", b"y" * 9000)
        os.replace(replacement, self.path)
        opts = self.extract()
        self.assertEqual(opts["totalBytes"], 9000)
        self.assertEqual(opts["format"], "zip")
        self.assertEqual(self.engine.inspect.call_count, 2)

    def test_mtime_only_change(self):
        self.inspect()
        old = self.path.stat()
        os.utime(self.path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000000))
        self.assertEqual(self.extract()["totalBytes"], 100)
        self.assertEqual(self.engine.inspect.call_count, 2)

    def test_size_change_with_same_mtime(self):
        self.inspect()
        old = self.path.stat()
        self.write_zip(4000)
        os.utime(self.path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(self.extract()["totalBytes"], 4000)
        self.assertEqual(self.engine.inspect.call_count, 2)

    def test_stat_failure_is_miss_and_not_cached(self):
        self.inspect()
        with patch.object(self.handler, "_archive_stat", return_value=None):
            self.assertEqual(self.extract()["totalBytes"], 100)
        self.assertEqual(self.engine.inspect.call_count, 2)
        self.assertNotIn(str(self.path), self.handler.ctx.inspect_cache)

    def test_os_stat_failure(self):
        with patch.object(server.os, "stat", side_effect=OSError("unavailable")):
            self.assertIsNone(self.handler._archive_stat(str(self.path)))

    def test_valid_hit_refreshes_lru_and_ignores_frontend(self):
        self.inspect()
        cache = self.handler.ctx.inspect_cache
        cache["other"] = {}
        self.assertEqual(self.extract()["totalBytes"], 100)
        self.assertEqual(list(cache), ["other", str(self.path)])
        self.assertEqual(self.engine.inspect.call_count, 1)

    def test_capacity_64_and_hit_survives_eviction(self):
        cache = self.handler.ctx.inspect_cache
        first = str(self.path)
        self.inspect()
        for index in range(63):
            path = self.root / (str(index) + ".zip")
            path.write_bytes(self.path.read_bytes())
            self.handler._inspect({"path": str(path)})
        self.extract()
        new = self.root / "new.zip"
        new.write_bytes(self.path.read_bytes())
        self.handler._inspect({"path": str(new)})
        self.assertEqual(len(cache), 64)
        self.assertIn(first, cache)
        self.assertNotIn(str(self.root / "0.zip"), cache)

    def test_change_during_inspect_not_bound_to_new_stat(self):
        def changing(path, password):
            info = self.real_inspect(path, password)
            self.write_zip(8000)
            return info
        self.engine.inspect.side_effect = changing
        self.assertEqual(self.inspect()["uncompressed"], 100)
        self.assertNotIn(str(self.path), self.handler.ctx.inspect_cache)
        self.engine.inspect.side_effect = None
        self.assertEqual(self.extract()["totalBytes"], 8000)

    def test_miss_without_frontend_info(self):
        self.body.pop("info")
        self.assertEqual(self.extract()["totalBytes"], 100)
        self.engine.inspect.assert_called_once_with(str(self.path), "")

    def test_fresh_size_drives_space_rejection(self):
        self.inspect()
        self.write_zip(5000)
        with patch.object(server.shutil, "disk_usage", return_value=SimpleNamespace(free=64*1024*1024+4999)):
            result = self.handler._extract(self.body)
        self.assertFalse(result["ok"])
        self.assertIn("磁盘剩余空间不足", result["error"])
        self.handler.ctx.manager.submit.assert_not_called()

    def test_password_needed_cache_reinspects_with_password(self):
        self.inspect()
        self.handler.ctx.inspect_cache[str(self.path)]["needsPassword"] = True
        self.body["password"] = "secret"
        self.extract()
        self.engine.inspect.assert_called_with(str(self.path), "secret")

    def test_resolved_split_entry_used_for_stat_and_cache(self):
        alias = self.root / "archive.z01"
        alias.write_bytes(b"part")
        self.handler._inspect({"path": str(alias)})
        self.assertIn(str(self.path), self.handler.ctx.inspect_cache)
        self.assertNotIn(str(alias), self.handler.ctx.inspect_cache)
        self.body["path"] = str(alias)
        self.assertEqual(self.extract()["archive"], str(self.path))
        self.assertEqual(self.engine.inspect.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
