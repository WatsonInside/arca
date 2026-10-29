"""T10：中断任务的“预填重开”后端契约。

锁定五件事：
1. 安全参数白名单（密码等绝不进入 journal / retry-request）；
2. journal schema v2 只持久化安全参数；
3. 崩溃恢复后 history 标记 retryable、并落一份可重试参数；
4. 旧 schema（v1）不会被当作可重试，升级不做静默猜测；
5. retry_plan 对源移动/缺失、部分解压条目失效给出明确错误或警告。

全部使用临时 HOME + 假引擎/假设置；不启动 App、不启动引擎。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from arca import jobs, util  # noqa: E402


class _Settings:
    def get(self, key, default=None):
        return default


class _HomeCase(unittest.TestCase):
    def setUp(self):
        self._old_home = os.environ.get("HOME")
        self.home = tempfile.mkdtemp(prefix="arca-retry-home-")
        os.environ["HOME"] = self.home
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            try:
                manager.shutdown(timeout=3)
            except Exception:
                pass
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self.home, ignore_errors=True)

    def _manager(self):
        manager = jobs.JobManager(object(), _Settings())
        self.managers.append(manager)
        return manager

    def _journal_record(self, job_id, **over):
        record = {
            "id": job_id, "kind": "compress", "title": "压缩 示例",
            "subtitle": "示例", "pid": 99999999, "created": time.time() - 10,
            "srcPath": "", "destPath": self.home, "planned": self.home + "/示例.7z",
            "format": "7Z", "total": 0, "bucket": self.home, "schema": 2,
            "passwordRequired": False,
            "request": {"inputs": [self.home + "/a.txt"],
                        "destDir": self.home, "name": "示例.7z", "format": "7z"},
        }
        record.update(over)
        path = os.path.join(manager_pending, job_id + ".json")
        util.write_json(path, record)
        return record


class SafeRequestTests(unittest.TestCase):
    def test_whitelist_drops_password_and_unknown_fields(self):
        body = {
            "inputs": ["/tmp/a.txt"], "destDir": "/tmp", "name": "a.7z",
            "format": "7z", "level": 3, "method": "auto", "splitBytes": 0,
            "password": "s3cret-pw", "remember": True, "info": {"x": 1},
            "onExists": "overwrite",
        }
        req = jobs.safe_request("compress", body)
        self.assertNotIn("password", req)
        self.assertNotIn("remember", req)
        self.assertNotIn("info", req)
        self.assertEqual(req["inputs"], ["/tmp/a.txt"])
        self.assertEqual(req["name"], "a.7z")
        raw = json.dumps(req, ensure_ascii=False)
        self.assertNotIn("s3cret-pw", raw)


class JournalSchemaTests(_HomeCase):
    def test_journal_schema_v2_has_safe_request_and_no_password(self):
        manager = self._manager()
        body = {"inputs": [self.home + "/a.txt"], "destDir": self.home,
                "name": "a.7z", "format": "7z", "level": 3,
                "password": "canary-secret", "remember": True}
        job = jobs.Job("compress", "压缩 a.7z", destPath=self.home,
                       planned=self.home + "/a.7z", secrets=["canary-secret"],
                       request=jobs.safe_request("compress", body))
        manager._journal_write(job)  # noqa: SLF001 - 契约级测试
        raw = open(manager._journal_path(job.id), "r", encoding="utf-8").read()  # noqa: SLF001
        record = json.loads(raw)
        self.assertEqual(record["schema"], jobs.JOURNAL_SCHEMA)
        self.assertEqual(record["request"]["inputs"], [self.home + "/a.txt"])
        self.assertTrue(record["passwordRequired"])
        self.assertNotIn("canary-secret", raw)
        self.assertNotIn('"password"', raw)


class RecoveryRetryableTests(_HomeCase):
    def test_crashed_v2_job_becomes_retryable_with_retry_params(self):
        manager = self._manager()
        global manager_pending
        manager_pending = manager._pending_dir  # noqa: SLF001
        jid = "deadbeef0001"
        self._journal_record(jid)
        manager.shutdown(timeout=3)

        fresh = self._manager()
        entry = next((i for i in fresh.history if i.get("id") == jid), None)
        self.assertIsNotNone(entry, "崩溃日志没有恢复成中断记录")
        self.assertEqual(entry["status"], "interrupted")
        self.assertTrue(entry["result"]["retryable"])
        path = jobs.retry_request_path(jid)
        self.assertTrue(os.path.exists(path), "未保存可重试参数")
        doc = json.load(open(path, "r", encoding="utf-8"))
        self.assertEqual(doc["schema"], jobs.JOURNAL_SCHEMA)
        self.assertIn("inputs", doc["request"])

    def test_v1_journal_is_not_retryable(self):
        manager = self._manager()
        global manager_pending
        manager_pending = manager._pending_dir  # noqa: SLF001
        jid = "olddead000002"
        record = self._journal_record(jid, schema=1)
        record.pop("request", None)
        util.write_json(os.path.join(manager_pending, jid + ".json"), record)
        manager.shutdown(timeout=3)

        fresh = self._manager()
        entry = next((i for i in fresh.history if i.get("id") == jid), None)
        self.assertIsNotNone(entry)
        self.assertFalse((entry.get("result") or {}).get("retryable"))
        self.assertFalse(os.path.exists(jobs.retry_request_path(jid)))


class RetryPlanValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arca-retryplan-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, name, content=b"x"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def test_missing_inputs_are_removed_with_warning(self):
        alive = self._file("alive.txt")
        plan, error = jobs.retry_plan(None, {"inputs": [alive, self.tmp + "/gone.txt"],
                                             "destDir": self.tmp}, "compress")
        self.assertIsNone(error)
        self.assertEqual(plan["request"]["inputs"], [alive])
        self.assertTrue(any("不存在" in w for w in plan["warnings"]))

    def test_all_inputs_gone_is_an_error(self):
        plan, error = jobs.retry_plan(None, {"inputs": [self.tmp + "/gone.txt"]}, "compress")
        self.assertIsNone(plan)
        self.assertIn("都不在了", error)

    def test_missing_archive_is_an_error(self):
        plan, error = jobs.retry_plan(None, {"path": self.tmp + "/gone.7z",
                                             "destDir": self.tmp}, "extract")
        self.assertIsNone(plan)
        self.assertIn("归档已不存在", error)

    def test_partial_entries_fall_back_to_whole_archive_with_warning(self):
        archive = self._file("a.7z")
        plan, error = jobs.retry_plan(None, {"path": archive, "destDir": self.tmp,
                                            "entries": ["a.txt"]}, "extract")
        self.assertIsNone(error)
        self.assertNotIn("entries", plan["request"])
        self.assertTrue(any("部分解压" in w for w in plan["warnings"]))


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(SafeRequestTests),
        loader.loadTestsFromTestCase(JournalSchemaTests),
        loader.loadTestsFromTestCase(RecoveryRetryableTests),
        loader.loadTestsFromTestCase(RetryPlanValidationTests),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
