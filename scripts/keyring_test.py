from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from arca import jobs, keyring, server


class KeyringTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.index = self.root / "saved-passwords.json"
        state = patch.object(keyring.util, "state_path", side_effect=lambda name: str(self.root / name))
        state.start()
        self.addCleanup(state.stop)
        self.secret = "a6-private-value-独立"
        self.cli = patch.object(keyring.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=(self.secret + "\n").encode()))
        self.run = self.cli.start()
        self.addCleanup(self.cli.stop)
        self.saved = keyring.SavedPasswords()
        self.path = str(self.root / "archive.7z")

    def test_account_resolves_path_and_hashes(self):
        expected = hashlib.sha256(str(Path(self.path).resolve()).encode()).hexdigest()[:16]
        self.assertEqual(keyring.account(self.path), expected)
        self.assertEqual(keyring.account(str(self.root / "sub" / ".." / "archive.7z")), expected)
        self.assertNotIn("archive", expected)

    def test_default_and_truthy_do_not_store(self):
        for opt in (False, None, 1, "true"):
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=opt))
        self.run.assert_not_called()
        self.assertFalse(self.index.exists())

    def test_store_service_and_account(self):
        self.assertTrue(self.saved.remember(self.path, self.secret, opt_in=True))
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[:2], ["/usr/bin/security", "add-generic-password"])
        self.assertEqual(argv[argv.index("-s") + 1], "Arca")
        self.assertEqual(argv[argv.index("-a") + 1], keyring.account(self.path))
        self.assertNotIn(self.path, argv)

    def test_lookup(self):
        self.assertEqual(keyring.lookup(self.path), self.secret)
        self.assertEqual(self.run.call_args.args[0][1], "find-generic-password")

    def test_miss(self):
        self.run.return_value.returncode = 44
        self.assertIsNone(keyring.lookup(self.path))

    def test_missing_cli(self):
        self.run.side_effect = FileNotFoundError()
        self.assertIsNone(keyring.lookup(self.path))
        self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True))
        self.assertTrue(self.saved.clear_all())

    def test_timeout(self):
        self.run.side_effect = subprocess.TimeoutExpired("security", 3)
        self.assertIsNone(keyring.lookup(self.path))
        self.assertFalse(keyring.remember(self.path, self.secret, opt_in=True))

    def test_forget(self):
        self.saved.remember(self.path, self.secret, opt_in=True)
        self.assertTrue(self.saved.forget(self.path))
        self.assertEqual(self.saved.summary()["count"], 0)
        self.assertEqual(self.run.call_args.args[0][1], "delete-generic-password")

    def test_clear_all(self):
        for i in range(3):
            self.saved.remember(self.path + str(i), self.secret, opt_in=True)
        self.assertTrue(self.saved.clear_all())
        self.assertEqual(keyring.SavedPasswords().summary(), {"count": 0, "files": []})

    def test_failed_clear_preserves_index(self):
        self.saved.remember(self.path, self.secret, opt_in=True)
        self.run.return_value.returncode = 1
        self.assertFalse(self.saved.clear_all())
        self.assertEqual(self.saved.summary()["count"], 1)

    def test_fifo_maximum_and_update_order(self):
        for i in range(50):
            self.saved.remember(self.path + str(i), self.secret, opt_in=True)
        self.saved.remember(self.path + "0", self.secret, opt_in=True)
        self.saved.remember(self.path + "50", self.secret, opt_in=True)
        self.assertEqual(self.saved.summary()["count"], 50)
        self.assertEqual(self.saved.summary()["files"][0], "archive.7z1")
        deletes = [c.args[0] for c in self.run.call_args_list if c.args[0][1] == "delete-generic-password"]
        self.assertEqual(deletes[0][-1], keyring.account(self.path + "0"))

    def test_index_only_nonsecret_basename_and_account(self):
        self.saved.remember(self.path, self.secret, opt_in=True)
        text = self.index.read_text()
        self.assertNotIn(self.secret, text)
        self.assertNotIn(str(self.root), text)
        self.assertEqual(set(json.loads(text)[0]), {"name", "account"})
        self.assertEqual(keyring.SavedPasswords().summary()["files"], ["archive.7z"])

    def test_index_write_failure_rolls_back(self):
        with patch.object(keyring.os, "replace", side_effect=OSError()):
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True))
        self.assertEqual(self.saved.summary()["count"], 0)
        self.assertEqual(self.run.call_args.args[0][1], "delete-generic-password")

    def handler(self):
        handler = object.__new__(server.Handler)
        handler.ctx = SimpleNamespace(engine=Mock(), manager=Mock(), saved_passwords=self.saved,
                                      settings={"passwordAuth": "off"})
        handler.ctx.engine.resolve.side_effect = lambda value: value
        handler._json = lambda value: keyring.redact(value, getattr(handler, "_secrets", ()))
        handler._err = lambda message: {"ok": False, "error": message}
        return handler

    def test_api_empty_password_lookup_and_no_echo(self):
        handler = self.handler()
        with patch.object(jobs, "make_test_task", return_value=Mock()) as task:
            response = handler._test({"path": self.path})
        self.assertTrue(response["savedPasswordUsed"])
        self.assertFalse(response["passwordRemembered"])
        self.assertEqual(task.call_args.args[2], self.secret)
        self.assertNotIn(self.secret, json.dumps(response))
        self.assertFalse(self.index.exists())

    def test_api_explicit_opt_in_before_submit(self):
        handler = self.handler()
        handler.ctx.manager.submit.side_effect = lambda *args: self.assertTrue(self.index.exists())
        with patch.object(jobs, "make_test_task", return_value=Mock()):
            response = handler._test({"path": self.path, "password": self.secret, "remember": True})
        self.assertTrue(response["passwordRemembered"])
        self.assertFalse(response["savedPasswordUsed"])
        self.assertNotIn(self.secret, json.dumps(response))

    def test_api_unchecked_and_truthy_never_write(self):
        handler = self.handler()
        with patch.object(jobs, "make_test_task", return_value=Mock()):
            for opt in (None, False, 1, "true"):
                handler._test({"path": self.path, "password": self.secret, "remember": opt})
        self.run.assert_not_called()

    def test_job_response_log_error_and_history_redacted(self):
        job = jobs.Job("test", self.secret, secrets=[self.secret])
        job.update(log="output " + self.secret, error="argv " + self.secret,
                   result={"detail": self.secret}, status="done")
        job.progress(2, self.secret)
        self.assertNotIn(self.secret, json.dumps(job.to_dict()))
        manager = object.__new__(jobs.JobManager)
        manager.history = []
        manager._cv = jobs.threading.Condition()
        manager.settings = {"suppressSound": True}
        manager._record(job)
        self.assertNotIn(self.secret, (self.root / "history.json").read_text())
        handler = object.__new__(server.Handler)
        handler._secrets = [self.secret]
        handler._send = Mock()
        handler._err("exception " + self.secret)
        self.assertNotIn(self.secret.encode(), handler._send.call_args.args[1])


class ProtectedKeyringTests(KeyringTests):
    def setUp(self):
        super().setUp()
        names = ("kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
                 "kSecAttrAccessControl", "kSecValueData", "kSecAttrAccessibleWhenUnlockedThisDeviceOnly",
                 "kSecAccessControlUserPresence", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne")
        self.api = SimpleNamespace(**{name: name for name in names}, errSecSuccess=0,
                                   errSecItemNotFound=-25300, errSecDuplicateItem=-25299)
        self.acl = object()
        self.api.SecAccessControlCreateWithFlags = Mock(return_value=(self.acl, None))
        self.api.SecItemAdd = Mock(return_value=(0, None))
        self.api.SecItemUpdate = Mock(return_value=0)
        self.api.SecItemCopyMatching = Mock(return_value=(0, self.secret.encode()))
        self.framework = patch.object(keyring, "_security", return_value=self.api)
        self.load = self.framework.start()
        self.addCleanup(self.framework.stop)

    def test_off_never_loads_framework(self):
        keyring.lookup(self.path)
        keyring.remember(self.path, self.secret, opt_in=True)
        self.load.assert_not_called()

    def test_protected_acl_and_nonsecret_index(self):
        self.assertTrue(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        self.api.SecAccessControlCreateWithFlags.assert_called_once_with(
            None, self.api.kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            self.api.kSecAccessControlUserPresence, None)
        attrs = self.api.SecItemAdd.call_args.args[0]
        self.assertIs(attrs[self.api.kSecAttrAccessControl], self.acl)
        self.assertEqual(attrs[self.api.kSecValueData], self.secret.encode())
        self.assertEqual(attrs[self.api.kSecAttrService], "Arca")
        self.assertEqual(attrs[self.api.kSecAttrAccount], keyring.account(self.path))
        self.assertNotIn(self.secret, self.index.read_text())
        self.run.assert_not_called()

    def test_protected_requires_strict_opt_in(self):
        for opt in (False, None, 1, "true"):
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=opt, auth="touchid"))
        self.load.assert_not_called()
        self.run.assert_not_called()

    def test_acl_creation_failure_closed(self):
        for result in ((None, None), (self.acl, object())):
            self.api.SecAccessControlCreateWithFlags.return_value = result
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        self.api.SecItemAdd.assert_not_called()
        self.run.assert_not_called()
        self.assertFalse(self.index.exists())

    def test_add_failure_no_cli_or_index(self):
        self.api.SecItemAdd.return_value = (-25293, None)
        self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        self.run.assert_not_called()
        self.assertFalse(self.index.exists())

    def test_duplicate_updates_acl_without_delete(self):
        self.api.SecItemAdd.return_value = (self.api.errSecDuplicateItem, None)
        self.assertTrue(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        query, attrs = self.api.SecItemUpdate.call_args.args
        self.assertIs(attrs[self.api.kSecAttrAccessControl], self.acl)
        self.assertEqual(query[self.api.kSecAttrAccount], keyring.account(self.path))
        self.run.assert_not_called()

    def test_duplicate_update_failure_preserves_item(self):
        self.saved.remember(self.path, self.secret, opt_in=True)
        before = self.index.read_text()
        self.run.reset_mock()
        self.api.SecItemAdd.return_value = (self.api.errSecDuplicateItem, None)
        self.api.SecItemUpdate.return_value = -25293
        self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        self.assertEqual(self.index.read_text(), before)
        self.run.assert_not_called()

    def fill_index(self):
        for i in range(keyring.LIMIT):
            self.assertTrue(self.saved.remember(self.path + str(i), self.secret, opt_in=True))
        self.run.reset_mock()

    def test_protected_eviction_failure_rolls_back_new_item(self):
        self.fill_index()
        before = self.index.read_bytes()
        with patch.object(keyring, "_delete", side_effect=[False, True]) as delete:
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        self.assertEqual([call.args[0] for call in delete.call_args_list],
                         [keyring.account(self.path + "0"), keyring.account(self.path)])
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(keyring.SavedPasswords().summary()["count"], keyring.LIMIT)
        self.api.SecItemAdd.assert_called_once()

    def test_protected_failed_rollback_remains_cleanable_after_restart(self):
        self.fill_index()
        before = json.loads(self.index.read_text())
        with patch.object(keyring, "_delete", return_value=False):
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        rows = json.loads(self.index.read_text())
        self.assertEqual(rows[:-1], before)
        self.assertEqual(rows[-1], {"account": keyring.account(self.path),
                                    "name": Path(self.path).name, "cleanup": True})
        self.assertNotIn(self.secret, self.index.read_text())
        restarted = keyring.SavedPasswords()
        self.assertEqual(restarted.summary()["count"], keyring.LIMIT + 1)
        with patch.object(keyring, "_delete", return_value=True) as delete:
            self.assertTrue(restarted.clear_all())
        self.assertIn(keyring.account(self.path), [call.args[0] for call in delete.call_args_list])
        self.assertEqual(keyring.SavedPasswords().summary()["count"], 0)

    def test_protected_full_index_requires_durable_cleanup_record_before_write(self):
        self.fill_index()
        before = self.index.read_bytes()
        with patch.object(self.saved, "_save", return_value=False):
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        self.api.SecItemAdd.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.saved.summary()["count"], keyring.LIMIT)

    def test_protected_full_index_failed_final_save_retains_durable_cleanup_record(self):
        self.fill_index()
        save = self.saved._save
        def save_pending_once():
            if len(json.loads(self.index.read_text())) == keyring.LIMIT:
                return save()
            return False
        with patch.object(self.saved, "_save", side_effect=save_pending_once):
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        restarted = keyring.SavedPasswords()
        self.assertEqual(restarted.summary()["count"], keyring.LIMIT + 1)
        with patch.object(keyring, "_delete", return_value=True) as delete:
            self.assertTrue(restarted.clear_all())
        self.assertIn(keyring.account(self.path), [call.args[0] for call in delete.call_args_list])

    def test_protected_full_index_success_keeps_fifo_limit(self):
        self.fill_index()
        self.assertTrue(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        rows = json.loads(self.index.read_text())
        self.assertEqual(len(rows), keyring.LIMIT)
        self.assertEqual(rows[0]["account"], keyring.account(self.path + "1"))
        self.assertEqual(rows[-1], {"account": keyring.account(self.path), "name": Path(self.path).name})

    def test_off_eviction_failure_does_not_write_or_rollback(self):
        self.fill_index()
        before = self.index.read_bytes()
        with patch.object(keyring, "_delete", return_value=False) as delete:
            self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True))
        delete.assert_called_once_with(keyring.account(self.path + "0"))
        self.api.SecItemAdd.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(self.index.read_bytes(), before)

    def test_protected_lookup_success(self):
        self.assertEqual(keyring.lookup(self.path, auth="touchid"), self.secret)
        self.run.assert_not_called()
        query = self.api.SecItemCopyMatching.call_args.args[0]
        self.assertIs(query[self.api.kSecReturnData], True)
        self.assertEqual(query[self.api.kSecAttrAccount], keyring.account(self.path))

    def test_only_not_found_falls_back_after_framework(self):
        self.api.SecItemCopyMatching.return_value = (self.api.errSecItemNotFound, None)
        self.run.side_effect = lambda *a, **k: (
            self.api.SecItemCopyMatching.assert_called_once() or
            SimpleNamespace(returncode=0, stdout=self.secret.encode()))
        self.assertEqual(keyring.lookup(self.path, auth="touchid"), self.secret)
        self.assertEqual(self.run.call_args.args[0][1], "find-generic-password")

    def test_auth_cancel_other_errors_never_fallback_or_delete(self):
        for status in (-25293, -128, -25308, -50):
            self.api.SecItemCopyMatching.return_value = (status, None)
            self.assertIsNone(keyring.lookup(self.path, auth="touchid"))
        self.run.assert_not_called()

    def test_framework_exception_and_bad_data_closed(self):
        self.load.side_effect = RuntimeError(self.secret)
        self.assertIsNone(keyring.lookup(self.path, auth="touchid"))
        self.assertFalse(self.saved.remember(self.path, self.secret, opt_in=True, auth="touchid"))
        self.load.side_effect = None
        self.api.SecItemCopyMatching.return_value = (0, b"\xff")
        self.assertIsNone(keyring.lookup(self.path, auth="touchid"))
        self.run.assert_not_called()

    def test_settings_validation_does_not_migrate(self):
        from arca.settings import Settings
        settings = Settings()
        self.assertEqual(settings.get("passwordAuth"), "off")
        settings.patch({"passwordAuth": "touchid"})
        for value in (True, None, "yes", [], {}):
            settings.patch({"passwordAuth": value})
            self.assertEqual(settings.get("passwordAuth"), "touchid")
        self.assertEqual(Settings().get("passwordAuth"), "touchid")
        (self.root / "settings.json").write_text('{"passwordAuth":"invalid"}')
        self.assertEqual(Settings().get("passwordAuth"), "off")
        self.load.assert_not_called()
        self.run.assert_not_called()

    def test_api_passes_touchid_lookup_and_remember(self):
        handler = self.handler()
        handler.ctx.settings["passwordAuth"] = "touchid"
        with patch.object(jobs, "make_test_task", return_value=Mock()):
            response = handler._test({"path": self.path, "remember": True})
        self.assertTrue(response["savedPasswordUsed"])
        self.assertTrue(response["passwordRemembered"])
        self.assertNotIn(self.secret, json.dumps(response))
        self.api.SecItemCopyMatching.assert_called_once()
        self.api.SecItemAdd.assert_called_once()
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
