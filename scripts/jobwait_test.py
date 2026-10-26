"""await_job / reclaim_active 的取消时序与终态识别单元测试。

不触网、不睡真实时间：fetch/cancel 注入脚本化任务，clock/sleep 用假时钟
推进，POLL 步进只累加计数。锁定三件事：取消时机（完整 timeout 前绝不
取消）、真实终态识别（cancelled 双 l）、回收失败诊断（reclaimed=False）。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import jobwait


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def script_fetch(script, jid="j1"):
    """每次 fetch 弹出下一个状态；耗尽后停在最后一个。"""
    states = list(script)

    def fetch():
        return {"id": jid, "status": states.pop(0) if len(states) > 1 else states[0]}
    return fetch


class AwaitJobTests(unittest.TestCase):
    def test_terminal_statuses_recognized_without_cancel(self):
        # done / error / cancelled 都是终态；queued/running 不是。
        for status, expect_terminal in [("done", True), ("error", True),
                                        ("cancelled", True),
                                        ("queued", False), ("running", False)]:
            with self.subTest(status=status):
                cancels = []
                clock = FakeClock()
                job, reclaimed = jobwait.await_job(
                    lambda s=status: {"id": "j1", "status": s},
                    lambda: cancels.append(round(clock.now, 3)),
                    timeout=1.2, sleep=clock.sleep, clock=clock.clock)
                self.assertEqual(reclaimed, expect_terminal)
                self.assertEqual(job["status"], status)
                if expect_terminal:
                    self.assertEqual(cancels, [])      # 终态即刻返回：零等待零取消
                    self.assertEqual(clock.now, 0.0)
                else:
                    # 非终态：完整 timeout 前不取消，到点恰好取消一次。
                    self.assertEqual(cancels, [1.2])
                    self.assertGreaterEqual(clock.now, 1.2)

    def test_cancel_only_after_full_timeout_and_only_once(self):
        clock = FakeClock()
        cancels = []
        state = {"status": "running"}

        def fetch():
            return {"id": "j1", "status": state["status"]}

        def cancel():
            cancels.append(round(clock.now, 3))
            state["status"] = "cancelled"

        job, reclaimed = jobwait.await_job(fetch, cancel, timeout=2,
                                           sleep=clock.sleep, clock=clock.clock)
        # 取消恰好在完整 timeout 到点发生、且只发生一次；之前绝不取消。
        self.assertEqual(cancels, [2.0])
        self.assertTrue(reclaimed)
        self.assertEqual(job["status"], "cancelled")
        self.assertGreaterEqual(clock.now, 2.0)

    def test_reclaim_failure_surfaces_for_diagnosis(self):
        # 取消也不奏效（后端卡死）：必须以 reclaimed=False 交还给调用方诊断，
        # 且 timeout 与 grace 两个窗口都完整花满。
        clock = FakeClock()
        cancels = []
        job, reclaimed = jobwait.await_job(
            lambda: {"id": "j1", "status": "running"},
            lambda: cancels.append(1), timeout=1, grace=1,
            sleep=clock.sleep, clock=clock.clock)
        self.assertFalse(reclaimed)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "running")
        self.assertEqual(len(cancels), 1)
        self.assertGreaterEqual(clock.now, 2.0)

    def test_queued_running_then_cancelled_sequence(self):
        clock = FakeClock()
        cancels = []
        fetch = script_fetch(["queued", "queued", "running", "running",
                              "cancelled"])
        job, reclaimed = jobwait.await_job(fetch, lambda: cancels.append(clock.now),
                                           timeout=10, sleep=clock.sleep,
                                           clock=clock.clock)
        self.assertEqual(cancels, [])          # 自然到达终态：全程未取消
        self.assertTrue(reclaimed)
        self.assertEqual(job["status"], "cancelled")

    def test_vanished_job_reported_as_none(self):
        job, reclaimed = jobwait.await_job(lambda: None, lambda: None, timeout=1,
                                           sleep=lambda s: None, clock=lambda: 0)
        self.assertIsNone(job)
        self.assertTrue(reclaimed)             # 消失视同已回收，由调用方定夺


class ReclaimActiveTests(unittest.TestCase):
    def test_cancels_every_active_job_then_confirms_recovery(self):
        clock = FakeClock()
        cancels = []
        state = {"a": "running", "b": "queued"}

        def list_jobs():
            return [{"id": "a", "status": state["a"]},
                    {"id": "b", "status": state["b"]}]

        def cancel(jid):
            cancels.append(jid)
            state[jid] = "cancelled"

        left, jobs = jobwait.reclaim_active(list_jobs, cancel, timeout=5,
                                            sleep=clock.sleep, clock=clock.clock)
        self.assertEqual(cancels, ["a", "b"])
        self.assertEqual(left, 0)
        self.assertTrue(all(j["status"] in jobwait.TERMINAL for j in jobs))

    def test_reports_stuck_job_after_grace(self):
        clock = FakeClock()
        list_jobs = lambda: [{"id": "a", "status": "running"}]
        left, jobs = jobwait.reclaim_active(list_jobs, lambda jid: None, timeout=1,
                                            sleep=clock.sleep, clock=clock.clock)
        self.assertEqual(left, 1)
        self.assertEqual(jobs[0]["status"], "running")
        self.assertGreaterEqual(clock.now, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
