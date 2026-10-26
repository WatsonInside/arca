"""menu_test 收尾策略回归锁：现场只能在确认任务回收之后删除。

对应缺陷：main() 曾忽略 reclaim_round() 的返回值、回收检查异常只打 WARN，
随后仍无条件 rmtree(DEMO/profile)，可能在任务仍在写盘时删掉现场并假报成功。

本套件全部用 mock：mkdtemp/makedirs/rmtree/_run/reclaim_round 均不触真实
现场，只断言「哪条路径允许/禁止删除」以及退出码与原异常是否被保留。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import menu_test  # noqa: E402

DEMO = "/fake/build/menu-demo-test"


class MainTeardownTests(unittest.TestCase):
    def setUp(self):
        self.rmtree = mock.patch.object(menu_test.shutil, "rmtree").start()
        mock.patch.object(menu_test.tempfile, "mkdtemp",
                          return_value=DEMO).start()
        mock.patch.object(menu_test.os, "makedirs").start()
        self.run_mock = mock.patch.object(menu_test, "_run", return_value=0).start()
        self.reclaim = mock.patch.object(menu_test, "reclaim_round").start()
        self.addCleanup(mock.patch.stopall)
        self.out = io.StringIO()

    # ------------------------------------------------------------ helpers
    def invoke(self):
        with contextlib.redirect_stdout(self.out):
            return menu_test.main()

    def profile(self):
        """main() 为 Chrome profile 生成的路径（第二次 rmtree 的目标）。"""
        self.assertEqual(len(self.rmtree.call_args_list), 2)
        return self.rmtree.call_args_list[1].args[0]

    def assert_scene_deleted(self):
        self.assertEqual(self.rmtree.call_args_list,
                         [mock.call(DEMO, ignore_errors=True),
                          mock.call(self.profile(), ignore_errors=True)])

    def assert_scene_kept(self):
        self.rmtree.assert_not_called()

    # ------------------------------------------------ allowed cleanup paths
    def test_not_connected_none_allows_cleanup(self):
        # 尚未连接实例：本轮不可能提交过任务，允许删现场。
        self.reclaim.return_value = None
        code = self.invoke()
        self.assertEqual(code, 0)
        self.assert_scene_deleted()
        self.assertIn("尚未连接实例", self.out.getvalue())

    def test_zero_left_allows_cleanup(self):
        # 本轮活动任务全部回收：允许删现场。
        self.reclaim.return_value = 0
        code = self.invoke()
        self.assertEqual(code, 0)
        self.assert_scene_deleted()
        self.assertIn("已全部回收", self.out.getvalue())

    def test_nonzero_run_code_preserved_when_cleanup_allowed(self):
        # _run 自身失败码不能被收尾洗成 0。
        self.run_mock.return_value = 1
        self.reclaim.return_value = 0
        self.assertEqual(self.invoke(), 1)
        self.assert_scene_deleted()

    # ---------------------------------------------- forbidden cleanup paths
    def test_leftover_blocks_cleanup_and_fails(self):
        # 仍有活动任务：不删 DEMO、不删 profile，测试非零退出。
        self.reclaim.return_value = 3
        code = self.invoke()
        self.assertEqual(code, 1)
        self.assert_scene_kept()
        out = self.out.getvalue()
        self.assertIn("仍有 3 个本轮任务未回收", out)
        self.assertIn(f"DEMO={DEMO}", out)
        self.assertIn("Chrome profile=", out)

    def test_reclaim_exception_blocks_cleanup_and_fails(self):
        # 回收检查抛异常同样禁止清理，并把异常原因写进诊断。
        self.reclaim.side_effect = RuntimeError("reclaim boom")
        code = self.invoke()
        self.assertEqual(code, 1)
        self.assert_scene_kept()
        out = self.out.getvalue()
        self.assertIn("收尾任务回收检查失败", out)
        self.assertIn("RuntimeError: reclaim boom", out)
        self.assertIn(f"DEMO={DEMO}", out)

    # ------------------------------------------------- original exception
    def test_run_exception_survives_leftover(self):
        # _run 的异常 + 收尾有未回收任务：原异常必须继续传播，不得被吞。
        self.run_mock.side_effect = ValueError("run boom")
        self.reclaim.return_value = 2
        with contextlib.redirect_stdout(self.out):
            with self.assertRaises(ValueError) as ctx:
                menu_test.main()
        self.assertEqual(str(ctx.exception), "run boom")
        self.assert_scene_kept()
        self.assertIn("仍有 2 个本轮任务未回收", self.out.getvalue())
        self.assertTrue(any("现场已保留" in note
                            for note in getattr(ctx.exception, "__notes__", [])))

    def test_run_exception_survives_reclaim_check_exception(self):
        # 收尾检查自身抛异常也不能顶掉 _run 的原始异常。
        self.run_mock.side_effect = ValueError("run boom")
        self.reclaim.side_effect = RuntimeError("reclaim boom")
        with contextlib.redirect_stdout(self.out):
            with self.assertRaises(ValueError) as ctx:
                menu_test.main()
        self.assertEqual(str(ctx.exception), "run boom")
        self.assert_scene_kept()
        out = self.out.getvalue()
        self.assertIn("收尾任务回收检查失败", out)
        self.assertIn("RuntimeError: reclaim boom", out)
        self.assertTrue(any("现场已保留" in note
                            for note in getattr(ctx.exception, "__notes__", [])))

    def test_run_exception_survives_teardown_self_error(self):
        # 防御分支：收尾辅助函数自身意外抛错时，也只追加诊断，不顶掉原异常。
        self.run_mock.side_effect = ValueError("run boom")
        with mock.patch.object(menu_test, "_teardown",
                               side_effect=RuntimeError("teardown boom")):
            with contextlib.redirect_stdout(self.out):
                with self.assertRaises(ValueError) as ctx:
                    menu_test.main()
        self.assertEqual(str(ctx.exception), "run boom")
        out = self.out.getvalue()
        self.assertIn("收尾自身异常", out)
        self.assertIn("teardown boom", out)
        self.assertTrue(any("teardown boom" in note
                            for note in getattr(ctx.exception, "__notes__", [])))

    def test_run_exception_survives_successful_cleanup(self):
        # 收尾成功同样只是被清理现场，_run 的异常照常向外传播。
        self.run_mock.side_effect = ValueError("run boom")
        self.reclaim.return_value = 0
        with contextlib.redirect_stdout(self.out):
            with self.assertRaises(ValueError) as ctx:
                menu_test.main()
        self.assertEqual(str(ctx.exception), "run boom")
        self.assert_scene_deleted()
        self.assertTrue(any("现场已清理" in note
                            for note in getattr(ctx.exception, "__notes__", [])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
