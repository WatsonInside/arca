from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from arca import server

MARGIN = 64 * 1024 * 1024


class CompressSpaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "input.bin"
        self.source.write_bytes(b"a" * 4096)
        self.dest = self.root / "out"
        self.dest.mkdir()
        self.target = self.dest / "archive.zip"
        self.target.write_bytes(b"old archive")
        self.handler = object.__new__(server.Handler)
        self.handler.ctx = SimpleNamespace(
            engine=SimpleNamespace(rar=True, zstd=True, par2=True,
                                   resolve=lambda p: p, parts=lambda p: []),
            manager=Mock(_cv=server.threading.Condition(), _targets={}))
        self.handler._err = lambda message: {"ok": False, "error": message}
        self.handler._json = lambda payload: payload
        self.body = {"inputs": [str(self.source)], "format": "zip",
                     "destDir": str(self.dest), "name": "archive",
                     "overwrite": True, "size": 0, "totalBytes": 0,
                     "info": {"uncompressed": 0}}

    def compress(self, free, **patches):
        with ExitStack() as stack:
            usage = stack.enter_context(patch.object(
                server.shutil, "disk_usage", return_value=SimpleNamespace(free=free)))
            for name, kwargs in patches.items():
                stack.enter_context(patch(name, **kwargs))
            result = self.handler._compress(self.body)
        return result, usage

    def assert_rejected(self, result):
        self.assertFalse(result["ok"])
        self.assertEqual(self.target.read_bytes(), b"old archive")
        self.handler.ctx.manager.submit.assert_not_called()

    def test_file_rejects_before_overwrite_and_ignores_frontend_size(self):
        result, usage = self.compress(MARGIN + 4095)
        self.assert_rejected(result)
        usage.assert_called_once_with(str(self.dest))
        self.assertEqual(result["error"], "磁盘剩余空间不足：还需要约 %s，可用 %s" % (
            server.util.human_size(4096), server.util.human_size(MARGIN + 4095)))

    def test_nested_directory_and_multiple_inputs(self):
        directory = self.root / "folder"
        (directory / "nested").mkdir(parents=True)
        (directory / "first").write_bytes(b"x" * 8192)
        (directory / "nested" / "second").write_bytes(b"y" * 16384)
        self.body["inputs"].append(str(directory))
        result, _ = self.compress(MARGIN + 4096 + 8192 + 16384 - 1)
        self.assert_rejected(result)

    def test_exact_boundary_submits_and_overwrites(self):
        result, _ = self.compress(MARGIN + 4096)
        self.assertTrue(result["ok"])
        self.handler.ctx.manager.submit.assert_called_once()
        self.assertEqual(self.target.read_bytes(), b"old archive")

    def test_zero_free_rejects(self):
        result, _ = self.compress(0)
        self.assert_rejected(result)

    def test_empty_input_directory_still_needs_margin(self):
        directory = self.root / "empty"
        directory.mkdir()
        self.body["inputs"] = [str(directory)]
        result, _ = self.compress(MARGIN - 1)
        self.assert_rejected(result)

    def test_disk_error_allows_submission(self):
        result, _ = self.compress(0, **{
            "arca.server.shutil.disk_usage": {"side_effect": OSError("disk unavailable")}})
        self.assertTrue(result["ok"])
        self.handler.ctx.manager.submit.assert_called_once()

    def test_stat_error_allows_submission(self):
        real_stat = os.stat
        def failing_stat(path, *args, **kwargs):
            if str(path) == str(self.source) and kwargs.get("follow_symlinks") is False:
                raise OSError("stat unavailable")
            return real_stat(path, *args, **kwargs)
        result, _ = self.compress(0, **{
            "arca.server.os.stat": {"side_effect": failing_stat}})
        self.assertTrue(result["ok"])
        self.handler.ctx.manager.submit.assert_called_once()

    def test_walk_error_allows_submission(self):
        directory = self.root / "folder"
        directory.mkdir()
        self.body["inputs"] = [str(directory)]
        def failing_walk(path, *, onerror):
            onerror(PermissionError("unreadable directory"))
            return iter(())
        result, _ = self.compress(0, **{
            "arca.server.os.walk": {"side_effect": failing_walk}})
        self.assertTrue(result["ok"])
        self.handler.ctx.manager.submit.assert_called_once()

    def test_nested_stat_error_discards_partial_estimate(self):
        directory = self.root / "folder"
        directory.mkdir()
        broken = directory / "broken"
        broken.write_bytes(b"x")
        self.body["inputs"].append(str(directory))
        real_stat = os.stat
        def failing_stat(path, *args, **kwargs):
            if str(path) == str(broken) and kwargs.get("follow_symlinks") is False:
                raise OSError("file disappeared")
            return real_stat(path, *args, **kwargs)
        result, _ = self.compress(0, **{
            "arca.server.os.stat": {"side_effect": failing_stat}})
        self.assertTrue(result["ok"])
        self.handler.ctx.manager.submit.assert_called_once()

    def test_symlink_directory_allows_unknown_size(self):
        directory = self.root / "folder"
        directory.mkdir()
        (directory / "cycle").symlink_to(directory, target_is_directory=True)
        self.body["inputs"] = [str(directory)]
        result, _ = self.compress(0)
        self.assertTrue(result["ok"])
        self.handler.ctx.manager.submit.assert_called_once()

    def test_overlap_does_not_double_count(self):
        self.body["inputs"].append(str(self.source))
        result, _ = self.compress(MARGIN + 4096)
        self.assertTrue(result["ok"])
        self.handler.ctx.manager.submit.assert_called_once()

    def test_default_destination(self):
        del self.body["destDir"]
        result, usage = self.compress(0)
        self.assertFalse(result["ok"])
        usage.assert_called_once_with(str(self.root))
        self.handler.ctx.manager.submit.assert_not_called()

    def test_all_formats_check_before_task(self):
        for fmt in ("7z", "zip", "tar.zst", "rar"):
            with self.subTest(format=fmt):
                self.body["format"] = fmt
                result, _ = self.compress(0)
                self.assert_rejected(result)

    def test_rejection_never_calls_remove_target(self):
        with patch.object(server, "_remove_target") as remove:
            result, _ = self.compress(0)
        self.assert_rejected(result)
        remove.assert_not_called()


class UniqueTargetTests(unittest.TestCase):
    """唯一化命名：复合扩展名 .tar.zst 必须整体对待（回归锁）。

    此前 os.path.splitext 只剥出 ".zst"，冲突时产出 "x.tar 2.zst"；
    正确结果是把序号插在整个 ".tar.zst" 之前。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name)
        self.handler = object.__new__(server.Handler)
        self.handler.ctx = SimpleNamespace(
            engine=SimpleNamespace(resolve=lambda p: p, parts=lambda p: []),
            manager=Mock(_cv=server.threading.Condition(), _targets={}))

    def unique(self, name):
        target = str(self.dest / name)
        return self.handler._unique_target(target, archive=True)

    def place(self, name):
        (self.dest / name).write_bytes(b"x")

    def test_tar_zst_conflict_increments_before_full_extension(self):
        self.place("name.tar.zst")
        first = self.unique("name.tar.zst")
        self.assertEqual(os.path.basename(first), "name 2.tar.zst")
        self.place("name 2.tar.zst")
        second = self.unique("name.tar.zst")
        self.assertEqual(os.path.basename(second), "name 3.tar.zst")

    def test_tar_zst_skips_numbers_taken_by_par2_sidecars(self):
        # 真实 PAR2 sidecar 占用（此前误用普通归档文件冒充）：归档本体不在，
        # 但 .par2 索引与 .vol 数据档在——复用该名字会让新档与旧恢复记录
        # 纠缠，必须跳到下一个序号。占用判定走 par2.recovery_files 的真实
        # 匹配规则（<stem>.par2 与 <stem>.vol*）。
        self.place("name.tar.zst")
        self.place("name 2.tar.zst.par2")
        self.place("name 2.tar.zst.vol1-2.par2")
        suggestion = self.unique("name.tar.zst")
        self.assertEqual(os.path.basename(suggestion), "name 3.tar.zst")

    def test_tar_zst_skips_numbers_taken_by_split_volumes(self):
        # 分卷占用走 engine.parts：任一分卷（.001…）存在即视为该名字已占用。
        self.handler.ctx.engine.parts = (
            lambda p: [p + ".001"] if os.path.basename(p) == "name 2.tar.zst" else [])
        self.place("name.tar.zst")
        suggestion = self.unique("name.tar.zst")
        self.assertEqual(os.path.basename(suggestion), "name 3.tar.zst")

    def test_other_formats_keep_existing_behavior(self):
        for name, expected in (("a.zip", "a 2.zip"), ("a.7z", "a 2.7z"),
                               ("a.rar", "a 2.rar"), ("a.zst", "a 2.zst"),
                               ("a.tar", "a 2.tar")):
            with self.subTest(name=name):
                self.place(name)
                got = self.unique(name)
                self.assertEqual(os.path.basename(got), expected)

    def test_active_job_target_blocks_name_even_without_file(self):
        # 目录里还没有这个文件，但一个活动任务已把它登记为目标：同样算冲突。
        self.handler.ctx.manager._targets["job-1"] = os.path.realpath(
            self.dest / "busy.tar.zst")
        got = self.unique("busy.tar.zst")
        self.assertEqual(os.path.basename(got), "busy 2.tar.zst")


if __name__ == "__main__":
    unittest.main(verbosity=2)
