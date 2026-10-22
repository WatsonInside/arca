from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
from arca import engine, server

HINT = ("ZIP 格式暂不支持含中文等非 ASCII 字符的密码，"
        "请改用 7z 格式（同时会加密文件名，更安全）")


class ZipPasswordTests(unittest.TestCase):
    def setUp(self):
        warning_scope = warnings.catch_warnings()
        warning_scope.__enter__()
        self.addCleanup(warning_scope.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "input.txt"
        self.source.write_text("archive regression fixture\n")
        self.engine = engine.Engine()
        self.manager = SimpleNamespace(engine=self.engine, settings={"suppressSound": True},
                                       submit=Mock(), _cv=server.threading.Condition(), _targets={})
        self.handler = object.__new__(server.Handler)
        self.handler.ctx = SimpleNamespace(engine=self.engine, manager=self.manager)
        self.handler._err = lambda message: {"ok": False, "error": message}
        self.handler._json = lambda payload: payload

    def body(self, fmt, password):
        return {"inputs": [str(self.source)], "destDir": str(self.root),
                "name": "archive", "format": fmt, "password": password,
                "overwrite": True}

    def create(self, fmt, password):
        result = self.handler._compress(self.body(fmt, password))
        self.assertTrue(result["ok"])
        job, task = self.manager.submit.call_args.args
        task(job)
        target = Path(result["target"])
        self.assertTrue(target.is_file())
        info = self.engine.inspect(str(target), password)
        self.assertEqual(info["counts"]["files"], 1)
        self.assertEqual(info["entries"][0]["path"], self.source.name)
        code, _ = self.engine.run([self.engine.zz, "t", str(target), "-p" + password])
        self.assertEqual(code, 0, "Archive verification failed")
        return result

    def test_ascii_zip_real_creation(self):
        self.assertFalse(self.create("zip", "AsciiFixture42!")["namesEncrypted"])

    def test_non_ascii_7z_real_creation(self):
        self.assertTrue(self.create("7z", "中文Fixture42!")["namesEncrypted"])

    def test_non_ascii_zip_rejects_before_target_removal(self):
        target = self.root / "archive.zip"
        target.write_bytes(b"existing archive must survive")
        with patch.object(server, "_remove_target") as remove:
            for password in ("中文Fixture42!", "caf\u00e9", "emoji\U0001f512", "\u0080"):
                result = self.handler._compress(self.body("zip", password))
                self.assertEqual(result, {"ok": False, "error": HINT})
                self.assertEqual(target.read_bytes(), b"existing archive must survive")
        remove.assert_not_called()
        self.manager.submit.assert_not_called()

    def test_http_rejection_is_synchronous_and_preserves_target(self):
        target = self.root / "archive.zip"
        target.write_bytes(b"existing archive")
        ctx = SimpleNamespace(engine=self.engine, manager=self.manager, token="fixture-token")
        service = server.Server(ctx)
        service.start()
        self.addCleanup(service.httpd.server_close)
        self.addCleanup(service.stop)
        request = urllib.request.Request(
            f"http://127.0.0.1:{service.port}/api/compress",
            data=json.dumps(self.body("zip", "中文Fixture42!")).encode(),
            headers={"Content-Type": "application/json", "X-Arca-Token": ctx.token})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            payload = json.load(response)
            self.assertEqual(response.status, 400)
        self.assertEqual(payload, {"ok": False, "error": HINT})
        self.assertEqual(target.read_bytes(), b"existing archive")
        self.manager.submit.assert_not_called()

    def test_contextual_mapping(self):
        self.assertEqual(engine.describe_error("E_INVALIDARG", 2,
                         format="zip", has_password=True), HINT)

    def test_mapping_does_not_escape_zip_password_failure(self):
        for fmt, has_password in (("", False), ("zip", False), ("7z", True),
                                  ("rar", True), ("tar.zst", True)):
            self.assertEqual(engine.describe_error("E_INVALIDARG", 2,
                             format=fmt, has_password=has_password), "操作失败")
        self.assertEqual(engine.describe_error("E_INVALIDARG", 0,
                         format="zip", has_password=True), "")
        self.assertEqual(engine.describe_error("E_INVALIDARG", 2, True,
                         format="zip", has_password=True), "已取消")
        self.assertEqual(engine.describe_error("Wrong password", 2,
                         format="zip", has_password=True), "密码不正确")

    def test_compress_task_supplies_mapping_context(self):
        self.engine.run = Mock(return_value=(2, "E_INVALIDARG"))
        with self.assertRaises(engine.EngineError) as caught:
            self.create("zip", "AsciiFixture42!")
        self.assertEqual(str(caught.exception), HINT)

    def test_password_hint_current_state_and_refresh_paths(self):
        source = (ROOT / "src/arca/web/app.js").read_text()
        start = source.index("function pwStrength(")
        end = source.index("function genPassword(", start)
        script = "const state={c:{pw:'',format:'zip'}};const nodes={};"
        script += "const $=s=>nodes[s]||(nodes[s]={dataset:{},textContent:''});\n"
        script += source[start:end]
        script += "\nconst cases=" + json.dumps([
            ["zip", "中文Fixture", HINT], ["7z", "中文Fixture", None],
            ["zip", "ASCII42!", None], ["zip", "", ""],
            ["zip", "caf\u00e9", HINT], ["7z", "caf\u00e9", None]]) + ";\n"
        script += "for(const [format,pw,want] of cases){state.c={format,pw};refreshCompressPasswordHint();"
        script += "const expected=want===null?STRENGTH_TEXT[pwStrength(pw)]:want;"
        script += "if($('#c-pw-text').textContent!==expected)throw Error('Hint state mismatch');}"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, "Password hint state test failed")
        self.assertEqual(source.count("refreshCompressPasswordHint();"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
