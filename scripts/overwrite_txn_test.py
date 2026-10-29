#!/usr/bin/env python3
"""T06：覆盖压缩的可恢复替换（archtxn）确定性测试。

覆盖点：
  * 切换前取消/中止 → 旧集合原样保留；
  * 提交成功 → 旧文件被同卷 rename 换掉，备份与 manifest 清理；
  * 切换中途崩溃（switch-manifest/switch-old/switch-new）→ recover_pending
    幂等前滚，旧备份清理；
  * 压缩阶段崩溃（manifest phase=staging）→ 丢弃 staging，旧集合不动；
  * 分卷集合按 .001/.002 后缀映射；
  * PAR2 best-effort 失败 → 新归档保留、结果带 par2Error、旧 PAR2 不混用；
  * 回读校验（7zz t）拒绝坏档；
  * 重复恢复幂等。
"""
import glob
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from arca import archtxn  # noqa: E402
from arca import util  # noqa: E402


def _find_tool():
    path = shutil.which("7zz") or shutil.which("7z")
    if path:
        return path
    for cand in sorted(glob.glob(os.path.join(ROOT, "build", "7z*", "7zz"))):
        if os.path.isfile(cand):
            return cand
    return ""


class TxnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arca-txn-test-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state, exist_ok=True)
        self._patch = mock.patch.object(util, "app_support_dir",
                                        return_value=self.state)
        self._patch.start()
        self.zz = _find_tool()
        self.engine = SimpleNamespace(zz=self.zz, zstd="", par2=None)
        self.target = os.path.join(self.tmp, "out.7z")

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ARCA_TXN_FAIL_AT", None)

    # ---------------------------------------------------------------- helpers
    def _begin(self, percent=0, single=False):
        return archtxn.begin("job1", self.target, par2_percent=percent,
                             par2_single=single)

    def _write(self, path, data=b"NEW"):
        with open(path, "wb") as fh:
            fh.write(data)

    def _manifest(self, txn):
        return archtxn._manifest_path(txn.id)

    def _backups(self):
        return glob.glob(os.path.join(self.tmp, ".out.7z.arca-backup-*"))

    def _staging(self):
        return glob.glob(os.path.join(self.tmp, ".out.7z.arca-txn-*"))

    # ---------------------------------------------------------------- tests
    def test_commit_replaces_old_archive(self):
        self._write(self.target, b"OLD")
        txn = self._begin()
        self._write(txn.staging, b"NEW")
        result = archtxn.commit(txn, self.engine, [txn.staging])
        self.assertEqual(result["files"], [self.target])
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"NEW")
        self.assertFalse(self._backups())
        self.assertFalse(self._staging())
        self.assertFalse(os.path.exists(self._manifest(txn)))
        self.assertEqual(result["par2"], None)

    def test_abort_keeps_old_archive(self):
        self._write(self.target, b"OLD")
        txn = self._begin()
        self._write(txn.staging, b"PART")
        archtxn.abort(txn)
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"OLD")
        self.assertFalse(self._staging())
        self.assertFalse(os.path.exists(self._manifest(txn)))

    def test_cancel_before_commit_keeps_old(self):
        import threading
        self._write(self.target, b"OLD")
        txn = self._begin()
        self._write(txn.staging, b"PART")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(RuntimeError):
            archtxn.commit(txn, self.engine, [txn.staging], cancel=cancel)
        archtxn.abort(txn)
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"OLD")

    def _crash_commit(self, point):
        self._write(self.target, b"OLD")
        txn = self._begin()
        self._write(txn.staging, b"NEW")
        os.environ["ARCA_TXN_FAIL_AT"] = point
        try:
            with self.assertRaises(archtxn.SimulatedCrash):
                archtxn.commit(txn, self.engine, [txn.staging])
        finally:
            os.environ.pop("ARCA_TXN_FAIL_AT", None)
        return txn

    def test_crash_at_switch_old_recovers_forward(self):
        txn = self._crash_commit("switch-old")
        # crash 发生在 rename 之前：旧名还在，恢复时先移旧档再落新档。
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"OLD")
        handled = archtxn.recover_pending(self.engine)
        self.assertEqual(handled, [txn.id])
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"NEW")
        self.assertFalse(self._backups())
        self.assertFalse(os.path.exists(self._manifest(txn)))

    def test_crash_at_switch_new_recovers_forward(self):
        txn = self._crash_commit("switch-new")
        self.assertFalse(os.path.exists(self.target))
        handled = archtxn.recover_pending(self.engine)
        self.assertEqual(handled, [txn.id])
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"NEW")
        self.assertFalse(self._backups())

    def test_crash_at_switch_manifest_recovers_forward(self):
        txn = self._crash_commit("switch-manifest")
        handled = archtxn.recover_pending(self.engine)
        self.assertEqual(handled, [txn.id])
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"NEW")

    def test_phase_staging_recovery_discards_staging(self):
        self._write(self.target, b"OLD")
        txn = self._begin()
        self._write(txn.staging, b"PART")
        handled = archtxn.recover_pending(self.engine)
        self.assertEqual(handled, [txn.id])
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"OLD")
        self.assertFalse(self._staging())
        self.assertFalse(os.path.exists(self._manifest(txn)))

    def test_split_set_maps_suffixes(self):
        self._write(self.target + ".001", b"OLD1")
        self._write(self.target + ".002", b"OLD2")
        txn = self._begin()
        self._write(txn.staging + ".001", b"NEW1")
        self._write(txn.staging + ".002", b"NEW2")
        result = archtxn.commit(txn, self.engine,
                                [txn.staging + ".001", txn.staging + ".002"])
        self.assertEqual(result["files"],
                         [self.target + ".001", self.target + ".002"])
        for name, want in ((".001", b"NEW1"), (".002", b"NEW2")):
            with open(self.target + name, "rb") as fh:
                self.assertEqual(fh.read(), want)
        self.assertFalse(self._backups())

    def test_par2_failure_is_best_effort(self):
        self._write(self.target, b"OLD")
        old_par2 = self.target + ".par2"
        self._write(old_par2, b"OLDPAR2")
        txn = self._begin(percent=5)
        self._write(txn.staging, b"NEW")
        engine = SimpleNamespace(zz=self.zz, zstd="",
                                 par2=os.path.join(self.tmp, "missing-par2"))
        result = archtxn.commit(txn, engine, [txn.staging])
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), b"NEW")
        self.assertTrue(result["par2Error"])
        self.assertEqual(result["par2"], None)
        self.assertFalse(os.path.exists(old_par2))
        self.assertFalse(glob.glob(self.target + ".*.par2"))

    def test_verify_accepts_good_archive_and_rejects_corrupt(self):
        if not self.zz or not os.path.isfile(self.zz):
            self.skipTest("找不到 7zz")
        good = os.path.join(self.tmp, "good.zip")
        with zipfile.ZipFile(good, "w") as zf:
            zf.writestr("a.txt", "hello")
        archtxn.verify(self.engine, [good])
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as fh:
            fh.write(b"this is not an archive at all")
        with self.assertRaises(archtxn.TxnError):
            archtxn.verify(self.engine, [bad])

    def test_recover_is_idempotent(self):
        self._crash_commit("switch-new")
        self.assertEqual(len(archtxn.recover_pending(self.engine)), 1)
        self.assertEqual(archtxn.recover_pending(self.engine), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
