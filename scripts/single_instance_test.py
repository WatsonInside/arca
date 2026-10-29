#!/usr/bin/env python3
"""单实例锁（T12-lite）回归。

真实 build/Arca-test.app --headless + 隔离 HOME：
- 同 HOME 第二实例快速退出（非零 + 明确提示），第一实例不受影响；
- 不同 HOME 互不阻塞；
- 第一个实例退出后锁释放，可再次启动；
- 同 HOME 并发启动恰好一个成功；
- 锁文件权限 0600。

不覆盖：`open -n` 的 LaunchServices 归一化、真实 GUI 提示框（人工项）。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
APP = os.path.join(BUILD, "Arca-test.app", "Contents", "MacOS", "Arca")
STATE = os.path.join("Library", "Application Support", "Arca")


def _wait_for(predicate, timeout=20.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class App:
    def __init__(self, home, label):
        self.home = home
        self.label = label
        self.tmp = tempfile.mkdtemp(prefix="si-%s-" % label, dir=home)
        self.port_file = os.path.join(self.tmp, "port.json")
        env = dict(os.environ, HOME=home, TMPDIR=self.tmp)
        env.pop("ARCA_HOME", None)
        self.proc = subprocess.Popen(
            [APP, "--headless", "--port-file", self.port_file],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            start_new_session=True)
        self.port = None

    def wait_ready(self, timeout=25.0):
        def readable():
            try:
                with open(self.port_file, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return None
        data = _wait_for(readable, timeout)
        if data:
            self.port = data.get("port")
        return bool(data)

    def stop(self, timeout=20.0):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        return self.proc.returncode

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SingleInstanceTest(unittest.TestCase):
    def setUp(self):
        self.roots = []

    def tearDown(self):
        for app in getattr(self, "apps", []):
            try:
                app.stop()
            except Exception:
                pass
            app.cleanup()
        for root in self.roots:
            shutil.rmtree(root, ignore_errors=True)

    def _home(self, label):
        root = tempfile.mkdtemp(prefix="arca-si-%s-" % label)
        self.roots.append(root)
        return root

    def _start(self, home, label):
        if not hasattr(self, "apps"):
            self.apps = []
        app = App(home, label)
        self.apps.append(app)
        return app

    def test_same_home_second_instance_refused(self):
        home = self._home("same")
        first = self._start(home, "one")
        self.assertTrue(first.wait_ready(), "第一个实例未就绪")

        second = self._start(home, "two")
        try:
            code = second.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.fail("同 HOME 第二实例没有退出（单实例锁未生效）")
        err = second.proc.stderr.read().decode("utf-8", "replace")
        self.assertEqual(code, 3, "第二实例退出码应为 3，实际 %r" % (code,))
        self.assertIn("另一个 Arca 实例", err)
        self.assertIn("ARCA_HOME", err)
        # 第一实例仍在服务：端口仍可连（headless 进程存活即可）
        self.assertIsNone(first.proc.poll())
        self.assertTrue(first.port)

    def test_different_home_not_blocked(self):
        home_a = self._home("a")
        home_b = self._home("b")
        first = self._start(home_a, "a")
        self.assertTrue(first.wait_ready())
        second = self._start(home_b, "b")
        try:
            self.assertTrue(second.wait_ready(25.0), "不同 HOME 的第二实例被错误阻塞")
        finally:
            second.stop()

    def test_lock_released_after_exit(self):
        home = self._home("release")
        first = self._start(home, "first")
        self.assertTrue(first.wait_ready())
        first.stop()
        time.sleep(0.3)
        again = self._start(home, "again")
        self.assertTrue(again.wait_ready(), "前一个实例退出后锁未释放")

    def test_lock_file_mode_is_owner_only(self):
        home = self._home("mode")
        app = self._start(home, "mode")
        self.assertTrue(app.wait_ready())
        lock = os.path.join(home, STATE, "instance.lock")
        self.assertTrue(os.path.exists(lock), "未创建 instance.lock")
        mode = stat.S_IMODE(os.stat(lock).st_mode)
        self.assertEqual(mode, 0o600, "instance.lock 权限应为 0600，实际 %o" % mode)
        holder = open(lock, "r", encoding="utf-8").read()
        self.assertIn("pid=", holder)
        self.assertIn("home=", holder)

    def test_concurrent_start_yields_single_winner(self):
        home = self._home("race")
        one = self._start(home, "race1")
        two = self._start(home, "race2")
        deadline = time.time() + 30.0
        winner = None
        while time.time() < deadline:
            for app in (one, two):
                if app.proc.poll() is None and app.wait_ready(0.1):
                    winner = app
            if winner:
                break
            if one.proc.poll() is not None and two.proc.poll() is not None:
                break
        self.assertIsNotNone(winner, "并发启动没有成功者")
        losers = [a for a in (one, two) if a is not winner]
        loser = losers[0]
        try:
            code = loser.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.fail("并发启动的失败者没有退出")
        self.assertEqual(code, 3)
        self.assertIsNone(winner.proc.poll(), "并发启动后胜者意外退出")


def main():
    if not os.path.exists(APP):
        print("SKIP: 缺少测试包 %s（先 make bundle 或 sync_bundle）" % APP)
        return 2
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        SingleInstanceTest("test_same_home_second_instance_refused"),
        SingleInstanceTest("test_different_home_not_blocked"),
        SingleInstanceTest("test_lock_released_after_exit"),
        SingleInstanceTest("test_lock_file_mode_is_owner_only"),
        SingleInstanceTest("test_concurrent_start_yields_single_winner"),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
